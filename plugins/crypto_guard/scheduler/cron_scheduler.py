from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.data.binance_rest import MarketDataError
from plugins.crypto_guard.data.candle_backfill import backfill_symbol_interval
from plugins.crypto_guard.data.candle_store import fetch_and_upsert_closed_klines
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.utils import INTERVAL_MS, latest_closed_close_time_ms, utc_ms


LOGGER = get_logger("crypto_guard.scheduler")


def fetch_closed_klines_for_active_symbols(interval: str, lookback: int, *, analysis_time_utc: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        analysis_time = latest_closed_close_time_ms(interval, analysis_time_utc or utc_ms())
        results = []
        for symbol in repo.active_analysis_symbols():
            try:
                result = fetch_and_upsert_closed_klines(repo, symbol, interval, analysis_time_utc=analysis_time, lookback=lookback)
            except MarketDataError as exc:
                LOGGER.warning("fetch_closed_klines failed symbol=%s interval=%s error=%s", symbol, interval, exc)
                result = {"ok": False, "symbol": symbol, "interval": interval, "error": str(exc), "analysis_time_utc": analysis_time}
            if interval in {"1d", "4h", "1h"}:
                try:
                    result["agent_summary"] = summarize_higher_timeframe(repo, symbol, interval, analysis_time)
                except Exception as exc:
                    result["agent_summary"] = {"ok": False, "error": str(exc)}
            results.append(result)
        return {"ok": all(item.get("ok") for item in results), "interval": interval, "analysis_time_utc": analysis_time, "results": results}
    finally:
        conn.close()


def summarize_higher_timeframe(repo: CryptoGuardRepository, symbol: str, interval: str, analysis_time_utc: int) -> dict[str, Any]:
    candles = repo.get_candles(symbol, interval, analysis_time_utc=analysis_time_utc, limit=80)
    fallback = {
        "summary": f"{symbol} {interval} K 线已更新，等待后续多周期分析引用。",
        "trend_context": "unknown",
        "key_levels": [],
        "risk_notes": [],
    }
    agent = run_agent_json_task(
        task_name="higher_timeframe_kline_summary",
        payload={
            "symbol": symbol,
            "interval": interval,
            "analysis_time_utc": int(analysis_time_utc),
            "recent_candles": candles[-40:],
        },
        fallback=fallback,
        instructions=[
            "总结高周期 K 线背景，提取趋势状态、关键区域和风险，供低周期巡航复用。",
            "只基于已收盘 K 线，不得使用未来函数，不得输出实盘建议。",
        ],
    )
    repo.save_module_result(symbol, interval, analysis_time_utc, "ga_higher_timeframe_summary", agent, None)
    return agent


def _commit_skill_log_lifecycle(
    conn: Any,
    repo: CryptoGuardRepository,
    prepared: list[dict[str, Any]],
    *,
    batch_id: str,
    attempt_id: int,
) -> None:
    """07-14 R8 P2-NEW-1 (contract #2): on a SUCCESSFUL Phase-2 seal, flip every
    prepared skill-execution log to ``commit_state='committed'`` and write the
    DEFERRED feedback rows -- inside the still-open transaction.

    The prepared audit rows were written in Phase 1 (autocommit); this flips them
    to 'committed' so ``latest_skill_result_refs`` (gated on
    'committed'/'legacy_committed') exposes them to the orchestrator. The
    feedback was COLLECTED in Phase 1 (not written), so this is the FIRST and
    only feedback write -- it lands atomically with the batch. A ROLLBACK of the
    outer transaction reverts the feedback INSERTs AND the UPDATEs, leaving the
    logs 'prepared' (to be terminalized by the abort path / recovery).

    07-15 R9-P1a: the feedback write uses the STRICT path (``strict=True``) so a
    ``save_skill_feedback_memory`` failure PROPAGATES into the Phase-2 ``except``
    -> ``ROLLBACK`` -> ``_abort_unsealed_skill_logs``. Pre-R9-P1a the write was
    wrapped in a broad ``except: pass`` that SILENTLY dropped the feedback while
    the batch sealed + log committed -- breaking cross-round continuity.

    07-15 R9-P2: the commit UPDATE is now a CAS keyed on
    ``(commit_state='prepared', batch_id, attempt_id)`` with a ``rowcount==1``
    guard. ``attempt_id`` is a per-producer-call monotonic identity (derived in
    ``enqueue_market_analysis``) so an exception-recovery / retry of the SAME
    batch is auditable and a stale/aborted row CANNOT be silently re-marked
    ``committed``. A CAS mismatch (rowcount != 1) RAISES so the transaction rolls
    back and the prepared log is aborted -- fail-closed.
    """
    # Avoid a circular import: runner lives in the skills package.
    from plugins.crypto_guard.skills.runner import _write_skill_feedback

    for item in prepared:
        skill_sink = item.get("skill_sink") or []
        if not skill_sink:
            continue
        for (log_id, feedback_payload) in skill_sink:
            # R9-P2 CAS: only flip a row that is STILL 'prepared' AND belongs to
            # THIS (batch_id, attempt_id). A row already aborted by recovery, or
            # stamped with a different attempt_id (a stale retry), must NOT be
            # silently re-committed. rowcount != 1 -> fail-closed (raise ->
            # ROLLBACK -> abort the prepared log).
            cur = conn.execute(
                "UPDATE skill_execution_logs SET commit_state='committed' "
                "WHERE id=? AND commit_state='prepared' AND batch_id=? AND attempt_id=?",
                (int(log_id), batch_id, int(attempt_id)),
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError(
                    "skill log CAS-commit failed: id=%s batch_id=%s attempt_id=%s "
                    "did not match exactly one 'prepared' row (rowcount=%d) -- "
                    "fail-closed to prevent re-marking an aborted/stale log "
                    "'committed'." % (int(log_id), batch_id, int(attempt_id),
                                      int(cur.rowcount or 0)),
                )
            if feedback_payload is not None:
                _write_skill_feedback(repo, int(log_id), feedback_payload, strict=True)


def _abort_unsealed_skill_logs(
    conn: Any,
    prepared: list[dict[str, Any]],
) -> None:
    """07-14 R8 P2-NEW-1 (contract #3): on a Phase-2 seal failure / crash, mark
    every prepared skill-execution log ``commit_state='aborted_unsealed'``.

    The prepared audit rows were autocommitted in Phase 1 (outside the rolled-
    back transaction), so ROLLBACK cannot remove them. Instead they are
    terminalized here in a fresh autocommit UPDATE so they are RETAINED as audit
    but EXCLUDED from learning (``latest_skill_result_refs`` only reads
    'committed'/'legacy_committed'). NO feedback is written on this path -- the
    collected feedback payloads are simply dropped -- so a failed tick never
    pollutes future learning/decisions.
    """
    log_ids: list[int] = []
    for item in prepared:
        for (log_id, _payload) in (item.get("skill_sink") or []):
            log_ids.append(int(log_id))
    if not log_ids:
        return
    placeholders = ",".join("?" for _ in log_ids)
    conn.execute(
        f"UPDATE skill_execution_logs SET commit_state='aborted_unsealed' "
        f"WHERE id IN ({placeholders}) AND commit_state='prepared'",
        log_ids,
    )


def recover_stale_prepared_skill_logs(conn: Any, *, stale_after_seconds: int = 600) -> dict[str, Any]:
    """07-14 R8 P2-NEW-1 (contract #4): crash-recovery hook that terminalizes
    long-lived ``prepared`` skill-execution logs left behind when a producer
    died between Phase 1 (prepared log write) and Phase 2 (commit/abort).

    A ``prepared`` row whose producer never reached the Phase-2 terminalization
    is stuck -- it is retained as audit but, because it is neither 'committed'
    nor 'aborted_unsealed', it is excluded from learning (contract #5) yet never
    signals the failure. This hook marks any ``prepared`` row older than
    ``stale_after_seconds`` (default 10 min -- far longer than any producer tick)
    as ``commit_state='aborted'`` so diagnostics can report it and it never
    blocks the audit. Returns a summary dict for the diagnostics layer.
    """
    cur = conn.execute(
        """
        UPDATE skill_execution_logs
        SET commit_state='aborted'
        WHERE commit_state='prepared'
          AND created_at < datetime('now', ? )
        """,
        (f"-{int(stale_after_seconds)} seconds",),
    )
    affected = int(cur.rowcount or 0)
    return {
        "ok": True,
        "terminalized_prepared_to_aborted": affected,
        "stale_after_seconds": int(stale_after_seconds),
    }


def _allocate_attempt_id(conn: Any, batch_id: str) -> int:
    """07-15 R10-P2: allocate a per-batch, per-call-unique monotonic
    ``attempt_id`` for a producer tick.

    PRE-R10 (the defect): the allocation was a bare
    ``COALESCE(MAX(attempt_id),0)+1`` SELECT read OUTSIDE any transaction. Two
    concurrent producers (two connections -- e.g. the 5m + 15m crons firing
    together, or a retry overlap) could BOTH read the same MAX before either
    writes a prepared skill log -> both stamp the same ``attempt_id`` -> audit
    identity collision. The Phase-2 CAS includes the log ``id`` so there is no
    direct data overwrite (hence the reviewer marked this P2, not P0), but the
    per-call-unique-monotonic contract is broken.

    FIX: allocate from a DEDICATED per-batch attempt counter row
    (``_analysis_attempt_counter``) under an explicit ``BEGIN IMMEDIATE``. The
    RESERVED lock serializes concurrent allocators: the second blocks until the
    first commits, then re-reads the incremented counter -> each gets a DISTINCT
    monotonic integer. The counter is keyed by ``batch_id`` so each batch has
    its own 1,2,3,... sequence (preserving the R9-P2 per-batch monotonic
    contract the audit relies on). The transaction is a single upsert + read ->
    the lock is held only for that instant, well under the ``busy_timeout``.

    Returns the allocated attempt_id (a positive int; the first call for a
    batch_id returns 1). Idempotent in the sense that every call advances the
    counter and returns a fresh, distinct value.
    """
    # ATOMIC (R10-P2 fix): allocate from the dedicated per-batch counter row
    # under BEGIN IMMEDIATE. The RESERVED lock serializes concurrent allocators
    # (two connections racing on the same batch_id): the second blocks until the
    # first COMMITs, then re-reads the incremented counter -> distinct values.
    # The counter is keyed by batch_id, so each batch has its own 1,2,3,...
    # sequence (preserving the R9-P2 per-batch monotonic contract).
    #
    # The upsert+read is a single transaction; the lock is held only for that
    # instant, well under the busy_timeout. We read back via the SAME atomic
    # increment rather than ``last_insert_rowid()`` so the value is correct
    # whether the row was INSERTed or UPDATEd (ON CONFLICT path).
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO _analysis_attempt_counter(batch_id, next_attempt) "
            "VALUES(?, 1) "
            "ON CONFLICT(batch_id) DO UPDATE SET "
            "next_attempt = _analysis_attempt_counter.next_attempt + 1",
            (batch_id,),
        )
        row = conn.execute(
            "SELECT next_attempt FROM _analysis_attempt_counter WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        attempt_id = int(row["next_attempt"])
        conn.commit()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return attempt_id


def enqueue_market_analysis(
    *,
    analysis_time_utc: int | None = None,
    mode: str = "scheduled",
    primary_interval: str = "5m",
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    # P1-2 R4: Readiness gate — defer analysis when market-data warmup hasn't
    # reached the "ready" state. The state machine has 3 explicit states:
    #   "pending" — warmup started but not finished (defer)
    #   "ready"   — warmup succeeded AND not degraded (allow)
    #   "failed"  — warmup raised, returned degraded, or timed out (defer)
    # Only "ready" opens the gate. The next periodic warmup job can recover
    # from "failed" to "ready" on a subsequent successful run.
    # In tests/CLI (where start_all_services isn't called), state defaults to
    # "ready" so analysis proceeds normally.
    from plugins.crypto_guard.service_manager import is_warmup_complete, get_warmup_state
    if not is_warmup_complete():
        LOGGER.info(
            "enqueue_market_analysis: deferred — warmup state: %s (primary_interval=%s)",
            get_warmup_state(), primary_interval,
        )
        return {
            "ok": False,
            "deferred": True,
            "reason": "warmup_not_complete",
            "warmup_state": get_warmup_state(),
            "primary_interval": primary_interval,
            "analysis_time_utc": analysis_time_utc,
            "queued": 0,
        }

    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    # 07-15 R9-P1b: ``prepared`` is initialized BEFORE the outer lifecycle
    # ``try`` so the outer ``except`` (which covers Phase-1 mid-loop exceptions)
    # can reference it to abort the skill logs already created this tick. Pre-R9
    # it was defined INSIDE the try, so a Phase-1 crash left symbols 1..N-1
    # prepared logs stuck as 'prepared' (the outer try had only ``finally``, no
    # ``except``).
    prepared: list[dict[str, Any]] = []
    try:
        repo = CryptoGuardRepository(conn)
        analysis_time = latest_closed_close_time_ms(primary_interval, analysis_time_utc or utc_ms())
        # Hourly Report Accuracy: register a single analysis_batches row that
        # aggregates this scheduler tick across every enabled symbol. ga_decisions
        # reference this batch_id so the report renderer can gate on completion.
        batch_id = f"{primary_interval}:{analysis_time}"
        enabled_symbols = repo.active_analysis_symbols()
        priority = 6 if primary_interval == "5m" else 5
        # 07-15 R9-P2: per-producer-call monotonic attempt identity. A retry /
        # exception-recovery of the SAME batch_id gets a DIFFERENT attempt_id
        # (auditable), while the first call gets 1. Pre-R9 this was a hardcoded
        # literal ``1`` at every call, so an exception-recovery retry could not be
        # distinguished from the original attempt and a stale/aborted row could be
        # silently re-marked ``committed`` by an id-only UPDATE. The prepared skill
        # logs are stamped with this value in Phase 1 (save_skill_execution_log),
        # and the Phase-2 CAS-commit keys on it
        # (``commit_state='prepared' AND batch_id=? AND attempt_id=?``).
        # 07-15 R10-P2: the allocation is now ATOMIC. Pre-R10 it was a bare
        # ``COALESCE(MAX(attempt_id),0)+1`` SELECT OUTSIDE any transaction; two
        # concurrent producers (two connections) could both read the same MAX and
        # stamp the same attempt_id -> audit-identity collision. Now it allocates
        # from the dedicated ``_analysis_attempt_counter`` row under
        # ``BEGIN IMMEDIATE`` (see ``_allocate_attempt_id``), so each call gets a
        # DISTINCT per-batch monotonic integer even under concurrent producers.
        attempt_id = _allocate_attempt_id(conn, batch_id)

        # 07-13 R7 (P0-2): the batch-creation contract is ATOMIC. The SQLite
        # connection runs in autocommit mode (sqlite_db.py ``isolation_level=
        # None``), so without an explicit transaction the batch row, every job
        # INSERT, every batch_symbol_status row, and the seal stamp are N
        # independent transactions. A crash between them leaves a half-built
        # batch; a duplicate/foreign job inserted AFTER the seal (post-seal
        # pollution) is then claimed by ``claim_next_batch`` because the claim
        # only re-checks ``claim_ready_at`` (P0-2 item 2).
        #
        # Phase 1 (below, no transaction): build per-symbol snapshots. This is
        # read-only DB + in-memory computation (``build_market_state_snapshot``
        # reads candles/analysis_states; ``assess_health`` is read-only; the real
        # network fetch is the separate ``market_data_warmup`` cron job, NOT this
        # producer). Building snapshots outside the write lock keeps
        # ``BEGIN IMMEDIATE`` short-lived. The snapshot dicts are collected here
        # and ONLY persisted inside Phase 2's transaction (R8-C), so a seal
        # failure rolls the snapshots back with the batch -- no orphan.
        skipped_pending = 0
        for symbol in enabled_symbols:
            session_id = f"system:scheduled:{primary_interval}:{symbol}:{analysis_time}"
            pending = conn.execute(
                """
                SELECT 1
                FROM agent_jobs
                WHERE job_type='scheduled_market_analysis'
                  AND session_id=?
                  AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if pending:
                skipped_pending += 1
                # The symbol already has a pending/running job for this tick.
                # It is registered as 'pending' in Phase 2 so the exact-set
                # seal sees it (jobs == bss == enabled), but no NEW job is
                # inserted (P0-4: keep status 'pending' so _await_batch_completion
                # won't count it as completed). Recorded as a no-job prepared
                # entry so Phase 2 still registers its batch_symbol_status row
                # inside the atomic transaction.
                prepared.append({"symbol": symbol, "session_id": session_id,
                                 "snapshot": None, "snapshot_id": None,
                                 "module_sink": None, "skill_sink": None})
                continue
            # 07-14 R8 P2-2: collect this symbol's module_analysis_results
            # writes here (Phase 1) and persist them in Phase 2 inside the
            # BEGIN IMMEDIATE so a seal failure rolls them back with the batch.
            module_sink: list = []
            # 07-14 R8 P2-NEW-1: LAYERED skill-log lifecycle. Collect the
            # (log_id, feedback_payload) tuples here (Phase 1 writes the audit
            # rows commit_state='prepared' immediately). Phase 2 marks them
            # 'committed' + writes the deferred feedback on success (contract
            # #2), or 'aborted_unsealed' + ZERO feedback on a seal failure
            # (contract #3). The audit rows are retained in BOTH cases
            # (immutable audit); only the learning signal (feedback) is gated.
            skill_sink: list = []
            # 07-15 R10-P1 #2 (current-symbol partial skill logs): register the
            # skill_sink-bearing item into ``prepared`` BEFORE calling
            # ``build_market_state_snapshot``. ``_run_skill`` (runner.py:193-210)
            # appends each (log_id, feedback) tuple to the sink IMMEDIATELY as
            # the skills run sequentially inside the build. If a later skill or
            # the build itself RAISES after an earlier skill already wrote a
            # prepared log, that log is in the LOCAL ``skill_sink`` but -- pre-
            # R10 -- the item was only appended to ``prepared`` AFTER the build
            # returned, so the outer ``_abort_unsealed_skill_logs`` never saw it
            # and it survived as ``prepared`` until startup recovery. Pre-
            # registering the item (with placeholder snapshot/snapshot_id filled
            # in after the build) means a mid-build exception's partial logs are
            # already in the abort list. The R9-P1b test only covered PRIOR
            # COMPLETE symbols; this covers the CURRENT symbol's partial write.
            item: dict[str, Any] = {
                "symbol": symbol, "session_id": session_id,
                "snapshot": None, "snapshot_id": None,
                "module_sink": module_sink, "skill_sink": skill_sink,
            }
            prepared.append(item)
            # 07-15 R9-P2: pass the per-call monotonic attempt_id (not the
            # hardcoded literal ``1``) so the prepared logs are stamped with an
            # auditable identity that differs across retries.
            snapshot = build_market_state_snapshot(
                repo, symbol=symbol, analysis_time_utc=analysis_time,
                mode=mode, timeframes=timeframes,
                module_result_sink=module_sink,
                skill_log_sink=skill_sink, batch_id=batch_id, attempt_id=attempt_id,
            )
            # 07-15 R8-C (P1-2): DO NOT persist the snapshot here. The connection
            # is autocommit (sqlite_db.py ``isolation_level=None``), so a
            # ``save_market_snapshot`` call NOW would auto-commit the row BEFORE
            # Phase 2's ``BEGIN IMMEDIATE`` -- and a later seal-failure
            # ``ROLLBACK`` would revert the batch/jobs/status but LEAVE the
            # snapshot row behind (an orphan). The snapshot dict is built here
            # (read-only market-state construction, kept OUT of the write lock)
            # but persisted INSIDE Phase 2's transaction so it rolls back with
            # the batch. The ``snapshot_id`` is assigned after the persist call
            # in Phase 2 and threaded into the job payload.
            #
            # 07-14 R8 P2-2: the same applies to ``module_analysis_results``.
            # ``build_market_state_snapshot`` is NOT read-only -- it writes
            # ``module_analysis_results`` (per-TF modules + trend_stage_fusion +
            # market_regime). With the sink it collects those tuples instead of
            # persisting them in autocommit Phase 1, so they can be persisted
            # inside the Phase-2 transaction and roll back with the batch. The
            # plan's "read-only build" assumption was false; this makes the
            # orphan-snapshot guarantee extend to module_analysis_results too.
            item["snapshot"] = snapshot

        # Phase 2 (atomic): batch row + all jobs + all batch_symbol_status rows
        # + exact-set validation + seal stamp in ONE transaction. On any failure
        # the whole thing rolls back -- no partial batch is ever visible to
        # ``claim_next_batch`` (which only selects sealed batches, and a rolled-
        # back batch is never sealed). None of the repo methods called here
        # commit/rollback internally (verified: start_analysis_batch /
        # enqueue_job / mark_batch_symbol_completed / seal_analysis_batch only
        # issue INSERT/UPDATE), so the outer BEGIN/COMMIT owns the transaction.
        job_ids: list[int] = []
        sealed = False
        # ``seal_failed`` distinguishes a seal exact-set validation failure
        # (return ok=False, do NOT raise -- the cron caller logs and retries
        # next tick) from a genuine crash/constraint error (rollback + re-raise
        # so the caller sees the real exception). Both roll back identically.
        seal_failed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            repo.start_analysis_batch(
                batch_id=batch_id,
                primary_interval=primary_interval,
                analysis_time=analysis_time,
                enabled_symbols=enabled_symbols,
            )
            for item in prepared:
                symbol = item["symbol"]
                if item["snapshot"] is None:
                    # Skipped-pending symbol: register its batch_symbol_status
                    # row only (the job already exists from a prior tick).
                    repo.mark_batch_symbol_completed(batch_id=batch_id, symbol=symbol, status="pending")
                    continue
                # 07-15 R8-C (P1-2): persist the snapshot INSIDE the
                # ``BEGIN IMMEDIATE`` so a seal failure ``ROLLBACK`` reverts the
                # snapshot row WITH the batch (no orphan). Pre-R8-C this
                # ``save_market_snapshot`` call ran in Phase 1 (before the
                # transaction) and auto-committed, leaving an orphan snapshot on
                # every seal failure. ``save_market_snapshot`` issues raw
                # INSERT/UPDATE/UPDATE (no internal commit/rollback), so it
                # joins the active transaction cleanly.
                #
                # 07-14 R8 P2-2: FIRST persist this symbol's deferred
                # ``module_analysis_results`` rows (collected in Phase 1 via the
                # sink) so they are inside the same transaction AND exist before
                # ``save_market_snapshot`` calls ``link_module_results_to_snapshot``
                # (which keys the link by symbol + analysis_time). A seal-failure
                # ``ROLLBACK`` now reverts these rows with the snapshot/batch --
                # no orphan module_analysis_results. ``save_module_result`` is an
                # upsert (ON CONFLICT DO UPDATE) and issues raw INSERT, so it
                # joins the active transaction without committing.
                for (_sym, _tf, _at, _module, _result, _conf) in (item.get("module_sink") or []):
                    repo.save_module_result(_sym, _tf, _at, _module, _result, _conf)
                snapshot_id = repo.save_market_snapshot(item["snapshot"])
                item["snapshot_id"] = snapshot_id
                # 07-13 R7 (P0-1): the AUTHORITATIVE symbol field. The seal
                # (``seal_analysis_batch``) derives each job's symbol from
                # ``payload.symbol`` -- NOT from the ``<batch_id>:<symbol>``
                # prefix of ``session_id`` -- because the production session_id
                # format is ``system:scheduled:{interval}:{symbol}:{time}``
                # (above), which the legacy prefix-strip parser mangled into
                # the full ``system:scheduled:...`` string and never matched
                # the enabled set -> production batches never sealed
                # (reproduced in-memory: ``production_session_seals=False``).
                # ``payload.symbol`` is the single source of truth; session_id
                # + snapshot.symbol are cross-checked for identity consistency
                # by the seal.
                payload = {
                    "snapshot_id": item["snapshot_id"],
                    "snapshot": item["snapshot"],
                    "primary_interval": primary_interval,
                    "batch_id": batch_id,
                    "symbol": symbol,
                }
                job_id = repo.enqueue_job(
                    "scheduled_market_analysis",
                    priority,
                    "scheduler",
                    item["session_id"],
                    payload,
                )
                job_ids.append(job_id)
                # 07-13 R6-B (P0-1): register a 'pending' batch_symbol_status
                # row for every enabled symbol that received a job, so the
                # whole-batch seal can validate the exact set
                # (jobs == batch_symbol_status == enabled). The controller
                # flips this to 'completed'/'failed' on terminal.
                repo.mark_batch_symbol_completed(batch_id=batch_id, symbol=symbol, status="pending")
            # 07-13 R6-B (P0-1): seal the whole batch AFTER every enabled-symbol
            # job + batch_symbol_status row exists. seal_analysis_batch validates
            # the exact-set equality and stamps claim_ready_at/sealed_at only on
            # success. A malformed/incomplete/duplicate/cross-symbol set fails
            # closed (the batch stays non-claimable) instead of being partially
            # claimed. A failed seal is rolled back (no partial batch) and
            # reported as ok=False below.
            sealed = repo.seal_analysis_batch(batch_id)
            if not sealed:
                # Exact-set validation failed. Flag seal_failed so the except
                # branch knows this is a controlled rollback (return ok=False),
                # not a crash (re-raise). The batch stays unsealed + unclaimable.
                seal_failed = True
                raise RuntimeError("seal_analysis_batch exact-set validation failed")
            # 07-14 R8 P2-NEW-1 (contract #2): the Phase-2 seal SUCCEEDED. Now
            # terminalize the prepared skill logs to 'committed' and write the
            # DEFERRED feedback rows -- all INSIDE the still-open transaction so
            # the learning signal lands atomically with the batch. The prepared
            # audit rows were written in Phase 1 (autocommit); here they are
            # flipped to 'committed'. The feedback was NOT written in Phase 1
            # (it was collected into skill_sink), so this is the FIRST and only
            # write. ``latest_skill_result_refs`` only reads 'committed' rows, so
            # the orchestrator sees this tick's skill output only after a
            # successful seal (contract #5).
            _commit_skill_log_lifecycle(conn, repo, prepared, batch_id=batch_id, attempt_id=attempt_id)
            conn.execute("COMMIT")
        except Exception:
            # Roll back the entire batch creation on ANY failure (crash, seal
            # validation failure, constraint error). The batch stays unsealed
            # and unclaimable; the next tick rebuilds it from scratch.
            # ``ROLLBACK`` is issued unconditionally; if no transaction is open
            # (e.g. the BEGIN itself raised) sqlite3 raises OperationalError
            # "no transaction is active" which we swallow.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            sealed = False
            # 07-14 R8 P2-NEW-1 (contract #3): a seal failure / crash MUST leave
            # the prepared skill logs RETAINED as audit but MARKED
            # 'aborted_unsealed', and MUST write ZERO new feedback rows. The
            # prepared logs were autocommitted in Phase 1 (outside the rolled-
            # back transaction), so ROLLBACK cannot remove them -- instead they
            # are terminalized here in a fresh autocommit UPDATE. NO feedback is
            # written on the failure path (it was collected but never persisted),
            # so a failed tick never pollutes future learning/decisions. An
            # exception here is logged but never masks the original failure.
            try:
                _abort_unsealed_skill_logs(conn, prepared)
            except Exception:
                LOGGER.exception(
                    "enqueue_market_analysis: failed to mark prepared skill logs "
                    "aborted_unsealed for batch %s (audit rows may remain "
                    "'prepared' until recovery).", batch_id,
                )
            if seal_failed:
                # Controlled seal-failure rollback: return ok=False (logged +
                # retried next tick) rather than crashing the cron caller.
                LOGGER.warning(
                    "enqueue_market_analysis: batch %s seal failed (exact-set "
                    "validation) - rolled back, ok=False. enabled=%d queued=%d "
                    "skipped=%d", batch_id, len(enabled_symbols),
                    len(job_ids), skipped_pending,
                )
            else:
                # Genuine crash/constraint error: re-raise so the caller sees
                # the real cause (the cron wrapper logs it). R9-P1b: the OUTER
                # except below catches this re-raise and aborts the prepared
                # logs AGAIN (idempotent -- only 'prepared' rows are flipped),
                # so even a Phase-2 crash terminalizes any Phase-1 logs. It then
                # re-raises a second time to the caller.
                raise
        # 07-13 R7 (P0-1): a failed seal MUST NOT report ok=True. Pre-fix the
        # producer returned ok=True with sealed=False, so a downstream caller
        # that only checked ``ok`` treated an UNSEALED batch (whose job set did
        # not validate against the enabled set) as successfully enqueued -- and
        # ``claim_next_batch`` would never pick it up (claim_ready_at IS NULL),
        # silently dropping the whole tick. Now ok mirrors sealed: the tick is
        # ok only when the batch was actually sealed (claimable).
        return {
            "ok": bool(sealed),
            "primary_interval": primary_interval,
            "analysis_time_utc": analysis_time,
            "batch_id": batch_id,
            "queued": len(job_ids),
            "skipped_pending": skipped_pending,
            "priority": priority,
            "job_ids": job_ids,
            "sealed": sealed,
        }
    except Exception:
        # 07-15 R9-P1b: UNIFIED outer lifecycle except. Pre-R9 the outer
        # ``try`` had ONLY ``finally`` (no ``except``), so a Phase-1 mid-loop
        # symbol-N build failure propagated straight through to the caller,
        # BYPASSING ``_abort_unsealed_skill_logs`` -- symbols 1..N-1 prepared
        # logs survived as 'prepared' (the recovery hook only runs at startup).
        # This outer except now covers BOTH Phase 1 (the per-symbol build loop
        # above) and Phase 2 (via the inner except's re-raise). On ANY exception
        # that reaches here:
        #   1. ROLLBACK if a transaction is still open (a Phase-1 exception
        #      happens BEFORE ``BEGIN IMMEDIATE`` so there is no open txn -- the
        #      OperationalError is swallowed).
        #   2. Abort the prepared skill logs already created this tick (the
        #      symbols 1..N-1) so they are immediately terminalized to
        #      'aborted_unsealed' instead of staying stuck 'prepared'.
        #   3. Re-raise so the caller (the cron wrapper) logs the genuine
        #      crash. This is distinct from the controlled ``seal_failed``
        #      path (return ok=False) which never reaches here.
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        if prepared:
            try:
                _abort_unsealed_skill_logs(conn, prepared)
            except Exception:
                LOGGER.exception(
                    "enqueue_market_analysis: outer-except failed to abort "
                    "prepared skill logs (audit rows may remain 'prepared' "
                    "until recovery). prepared_count=%d", len(prepared),
                )
        raise
    finally:
        conn.close()


def enqueue_15m_analysis(*, analysis_time_utc: int | None = None, mode: str = "scheduled") -> dict[str, Any]:
    return enqueue_market_analysis(analysis_time_utc=analysis_time_utc, mode=mode, primary_interval="15m")


def market_data_warmup(*, analysis_time_utc: int | None = None) -> dict[str, Any]:
    """R5: Pre-analysis market-data warmup job.

    For each active symbol and each TF in ``cfg.market_data.required_samples``,
    check health; if not ready, run ``backfill_symbol_interval`` with the
    configured ``max_pages_per_run`` budget. Uses ``task_locks`` dedup so
    only one backfill per ``(symbol, interval)`` proceeds at a time.

    This job runs on cron (every 5 min) and once at startup before the
    scheduler loop begins. It must never raise — per-(symbol, TF) exceptions
    are caught and logged.

    P1-2 R4: This function transitions the warmup state machine:
      - On success (no degradation): ``_set_warmup_ready()``
      - On degraded result: ``_set_warmup_failed("degraded")``
      - On exception: ``_set_warmup_failed(str(exc))``
    This allows the periodic cron job to recover from "failed" to "ready"
    on a subsequent successful run.

    Returns a structured summary of per-TF status for diagnostics and the
    hourly report.
    """
    # P1-2 R4: Transition the warmup state machine. Wrap the entire body
    # so that any exception (even from load_config/initialize_database)
    # transitions to "failed" instead of leaving the gate in "pending".
    from plugins.crypto_guard.service_manager import _set_warmup_ready, _set_warmup_failed
    try:
        result = _market_data_warmup_impl(analysis_time_utc=analysis_time_utc)
        if result.get("degraded"):
            _set_warmup_failed("degraded")
        else:
            _set_warmup_ready()
        return result
    except Exception as exc:
        LOGGER.exception("market_data_warmup: unhandled exception — transitioning to failed")
        _set_warmup_failed(str(exc))
        return {
            "ok": False,
            "degraded": True,
            "error": str(exc),
            "symbols": {},
        }


def _market_data_warmup_impl(*, analysis_time_utc: int | None = None) -> dict[str, Any]:
    """Implementation of market_data_warmup — separated so the wrapper can
    catch top-level exceptions and transition the state machine.
    """
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        now = utc_ms()
        required_samples = cfg.market_data.get("required_samples", {})
        if not required_samples:
            return {"ok": True, "degraded": False, "symbols": {}, "reason": "no_required_samples"}

        symbols = repo.active_analysis_symbols()
        backfill_cfg = cfg.market_data.get("backfill", {})
        max_pages = int(backfill_cfg.get("max_pages_per_run", 50))
        backfill_enabled = bool(backfill_cfg.get("enabled", True))

        per_symbol: dict[str, dict[str, Any]] = {}
        any_degraded = False

        for symbol in symbols:
            per_tf: dict[str, Any] = {}
            for tf, required_count in required_samples.items():
                tf_required = int(required_count)
                # Use a TF-appropriate analysis_time: the latest closed candle
                # boundary for this interval at the current time.
                span = INTERVAL_MS.get(tf)
                if span is None:
                    per_tf[tf] = {"ready": False, "reason": "invalid_interval"}
                    any_degraded = True
                    continue
                tf_analysis_time = latest_closed_close_time_ms(tf, analysis_time_utc or now)

                try:
                    health = assess_health(
                        repo, symbol, tf,
                        analysis_time_utc=tf_analysis_time,
                        required_count=tf_required,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "market_data_warmup: assess_health failed symbol=%s interval=%s error=%s",
                        symbol, tf, exc,
                    )
                    per_tf[tf] = {
                        "ready": False, "reason": "assess_error",
                        "contiguous_tail_count": 0, "required_count": tf_required,
                        "gap_count": 0, "largest_gap_bars": 0,
                        "last_close_time": None, "error": str(exc),
                    }
                    any_degraded = True
                    continue

                if not health["ready"] and backfill_enabled:
                    LOGGER.info(
                        "market_data_warmup: backfilling symbol=%s interval=%s "
                        "contiguous=%d required=%d reason=%s",
                        symbol, tf, health["contiguous_tail_count"],
                        tf_required, health["reason"],
                    )
                    try:
                        backfill_symbol_interval(
                            repo, symbol, tf,
                            analysis_time_utc=tf_analysis_time,
                            required_count=tf_required,
                            max_pages=max_pages,
                        )
                        # Re-assess after backfill.
                        health = assess_health(
                            repo, symbol, tf,
                            analysis_time_utc=tf_analysis_time,
                            required_count=tf_required,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "market_data_warmup: backfill failed symbol=%s interval=%s error=%s",
                            symbol, tf, exc,
                        )
                        health = {
                            "ready": False, "reason": "backfill_error",
                            "contiguous_tail_count": health["contiguous_tail_count"],
                            "required_count": tf_required,
                            "gap_count": health.get("gap_count", 0),
                            "largest_gap_bars": health.get("largest_gap_bars", 0),
                            "last_close_time": health.get("last_close_time"),
                            "error": str(exc),
                        }

                per_tf[tf] = {
                    "ready": health["ready"],
                    "reason": health.get("reason", ""),
                    "contiguous_tail_count": health["contiguous_tail_count"],
                    "required_count": tf_required,
                    "gap_count": health.get("gap_count", 0),
                    "largest_gap_bars": health.get("largest_gap_bars", 0),
                    "last_close_time": health.get("last_close_time"),
                    "total_closed_count": health.get("total_closed_count", 0),
                }
                if not health["ready"]:
                    any_degraded = True

            per_symbol[symbol] = per_tf

        LOGGER.info(
            "market_data_warmup: symbols=%d degraded=%s",
            len(symbols), any_degraded,
        )

        return {
            "ok": True,
            "degraded": any_degraded,
            "symbols": per_symbol,
            "analysis_time_utc": now,
        }
    finally:
        conn.close()
