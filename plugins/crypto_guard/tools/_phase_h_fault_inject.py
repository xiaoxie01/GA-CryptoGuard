"""Phase H (07-05) fault injection verifier.

For each of the 7 Phase H diagnostic codes, seed a defective row into a fresh
isolated PostgreSQL scratch schema, run diagnose_report_accuracy, and assert
the corresponding code is reported. Then drop the scratch schema and report
PASS/FAIL for each fault.

PG cutover: each fault runs against a fresh isolated scratch schema
(``fault_<uuid>``) on the dedicated ``crypto_guard_test`` DB (app role
``crypto_guard_test_app``, never the ``postgres`` superuser) - NOT a temp SQLite
file. The scratch schema carries the full schema + seeds + ALL contract
markers (including ``llm_fair_scheduling_context_contract_v1``) so the 07-10
§10 fair-scheduling findings fire at their real severity (NOT demoted).
Fail-closed: if the scratch connection cannot be opened or seeded the error
propagates (NO SQLite fallback).

Usage:
    python -m plugins.crypto_guard.tools._phase_h_fault_inject
"""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# Bootstrap the dedicated test DB + app role BEFORE importing config/migrations
# (which resolve ``CRYPTO_GUARD_DATABASE_URL`` at import/call time). The admin
# password is read from ``CRYPTO_GUARD_DB_ADMIN_PASSWORD`` (test env only) and
# is NEVER written to source/YAML/logs/git. The role is least-privilege
# (NOSUPERUSER); the runtime never connects as the ``postgres`` superuser.
from plugins.crypto_guard.tests._pg_bootstrap import ensure_bootstrap

_APP_DSN = ensure_bootstrap()
os.environ["CRYPTO_GUARD_DATABASE_URL"] = _APP_DSN

from plugins.crypto_guard.config import load_config
from plugins.crypto_guard.storage.migrations import (
    SCHEMA_PATH,
    _seed_strategies,
    _seed_symbols,
)
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.diagnostics.report_diagnostics import (
    diagnose_report_accuracy,
    MISSING_CANDIDATE_ON_LLM_FAILURE,
    WITHHELD_WITHOUT_BLOCKERS,
    MISSING_ANALYSIS_CONTINUITY,
    OVERSIZED_FEATURE_PACK,
    CANDIDATE_EFFECTIVE_PLAN_MISMATCH,
    BATCH_TIME_HEALTH_MISMATCH,
    FAILED_JOBS_OUTSIDE_WINDOW,
    # Phase I (07-07): LLM retry + hourly accuracy repair diagnostic codes.
    LLM_CONFIG_ERROR_DETECTED,
    LLM_RETRY_EXHAUSTED,
    LLM_CIRCUIT_BREAKER_OPEN,
    DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN,
    EFFECTIVE_GRADE_EXCEEDS_HTF_CAP,
    SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS,
    HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH,
    # 07-10 P1-1 (design §10): eight formal Phase F fair-scheduling +
    # context-continuity diagnostic codes.
    LLM_FIRST_ATTEMPT_COVERAGE_LOW,
    LLM_SYMBOL_STARVATION,
    LLM_REPORT_COUNT_MISMATCH,
    LLM_SUCCESS_MISSING_ATTEMPT_METADATA,
    LLM_CONTINUITY_NOT_INCLUDED,
    LLM_TIMEOUT_CONFIG_OUT_OF_RANGE,
    LLM_BATCH_DEGRADED_REPORTED_HEALTHY,
    LLM_REPAIR_COUNTED_AS_PROVIDER_CALL,
)
from plugins.crypto_guard.diagnostics.state_consistency import (
    diagnose_state_consistency,
    WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY,
    WATCH_RECHECK_RISK_SHAPE_CONTRACT_MARKER_KEY,
    WATCH_REVIEW_PAYLOAD_SERIALIZATION_CONTRACT_MARKER_KEY,
    WATCH_RECHECK_FUNNEL_CONTRACT_MARKER_KEY,
)

# 08-06 (release-blocker rework): issue code emitted by
# diagnose_state_consistency when the watch->order bridge contract marker row
# is missing. Kept as an explicit constant (state_consistency emits the type
# as a literal string, unlike report_diagnostics which exports code constants).
WATCH_ORDER_BRIDGE_CONTRACT_MARKER_MISSING = (
    "watch_order_bridge_contract_marker_missing"
)

# 08-08 Step 7: issue codes emitted by diagnose_state_consistency when each
# watch-recheck contract marker row is missing (fail-closed). Kept as explicit
# constants (state_consistency emits the types as literal strings).
WATCH_RECHECK_RISK_SHAPE_CONTRACT_MARKER_MISSING = (
    "watch_recheck_risk_shape_contract_marker_missing"
)
WATCH_REVIEW_PAYLOAD_SERIALIZATION_CONTRACT_MARKER_MISSING = (
    "watch_review_payload_serialization_contract_marker_missing"
)
WATCH_RECHECK_FUNNEL_CONTRACT_MARKER_MISSING = (
    "watch_recheck_funnel_contract_marker_missing"
)

# 08-12 P1 (Codex P1-2): issue code emitted by diagnose_state_consistency when
# a daily_review:<date> delivery's outcome is UNKNOWN (terminal failed with a
# send/finalize/crash reason code, or a long-stale 'sending' row).
DAILY_REVIEW_DELIVERY_OUTCOME_UNKNOWN = (
    "daily_review_delivery_outcome_unknown"
)


# ---------------------------------------------------------------------------
# Scratch-schema isolation (mirrors backtest/historical_replay._scratch_replay_repo)
# ---------------------------------------------------------------------------


def _ensure_all_contract_markers(cur: psycopg.Cursor) -> None:
    """Write every contract marker into the scratch schema.

    The 07-10 §10 fair-scheduling fault seeds require
    ``llm_fair_scheduling_context_contract_v1`` to be present or the findings
    are demoted to ``legacy_info`` (wrong severity). Production's
    ``initialize_database`` writes all markers after schema+seeds; we replicate
    the full set here so every fault fires at its real severity. Marker rows
    land in ``_migration_state`` (created by the schema SQL) inside the scratch
    schema via ``search_path``.
    """
    from plugins.crypto_guard.storage.migrations import (
        _ensure_btc9_trade_gate_contract_marker,
        _ensure_entry_confirmation_lifecycle_contract_marker,
        _ensure_execution_funnel_report_contract_marker,
        _ensure_hourly_decision_context_continuity_contract_marker,
        _ensure_hourly_market_semantic_accuracy_contract_marker,
        _ensure_hourly_report_accuracy_r4_contract_marker,
        _ensure_llm_failed_direction_fail_closed_marker,
        _ensure_llm_fair_scheduling_context_contract_marker,
        _ensure_llm_provider_timeout_envelope_contract_marker,
        _ensure_llm_risk_context_isolation_contract_marker,
        _ensure_llm_risk_proposal_contract_marker,
        _ensure_llm_schema_breaker_preset_integrity_marker,
        _ensure_market_data_contract_marker,
        _ensure_profit_protection_cutoff_marker,
        _ensure_risk_adjustment_verifier_contract_marker,
        _ensure_stop_loss_adjustment_dedup_marker,
        _ensure_watch_order_bridge_contract_marker,
        _ensure_watch_recheck_risk_shape_contract_marker,
        _ensure_watch_review_payload_serialization_contract_marker,
        _ensure_watch_recheck_funnel_contract_marker,
    )

    _ensure_profit_protection_cutoff_marker(cur)
    _ensure_hourly_report_accuracy_r4_contract_marker(cur)
    _ensure_btc9_trade_gate_contract_marker(cur)
    _ensure_market_data_contract_marker(cur)
    _ensure_hourly_market_semantic_accuracy_contract_marker(cur)
    _ensure_hourly_decision_context_continuity_contract_marker(cur)
    _ensure_llm_fair_scheduling_context_contract_marker(cur)
    # 07-22 Codex P1-1: timeout-envelope cutoff independent of fair-scheduling.
    # Without this marker, llm_timeout_config_out_of_range is fail-closed missing
    # or demoted, so the Phase H timeout fault seed cannot fire at error severity.
    _ensure_llm_provider_timeout_envelope_contract_marker(cur)
    # 07-27 Phase-2 C: llm_failed_direction fail-closed marker (full-set
    # mirror of _phase_i_fresh_verify; state_consistency is not run here).
    _ensure_llm_failed_direction_fail_closed_marker(cur)
    # 07-31 P1-4: schema/breaker/preset-integrity marker. Without it the two
    # LLM diagnostics (_check_llm_failure_rate_high / _check_llm_circuit_breaker_open)
    # fail-closed skip themselves and the breaker-open fault seed cannot fire.
    _ensure_llm_schema_breaker_preset_integrity_marker(cur)
    # 08-03 Codex P2-4 (terminal-review rework): execution-funnel report-contract
    # marker. Without it the Phase H scratch-schema report run emits a spurious
    # execution_funnel_report_contract_marker_missing finding (Phase I's
    # _ensure_all_contract_markers already seeds it; Phase H now mirrors).
    _ensure_execution_funnel_report_contract_marker(cur)
    # 08-06 P2 (release-blocker rework): watch -> order bridge contract marker
    # (full-set mirror of _phase_i_fresh_verify; state_consistency is not run
    # here, but the fresh-schema marker set must stay in lockstep so Phase I's
    # marker-missing checks fire at their real severity).
    _ensure_watch_order_bridge_contract_marker(cur)
    # 08-08 Step 7: three watch-recheck contract markers (full-set mirror of
    # _phase_i_fresh_verify; the fresh-schema marker set must stay in lockstep
    # so the marker-missing checks fire at their real severity).
    _ensure_watch_recheck_risk_shape_contract_marker(cur)
    _ensure_watch_review_payload_serialization_contract_marker(cur)
    _ensure_watch_recheck_funnel_contract_marker(cur)
    # 08-10 Step 3+9: entry-confirmation lifecycle + LLM risk-governance markers
    # (full-set mirror of _phase_i_fresh_verify; the fresh-schema marker set
    # must stay in lockstep so the marker-missing checks fire at their real
    # severity).
    _ensure_entry_confirmation_lifecycle_contract_marker(cur)
    _ensure_llm_risk_proposal_contract_marker(cur)
    _ensure_risk_adjustment_verifier_contract_marker(cur)
    _ensure_llm_risk_context_isolation_contract_marker(cur)
    _ensure_stop_loss_adjustment_dedup_marker(cur)


_FAULT_SCHEMA_RE = re.compile(r"^fault_[0-9a-f]{32}$")

_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


def _safe_error_summary(exc: BaseException) -> str:
    """Short failure summary safe for external surfaces.

    Mirrors ``backtest/historical_replay._safe_error_summary``. Never
    renders ``str(exc)``/``repr(exc)``: exception messages can embed the
    DSN, credentials, or arbitrary database text. Only the exception type
    name plus an optional strictly-validated SQLSTATE
    (``^[0-9A-Z]{5}$``) is allowed. The ``sqlstate`` read is fail-safe: a
    raising getter is ignored and never replaces the underlying error.
    """
    summary = type(exc).__name__
    try:
        sqlstate = getattr(exc, "sqlstate", None)
    except Exception:
        sqlstate = None
    if isinstance(sqlstate, str) and _SQLSTATE_RE.match(sqlstate):
        summary += f" (SQLSTATE {sqlstate})"
    return summary


def _drop_fault_schema(schema_name: str) -> str | None:
    """Drop the scratch schema via a dedicated connection; never silent.

    Mirrors ``backtest/historical_replay._drop_scratch_schema``: runs on a
    FRESH connection (autocommit) so it never depends on the fault-repo
    connection's transaction state. The schema name must match the
    ``fault_<32hex>`` scratch contract before any DROP. Returns ``None`` on
    success or a short human-readable failure description (never the DSN or
    any credential) when cleanup itself failed.
    """
    if not _FAULT_SCHEMA_RE.match(schema_name):
        raise RuntimeError(f"refusing to drop non-scratch schema {schema_name!r}")
    conn: psycopg.Connection | None = None
    try:
        conn = psycopg.connect(_APP_DSN, row_factory=dict_row, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace WHERE nspname = %s",
                (schema_name,),
            )
            row = cur.fetchone()
            if row is None or row["count"] != 0:
                return "schema still exists after DROP"
        return None
    except Exception as exc:
        return f"cleanup failure: {_safe_error_summary(exc)}"
    finally:
        # ``conn.close`` may itself fail (e.g. the server died mid-cleanup);
        # it must never replace the returned failure string or the in-flight
        # body exception, so guard it the same way the body guard does.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _attach_cleanup_failure(body_exc: BaseException, failure: str) -> None:
    """Attach a cleanup-failure detail to the in-flight body exception.

    Mirrors ``backtest/historical_replay._attach_cleanup_failure``.
    ``BaseException.add_note`` requires Python 3.11+ (PEP 678); on older
    runtimes fall back to chaining a ``RuntimeError`` via ``__cause__`` so the
    cleanup failure is never lost.
    """
    message = f"scratch schema cleanup failed: {failure}"
    add_note = getattr(body_exc, "add_note", None)
    if callable(add_note):
        add_note(message)
    else:
        body_exc.__cause__ = RuntimeError(message)


@contextmanager
def _scratch_fault_repo() -> Iterator[CryptoGuardRepository]:
    """Yield a repo bound to a fresh isolated scratch schema; drop it on exit.

    A dedicated (non-pooled) ``psycopg.connect`` to the test DSN with
    ``search_path`` routed to a unique scratch schema so writes never touch the
    ``public`` schema (and the production pool is never opened). The schema SQL
    is schema-agnostic (no ``public.`` qualification), so ``CREATE TABLE``
    lands in ``search_path``. We deliberately do NOT call
    ``initialize_database`` (which targets the production pool / advisory
    lock / contract markers) - we apply schema + seeds + markers directly.
    """
    schema_name = f"fault_{uuid.uuid4().hex}".lower()
    conn = psycopg.connect(_APP_DSN, row_factory=dict_row, autocommit=False)
    body_exc: BaseException | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema_name}"')
        # Commit CREATE SCHEMA + SET search_path as its own transaction so the
        # schema exists before the DDL is applied into it.
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema_name}", public')
        # Apply schema + seeds + contract markers as one transaction so a
        # seed/marker failure drops nothing half-built (we DROP on exit).
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        cfg = load_config()
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            _seed_symbols(cur, cfg.symbols)
            _seed_strategies(cur, cfg.strategies)
            _ensure_all_contract_markers(cur)
        conn.commit()
        yield CryptoGuardRepository(conn)
    except BaseException as exc:
        body_exc = exc
        raise
    finally:
        # NEVER flip ``autocommit`` on a connection that may still be INTRANS
        # (psycopg raises ProgrammingError). Roll back if the connection is
        # still usable, close it, then drop the schema through a dedicated
        # cleanup connection that owns its own transaction state (mirrors
        # backtest/historical_replay._scratch_replay_repo).
        try:
            conn.rollback()
        except Exception:
            pass
        # ``conn.close`` may itself fail (e.g. the server died mid-run); it
        # must never skip the schema DROP, so guard it the same way.
        try:
            conn.close()
        except Exception:
            pass
        failure = _drop_fault_schema(schema_name)
        if failure is not None:
            if body_exc is None:
                raise RuntimeError(
                    f"scratch schema cleanup failed: {failure}"
                ) from None
            _attach_cleanup_failure(body_exc, failure)


def _insert_decision(conn, *, raw, decision="monitor_only", grade="D",
                    confidence=0.3, analysis_time=2_000_000_000_000,
                    llm_status="ok", plan_status="executable",
                    symbol="BTCUSDT", batch_id=None):
    """Insert one ga_decisions row with custom raw_decision_json."""
    # R14 P2-2 fix: ``raw_decision_json.analysis_time_utc`` must match
    # production shape (ISO string). Production's
    # ``controller_decision_from_legacy`` (decision_schema.py:116) writes
    # ``iso_from_ms(at_int)`` (ISO string) to BOTH the DB column
    # (``analysis_time_utc TEXT NOT NULL``, schema.sql:149) AND the
    # ``raw_decision_json`` via ``json.dumps(decision)``. Pre-R14 the
    # seed wrote an integer here, which was harmless (no diagnostic
    # reads ``analysis_time_utc`` from ``raw_decision_json``) but
    # inconsistent with production. The DB column at line 89 already
    # correctly receives the ISO string.
    iso_str = "2033-05-18T08:33:20Z"
    raw_full = {
        "symbol": symbol,
        "decision": decision,
        "signal_grade": grade,
        "confidence": confidence,
        "analysis_time_utc": iso_str,
        "analysis_time_iso": iso_str,
        "decision_type": "scheduled",
        "market_bias": "neutral",
        "trend_stage": "unknown",
        "llm_status": llm_status,
        "plan_status": plan_status,
        "plan_blockers": [],
        "has_trade_plan": False,
        "trade_plan": None,
        "candidate_trade_plan": None,
        "plan_source": "deterministic_sop",
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": False, "reasons": ["test"]},
        "skill_result_refs": {},
        "feishu_actions": [],
        "final_summary": "test",
        "summary": "test",
        "timeframe_context": {},
        "alignment": "unknown",
        "htf_conflict": False,
        "market_reason_codes": [],
        "raw_signal_grade": grade,
        "raw_score": confidence,
        "effective_signal_grade": grade,
        "effective_execution_confidence": confidence,
        "grade_adjustments": [],
        "opportunity_watch": None,
    }
    raw_full.update(raw)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ga_decisions ("
            "  symbol, analysis_time, analysis_time_utc, decision_type,"
            "  signal_grade, confidence, decision, market_bias, trend_stage,"
            "  skill_result_refs_json, evidence_json, counter_evidence_json,"
            "  risk_check_json, trade_plan_json, opportunity_watch_json,"
            "  feishu_actions_json, final_summary, raw_decision_json, batch_id"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                symbol, analysis_time, "2033-05-18T08:33:20Z", "scheduled",
                grade, confidence, decision, "neutral", "unknown",
                "{}", "[]", "[]",
                "{}", None, None,
                "[]", "test", json.dumps(raw_full), batch_id,
            ),
        )
    conn.commit()


def _run_one(name, fn):
    """Build a fresh scratch schema, run fn to seed fault, run diagnostics,
    return result dict. The scratch schema is dropped on exit."""
    with _scratch_fault_repo() as repo:
        conn = repo.conn
        fn(conn)
        result = diagnose_report_accuracy(repo)
        codes = [i["type"] for i in result["issues"]]
        return {"name": name, "codes": codes, "issues": result["issues"]}


def _run_one_state(name, fn):
    """Build a fresh scratch schema, run fn to seed fault, run
    ``diagnose_state_consistency`` (not ``diagnose_report_accuracy``), return
    result dict. Used for faults whose diagnostic lives in state_consistency
    (e.g. the 08-06 watch->order bridge contract marker). The scratch schema
    is dropped on exit."""
    with _scratch_fault_repo() as repo:
        conn = repo.conn
        fn(conn)
        result = diagnose_state_consistency(repo)
        codes = [i["type"] for i in result["issues"]]
        return {"name": name, "codes": codes, "issues": result["issues"]}


def fault_missing_candidate(conn):
    """Fault: llm_status=failed + plan_status=withheld but candidate_trade_plan missing.

    P1-8 (07-05 final review): the diagnostic only fires when LLM failed
    AND a candidate was expected (plan_status=withheld or executable).
    plan_status=no_plan is the legitimate no-candidate path (low-score /
    no-edge) and must NOT fire.
    """
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "plan_status": "withheld",
        "plan_blockers": ["llm_failure"],
        "candidate_trade_plan": None,
    })


def fault_no_plan_no_candidate_negative(conn):
    """Negative: llm_status=failed + plan_status=no_plan + no candidate.

    This must NOT trigger MISSING_CANDIDATE_ON_LLM_FAILURE. The function
    is invoked via _run_one but the assertion is the absence of the code.
    """
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "plan_status": "no_plan",
        "plan_blockers": [],
        "candidate_trade_plan": None,
        "decision": "no_edge",
        "signal_grade": "D",
    })


def fault_withheld_without_blockers(conn):
    """Fault: plan_status=withheld but plan_blockers empty."""
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "plan_status": "withheld",
        "plan_blockers": [],
        "candidate_trade_plan": {"side": "long", "entry": 100, "stop": 95},
    })


def fault_missing_continuity(conn):
    """Fault: decision missing analysis_continuity block entirely."""
    _insert_decision(conn, raw={
        "analysis_continuity": None,
    })


def fault_oversized_feature_pack(conn):
    """Fault: multi_timeframe_feature_pack > 24 KiB."""
    # 24 KiB = 24576 bytes. Build a pack that exceeds it.
    big_text = "x" * 25000
    big = {"1d": {"bias": big_text}}
    _insert_decision(conn, raw={
        "multi_timeframe_feature_pack": big,
    })


def fault_candidate_effective_mismatch(conn):
    """Fault: candidate vs effective plan disagree on side/entry/stop."""
    _insert_decision(conn, raw={
        "candidate_trade_plan": {"side": "long", "entry": 100, "stop": 95},
        "trade_plan": {"side": "short", "entry": 200, "stop": 210},
        "has_trade_plan": True,
        "plan_status": "executable",
    })


def fault_batch_time_health_mismatch(conn):
    """Fault: success batch has unhealthy symbol at batch.analysis_time."""
    batch_id = "BATCH1"
    analysis_time = 2_000_000_000_000
    # R12 P2-2 fix: use ``primary_interval="15m"`` (matches
    # scheduler.yaml:analyze_market_15m) and seed all 5 required TFs in
    # the health dict, with the fault on one TF (``1h`` not ready). Pre-R12
    # used ``primary_interval="1h"`` with only ``1h`` in the health dict.
    # ``_required_timeframes_for_batch("1h")`` returned the fallback
    # (5-TF set), and the diagnostic fired ``missing_required_tf`` instead
    # of ``not_ready`` — so the ``not_ready`` branch was never exercised
    # by fault injection.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches (batch_id, primary_interval, "
            "analysis_time, status, enabled_symbols_json) "
            "VALUES (%s, %s, %s, %s, %s)",
            (batch_id, "15m", analysis_time, "success",
             json.dumps(["BTCUSDT"])),
        )
        # Insert a batch_symbol_status row marked completed
        cur.execute(
            "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
            "VALUES (%s, %s, %s)",
            (batch_id, "BTCUSDT", "completed"),
        )
        # P1-7 (07-05 final review): seed the PRODUCTION shape
        # ``data_quality.health[tf]`` — market_state_builder persists this
        # structure (see market_state_builder.py:_data_quality). The previous
        # fault seeded ``timeframes`` which does not exist in production,
        # so the diagnostic (which read ``timeframes``) appeared to catch
        # the fault but actually never fired on real data.
        # R12 P2-2: seed all 5 required TFs. 1h has ``ready=False`` to
        # exercise the ``not_ready`` branch of the diagnostic.
        cur.execute(
            "INSERT INTO market_snapshots "
            "(symbol, analysis_time, mode, snapshot_json, data_quality_json) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ("BTCUSDT", analysis_time, "scheduled", "{}",
             json.dumps({"health": {
                 "1d": {"ready": True, "last_close_time": analysis_time - 86_400_000},
                 "4h": {"ready": True, "last_close_time": analysis_time - 14_400_000},
                 "1h": {"ready": False, "last_close_time": 0},
                 "15m": {"ready": True, "last_close_time": analysis_time - 900_000},
                 "5m": {"ready": True, "last_close_time": analysis_time - 300_000},
             }})),
        )
        snap_id = cur.fetchone()["id"]
    # Insert a ga_decisions row referencing the snapshot via snapshot_id
    _insert_decision(conn, raw={
        "batch_id": batch_id,
    }, batch_id=batch_id, decision="no_edge", grade="C")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ga_decisions SET snapshot_id = %s WHERE batch_id = %s",
            (snap_id, batch_id),
        )
    conn.commit()


def fault_batch_time_health_stale_but_ready(conn):
    """R5 P1-1 fault: ``ready=True`` but ``last_close_time`` is stale.

    Previously the diagnostic only checked ``last_close <= batch_at`` and
    missed snapshots whose ``last_close_time`` was 12h stale. The R5 fix
    requires ``last_close >= batch_at - 2 * INTERVAL_MS[tf]``. For a 1h
    batch, 2 * 3_600_000 = 7_200_000 ms = 2h tolerance. A 12h-stale
    snapshot must now fire ``BATCH_TIME_HEALTH_MISMATCH``.

    R12 P2-2 fix: use ``primary_interval="15m"`` (matches
    scheduler.yaml:analyze_market_15m) and seed all 5 required TFs in
    the health dict, with the stale fault on ``1h``. Pre-R12 used
    ``primary_interval="1h"`` with only ``1h`` in the health dict.
    ``_required_timeframes_for_batch("1h")`` returned the fallback
    (5-TF set), and the diagnostic fired ``missing_required_tf`` instead
    of ``stale_by_X_bars`` — so the stale lower bound branch was never
    exercised by fault injection.
    """
    batch_id = "BATCH_STALE"
    analysis_time = 2_000_000_000_000
    # 1h interval = 3_600_000 ms. 12h stale = batch_at - 12 * 3_600_000.
    stale_close = analysis_time - 12 * 3_600_000
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches (batch_id, primary_interval, "
            "analysis_time, status, enabled_symbols_json) "
            "VALUES (%s, %s, %s, %s, %s)",
            (batch_id, "15m", analysis_time, "success",
             json.dumps(["BTCUSDT"])),
        )
        cur.execute(
            "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
            "VALUES (%s, %s, %s)",
            (batch_id, "BTCUSDT", "completed"),
        )
        # ``ready=True`` (would have passed old check) but last_close is 12h
        # stale, which must be caught by the R5 stale lower bound.
        # R12 P2-2: seed all 5 required TFs. Only ``1h`` is stale; the
        # others are fresh so the only reason the diagnostic fires is the
        # stale lower bound on ``1h``.
        cur.execute(
            "INSERT INTO market_snapshots "
            "(symbol, analysis_time, mode, snapshot_json, data_quality_json) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ("BTCUSDT", analysis_time, "scheduled", "{}",
             json.dumps({"health": {
                 "1d": {"ready": True, "last_close_time": analysis_time - 86_400_000},
                 "4h": {"ready": True, "last_close_time": analysis_time - 14_400_000},
                 "1h": {"ready": True, "last_close_time": stale_close},
                 "15m": {"ready": True, "last_close_time": analysis_time - 900_000},
                 "5m": {"ready": True, "last_close_time": analysis_time - 300_000},
             }})),
        )
        snap_id = cur.fetchone()["id"]
    _insert_decision(conn, raw={
        "batch_id": batch_id,
    }, batch_id=batch_id, decision="no_edge", grade="C")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ga_decisions SET snapshot_id = %s WHERE batch_id = %s",
            (snap_id, batch_id),
        )
    conn.commit()


def fault_failed_jobs_outside_window(conn):
    """Fault: failed batch older than 7 days — should be legacy_info."""
    # Set started_at to 8 days ago. PG: ``NOW() - INTERVAL '8 days'``.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches ("
            "  batch_id, primary_interval, analysis_time, status, started_at,"
            "  enabled_symbols_json, completed_symbols_json, failed_symbols_json"
            ") VALUES (%s, %s, %s, %s, (NOW() - INTERVAL '8 days'), %s, %s, %s)",
            ("BATCH_OLD", "1h", 2_000_000_000_000, "failed",
             json.dumps(["BTCUSDT"]), json.dumps([]), json.dumps(["BTCUSDT"])),
        )
    conn.commit()


# ── Phase I (07-07): LLM retry + hourly accuracy repair fault seeds ──────────

def _recent_analysis_time_ms() -> int:
    """Return a current-contract analysis time visible to 24h diagnostics.

    Use a one-second forward guard rather than ``now - 1h``.  The latter can
    fall before a freshly-created contract marker (and crosses the UTC date
    boundary during the first UTC hour), incorrectly demoting positive fault
    seeds to historical ``legacy_info``.
    """
    import time as _time
    return int(_time.time() * 1000) + 1_000


def fault_llm_config_error_http_422(conn):
    """Fault: ga_decisions row with ``llm_error_category=llm_config_error``.

    Per PRD AC1/AC18, HTTP 422 invalid_model_error → llm_config_error,
    non-retryable, breaker opens. Diagnostic must catch this in latest 24h.
    """
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "llm_error_category": "llm_config_error",
        "llm_error_stage": "call",
        "llm_error": "HTTP 422 invalid_model_error: model not found: xopglm52",
        "llm_attempt_count": 1,
        "llm_fallback_reason": "non_retryable_error",
        "llm_config_name": "native_claude_config",
        "llm_model": "xopglm52",
    }, analysis_time=at_ms, decision="no_edge", grade="D")


def fault_llm_retry_exhausted(conn):
    """Fault: ga_decisions row with ``llm_fallback_reason=retry_exhausted``."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "llm_error_category": "llm_empty_response",
        "llm_error_stage": "retry_exhausted",
        "llm_error": "empty response after 3 attempts",
        "llm_attempt_count": 3,
        "llm_fallback_reason": "retry_exhausted",
    }, analysis_time=at_ms, decision="no_edge", grade="D")


def fault_llm_circuit_breaker_open(conn):
    """Fault: latest batch with ``summary_json.llm_health.breaker_state=open``."""
    at_ms = _recent_analysis_time_ms()
    summary = {
        "llm_health": {
            "total_attempts": 10,
            "successful": 0,
            "failed": 10,
            "skipped_by_breaker": 0,
            "dominant_error_category": "llm_transport_error",
            "breaker_state": "open",
            "by_category": {"llm_transport_error": 10},
            "total_retries": 3,
        },
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches ("
            "  batch_id, primary_interval, analysis_time, status, started_at,"
            "  summary_json"
            ") VALUES (%s, %s, %s, %s, NOW(), %s)",
            ("BATCH_BREAKER_OPEN", "1h", at_ms, "failed",
             json.dumps(summary)),
        )
    conn.commit()


def fault_deterministic_candidate_reported_as_trade_plan(conn):
    """Fault (positive, the genuine contradiction): a ``ga_decisions`` row
    where an executable plan WAS persisted (``has_trade_plan=True``) yet
    ``plan_execution_state != confirmed`` — the row can be rendered/executed
    as a trade plan while the lifecycle state says it was never confirmed.

    Per design §11.1 + R6-E P1-3 #5 (AC14), the diagnostic reads
    ``raw_decision_json`` fields directly — it does NOT parse rendered report
    text. The seed plants ``has_trade_plan=True`` + ``unconfirmed`` (a valid
    candidate present alongside a persisted executable plan) — the diagnostic
    MUST flag this contradiction.
    """
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "llm_error_category": "llm_empty_response",
        "llm_fallback_reason": "retry_exhausted",
        "has_trade_plan": True,
        "trade_plan": {
            "side": "long", "entry": 100, "stop": 95,
            "trigger_price": 100, "trigger_side": "long",
        },
        "candidate_trade_plan": {
            "side": "long", "entry": 100, "stop": 95,
            "trigger_price": 100, "trigger_side": "long",
        },
        "plan_origin": "deterministic_fallback",
        "plan_execution_state": "unconfirmed",
        "plan_status": "withheld",
    }, analysis_time=at_ms, decision="no_edge", grade="C")


def fault_deterministic_candidate_reported_as_trade_plan_negative(conn):
    """Negative: a valid fail-closed deterministic candidate
    (``candidate_trade_plan`` present, ``has_trade_plan=False``,
    ``plan_execution_state=unconfirmed``). Per R6-E P1-3 #5 / AC14 this is
    the legitimate fail-closed path — the renderer already labels it
    "规则候选计划已生成，LLM 未确认，禁止执行" — and the diagnostic MUST NOT
    fire (the pre-R6-E logic wrongly counted this as a defect, AC14 noise).
    """
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "failed",
        "llm_error_category": "llm_empty_response",
        "llm_fallback_reason": "retry_exhausted",
        "has_trade_plan": False,
        "trade_plan": None,
        "candidate_trade_plan": {
            "side": "long", "entry": 100, "stop": 95,
            "trigger_price": 100, "trigger_side": "long",
        },
        "plan_origin": "deterministic_fallback",
        "plan_execution_state": "unconfirmed",
        "plan_status": "withheld",
    }, analysis_time=at_ms, decision="no_edge", grade="C")


def fault_raw_grade_exceeds_htf_cap(conn):
    """Fault: *effective* grade=S while HTF Cap 1 allows max B.

    07-22 Phase-2 contract correction: raw_signal_grade is a pre-gate audit
    value and may exceed the cap. The diagnostic now fires only when the
    *effective* / canonical grade exceeds the cap. This seed plants
    effective=S (and column grade=S) with Cap 1 conditions so the active
    code ``effective_grade_exceeds_htf_cap`` is caught.
    """
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "signal_grade": "S",
        "raw_signal_grade": "S",
        "effective_signal_grade": "S",
        "market_bias": "bullish",
        "timeframe_context": {
            "1d": {"bias": "bearish"},
            "4h": {"bias": "bearish"},
            "1h": {"bias": "bullish"},
            "15m": {"bias": "bullish"},
            "5m": {"bias": "bullish"},
        },
    }, analysis_time=at_ms, decision="long", grade="S")


def fault_success_batch_missing_completed_symbols(conn):
    """Fault: ``status=success`` batch with ``completed_symbols_json=[]``.

    Per design §10.1, this is the write-link gap. The diagnostic reads the
    raw column (not the read-time compensation). The seed plants a success
    batch with empty raw column but a live completed entry in
    ``batch_symbol_status`` to prove the column is stale.
    """
    at_ms = _recent_analysis_time_ms()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches ("
            "  batch_id, primary_interval, analysis_time, status, started_at,"
            "  enabled_symbols_json, completed_symbols_json, failed_symbols_json"
            ") VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s)",
            ("BATCH_EMPTY_COMPLETED", "1h", at_ms, "success",
             json.dumps(["BTCUSDT", "ETHUSDT"]),
             json.dumps([]),  # ← raw column empty — the defect
             json.dumps([])),
        )
        # Plant a live completed entry to prove the column is stale (not
        # legitimately empty).
        cur.execute(
            "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
            "VALUES (%s, %s, %s)",
            ("BATCH_EMPTY_COMPLETED", "BTCUSDT", "completed"),
        )
    conn.commit()


def fault_hourly_report_used_partial_running_batch(conn):
    """Fault: latest batch ``status=running`` AND a recent hourly_summary
    alert in ``alert_outbox``.

    Per design §9.4, the hourly report must select the latest *complete*
    batch. Rendering against a running batch + recent alert_outbox row
    violates the contract.
    """
    at_ms = _recent_analysis_time_ms()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches ("
            "  batch_id, primary_interval, analysis_time, status, started_at"
            ") VALUES (%s, %s, %s, %s, NOW())",
            ("BATCH_RUNNING", "1h", at_ms, "running"),
        )
        # Plant a recent hourly_summary alert (within last hour).
        cur.execute(
            "INSERT INTO alert_outbox ("
            "  alert_type, symbol, priority, payload_json, status, created_at"
            ") VALUES (%s, %s, %s, %s, %s, NOW())",
            ("hourly_summary", None, 5,
             json.dumps({"fallback_text": "hourly report test"}), "sent"),
        )
    conn.commit()


# ── 07-10 P1-1 (design §10): eight Phase F fair-scheduling fault seeds ───────
#
# Each seed plants the EXACT production defect into a fresh scratch schema
# (whose init writes the ``llm_fair_scheduling_context_contract_v1`` marker,
# so findings fire at their real severity - NOT demoted). Each has a paired
# negative control proving the check does NOT fire on the healthy shape (so
# a future widening cannot silently green it).
#
# Batch-row helper: the batch-level checks (coverage / starvation / mismatch /
# degraded) read ``analysis_batches`` rows. ``enabled_symbols_json`` is the
# authoritative denominator; the §8 envelope on each ga_decisions row is the
# authoritative per-symbol outcome. Both mirror the production write path
# (``finish_analysis_batch`` + ``create_ga_decision``).

def _insert_batch(conn, *, batch_id, enabled, analysis_time=None,
                  status="success", summary=None):
    """Insert an ``analysis_batches`` row mirroring production's
    ``finish_analysis_batch`` shape (enabled/completed/failed lists +
    summary_json). Returns ``batch_id`` for chaining."""
    if analysis_time is None:
        analysis_time = _recent_analysis_time_ms()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_batches ("
            "  batch_id, primary_interval, analysis_time, status, started_at,"
            "  enabled_symbols_json, completed_symbols_json, failed_symbols_json,"
            "  summary_json"
            ") VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s)",
            (batch_id, "15m", analysis_time, status,
             json.dumps(list(enabled)),
             json.dumps(list(enabled)),
             json.dumps([]),
             json.dumps(summary) if summary is not None else None),
        )
    return batch_id


def _insert_batch_decision(conn, *, batch_id, symbol, analysis_time=None,
                           **envelope):
    """Insert a ga_decisions row carrying the given §8 envelope at the top
    level of ``raw_decision_json`` (mirrors decision_schema's
    ``controller_decision_from_legacy`` surfacing). The envelope fields are
    merged on top of the default raw shape from ``_insert_decision``."""
    if analysis_time is None:
        analysis_time = _recent_analysis_time_ms()
    _insert_decision(
        conn,
        raw=dict(envelope),
        analysis_time=analysis_time,
        symbol=symbol,
        batch_id=batch_id,
    )


def fault_llm_first_attempt_coverage_low(conn):
    """Fault: a ``success`` batch with 3 enabled symbols but only 1 made a
    physical provider call. The 2 unattempted symbols have rows with
    ``llm_status="failed"`` AND ``llm_provider_call_count=0`` and NO
    budget/breaker terminal reason - bare failures that fall into NONE of
    production's accounting buckets (policy/breaker/budget skip exclude
    status="failed"; worker_failed requires no row). This is the silent-
    coverage gap - the original starvation signature (symbols recorded as
    failed with no call and no allowed reason)."""
    at_ms = _recent_analysis_time_ms()
    bid = _insert_batch(conn, batch_id="BATCH_COV_LOW",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms)
    # Only BTC made a provider call.
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    # ETH + SOL: bare failures (pcc=0, status=failed, no budget/breaker
    # reason) -> unaccounted residual (the defect).
    for sym in ("ETHUSDT", "SOLUSDT"):
        _insert_batch_decision(conn, batch_id=bid, symbol=sym,
                               analysis_time=at_ms,
                               llm_status="failed", llm_attempt_count=0,
                               llm_provider_call_count=0, llm_latency_ms=0,
                               llm_prompt_bytes=None,
                               llm_continuity_included=None,
                               llm_terminal_reason=None,
                               llm_provider_timeout_ms=None)
    conn.commit()


def fault_llm_first_attempt_coverage_low_negative(conn):
    """Negative: 3 enabled, 1 attempted, but the 2 unattempted are ALL
    explained by allowed skips (1 policy + 1 breaker). The coverage gap is
    fully explained -> must NOT fire LLM_FIRST_ATTEMPT_COVERAGE_LOW. This
    proves the check distinguishes explained skips from the bare-failure
    residual."""
    at_ms = _recent_analysis_time_ms()
    bid = _insert_batch(conn, batch_id="BATCH_COV_OK",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms)
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    # ETH: policy skip (LLM disabled) - allowed.
    _insert_batch_decision(conn, batch_id=bid, symbol="ETHUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=0,
                           llm_provider_call_count=0, llm_latency_ms=0,
                           llm_prompt_bytes=None, llm_continuity_included=None,
                           llm_terminal_reason="llm_disabled",
                           llm_provider_timeout_ms=None)
    # SOL: breaker skip - allowed.
    _insert_batch_decision(conn, batch_id=bid, symbol="SOLUSDT",
                           analysis_time=at_ms,
                           llm_status="skipped", llm_attempt_count=0,
                           llm_provider_call_count=0, llm_latency_ms=0,
                           llm_prompt_bytes=None, llm_continuity_included=None,
                           llm_terminal_reason="breaker_skipped",
                           llm_provider_timeout_ms=None)
    conn.commit()


def _starvation_batch(conn, *, batch_id, at_ms, eligible=True):
    """Insert one eligible success batch with ZERO physical provider calls
    (all symbols policy-skipped with no terminal reason -> the starvation
    signature: eligible but structurally unreachable)."""
    enabled = ["BTCUSDT", "ETHUSDT"] if eligible else []
    _insert_batch(conn, batch_id=batch_id, enabled=enabled,
                  analysis_time=at_ms)
    for sym in enabled:
        _insert_batch_decision(conn, batch_id=batch_id, symbol=sym,
                               analysis_time=at_ms,
                               llm_status="ok", llm_attempt_count=0,
                               llm_provider_call_count=0, llm_latency_ms=0,
                               llm_prompt_bytes=None,
                               llm_continuity_included=None,
                               llm_terminal_reason=None,
                               llm_provider_timeout_ms=None)
    conn.commit()


def fault_llm_symbol_starvation(conn):
    """Fault: 3 CONSECUTIVE eligible batches, each with zero physical
    provider calls. The most-recent batch anchors the finding."""
    base = _recent_analysis_time_ms()
    # Insert oldest-first so started_at ordering (now) is stable; use distinct
    # analysis_time so each batch is independently identifiable.
    for i, bid in enumerate(("BATCH_STARVE_OLD", "BATCH_STARVE_MID",
                             "BATCH_STARVE_NEW")):
        _starvation_batch(conn, batch_id=bid, at_ms=base + i * 60_000)


def fault_llm_symbol_starvation_negative(conn):
    """Negative: 3 consecutive eligible batches but the MOST RECENT made a
    physical provider call -> the run is broken -> must NOT fire
    LLM_SYMBOL_STARVATION."""
    base = _recent_analysis_time_ms()
    _starvation_batch(conn, batch_id="BATCH_STARVE_N1", at_ms=base)
    _starvation_batch(conn, batch_id="BATCH_STARVE_N2", at_ms=base + 60_000)
    # Most recent: real provider call -> run broken.
    bid = _insert_batch(conn, batch_id="BATCH_STARVE_N3",
                       enabled=["BTCUSDT"], analysis_time=base + 120_000)
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=base + 120_000,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    conn.commit()


def fault_llm_report_count_mismatch(conn):
    """Fault: the persisted ``summary_json.llm_health`` claims
    ``llm_symbols_attempted=3`` but the real ga_decisions rows show only 1
    physical provider call. The rendered report would say "3/3 covered"
    while only 1 symbol was actually attempted -> false-healthy report."""
    at_ms = _recent_analysis_time_ms()
    summary = {
        "llm_health": {
            "expected_symbols": 3,
            "llm_symbols_attempted": 3,  # ← LIES: only 1 real call
            "llm_physical_provider_calls": 3,
            "llm_symbols_success": 3,
            "llm_symbols_failed": 0,
        }
    }
    bid = _insert_batch(conn, batch_id="BATCH_MISMATCH",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms, summary=summary)
    # Only BTC actually made a call.
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    _insert_batch_decision(conn, batch_id=bid, symbol="ETHUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=0,
                           llm_provider_call_count=0, llm_latency_ms=0,
                           llm_prompt_bytes=None, llm_continuity_included=None,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=None)
    _insert_batch_decision(conn, batch_id=bid, symbol="SOLUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=0,
                           llm_provider_call_count=0, llm_latency_ms=0,
                           llm_prompt_bytes=None, llm_continuity_included=None,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=None)
    conn.commit()


def fault_llm_report_count_mismatch_negative(conn):
    """Negative: persisted ``summary_json.llm_health`` AGREES with the real
    re-aggregation (both say 3 attempted, 3 calls) -> must NOT fire
    LLM_REPORT_COUNT_MISMATCH."""
    at_ms = _recent_analysis_time_ms()
    summary = {
        "llm_health": {
            "expected_symbols": 3,
            "llm_symbols_attempted": 3,
            "llm_physical_provider_calls": 3,
            "llm_symbols_success": 3,
            "llm_symbols_failed": 0,
        }
    }
    bid = _insert_batch(conn, batch_id="BATCH_MATCH",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms, summary=summary)
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        _insert_batch_decision(conn, batch_id=bid, symbol=sym,
                               analysis_time=at_ms,
                               llm_status="ok", llm_attempt_count=1,
                               llm_provider_call_count=1, llm_latency_ms=10,
                               llm_prompt_bytes=100,
                               llm_continuity_included=True,
                               llm_terminal_reason=None,
                               llm_provider_timeout_ms=60_000)
    conn.commit()


def fault_llm_success_missing_attempt_metadata(conn):
    """Fault: a decision with ``llm_status="ok"`` but
    ``llm_provider_call_count`` is None (the audit-gap defect: a success
    recorded without proof of a real provider call)."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": None,  # ← missing on an "ok" success
        "llm_attempt_count": 1,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_success_missing_attempt_metadata_negative(conn):
    """Negative: ``llm_status="ok"`` WITH a complete §8 envelope (pcc=1,
    latency, prompt_bytes, continuity_included) -> must NOT fire
    LLM_SUCCESS_MISSING_ATTEMPT_METADATA."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": None,
        "llm_provider_timeout_ms": 60_000,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_continuity_not_included(conn):
    """Fault: a decision that made a real provider call (pcc=1) but
    ``llm_continuity_included`` is False - the prompt was built WITHOUT the
    analysis_continuity block (the S1 / P0 #1 regression)."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": False,  # ← continuity block absent
        "llm_terminal_reason": None,
        "llm_provider_timeout_ms": 60_000,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_continuity_not_included_negative(conn):
    """Negative: real provider call WITH ``llm_continuity_included=True`` ->
    must NOT fire LLM_CONTINUITY_NOT_INCLUDED. Also covers the no-call path
    (pcc=0): continuity flag is irrelevant when no prompt was sent."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": None,
        "llm_provider_timeout_ms": 60_000,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_timeout_config_out_of_range(conn):
    """Fault: a real provider call (pcc=1) with
    ``llm_provider_timeout_ms=0`` - the deadline was already exhausted yet a
    call still happened (the zero-on-real-call defect)."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": None,
        "llm_provider_timeout_ms": 0,  # ← zero on a real call
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_timeout_config_out_of_range_negative(conn):
    """Negative: real provider call with a valid in-range timeout
    (60_000 ms, within (0, 1_200_000]) -> must NOT fire
    LLM_TIMEOUT_CONFIG_OUT_OF_RANGE. Also covers the legitimate 0-on-skip
    path: pcc=0 + timeout=0 is the exhausted-deadline skip, NOT a defect."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": None,
        "llm_provider_timeout_ms": 60_000,  # ← valid
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_batch_degraded_reported_healthy(conn):
    """Fault: a batch with real coverage 1/3 (< 1.0, degraded) but
    ``summary_json.llm_health`` claims full coverage
    (``llm_first_attempt_coverage=1.0``) -> the report rendered false-healthy."""
    at_ms = _recent_analysis_time_ms()
    summary = {
        "llm_health": {
            "expected_symbols": 3,
            "llm_symbols_attempted": 3,
            "llm_first_attempt_coverage": 1.0,  # ← LIES: real is 0.333
            "llm_coverage_degraded": False,
        }
    }
    bid = _insert_batch(conn, batch_id="BATCH_FALSE_HEALTHY",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms, summary=summary)
    # Only BTC attempted -> real coverage 1/3.
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    for sym in ("ETHUSDT", "SOLUSDT"):
        _insert_batch_decision(conn, batch_id=bid, symbol=sym,
                               analysis_time=at_ms,
                               llm_status="ok", llm_attempt_count=0,
                               llm_provider_call_count=0, llm_latency_ms=0,
                               llm_prompt_bytes=None,
                               llm_continuity_included=None,
                               llm_terminal_reason=None,
                               llm_provider_timeout_ms=None)
    conn.commit()


def fault_llm_batch_degraded_reported_healthy_negative(conn):
    """Negative: a degraded batch (coverage < 1.0) whose persisted
    ``summary_json.llm_health`` HONESTLY marks ``llm_coverage_degraded=True``
    -> must NOT fire LLM_BATCH_DEGRADED_REPORTED_HEALTHY."""
    at_ms = _recent_analysis_time_ms()
    summary = {
        "llm_health": {
            "expected_symbols": 3,
            "llm_symbols_attempted": 1,
            "llm_first_attempt_coverage": 0.333,
            "llm_coverage_degraded": True,  # ← honestly marked
        }
    }
    bid = _insert_batch(conn, batch_id="BATCH_HONEST_DEGRADED",
                        enabled=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        analysis_time=at_ms, summary=summary)
    _insert_batch_decision(conn, batch_id=bid, symbol="BTCUSDT",
                           analysis_time=at_ms,
                           llm_status="ok", llm_attempt_count=1,
                           llm_provider_call_count=1, llm_latency_ms=10,
                           llm_prompt_bytes=100, llm_continuity_included=True,
                           llm_terminal_reason=None,
                           llm_provider_timeout_ms=60_000)
    for sym in ("ETHUSDT", "SOLUSDT"):
        _insert_batch_decision(conn, batch_id=bid, symbol=sym,
                               analysis_time=at_ms,
                               llm_status="ok", llm_attempt_count=0,
                               llm_provider_call_count=0, llm_latency_ms=0,
                               llm_prompt_bytes=None,
                               llm_continuity_included=None,
                               llm_terminal_reason=None,
                               llm_provider_timeout_ms=None)
    conn.commit()


def fault_llm_repair_counted_as_provider_call(conn):
    """Fault: a repaired success (``llm_terminal_reason="schema_repaired"``)
    where ``llm_provider_call_count=2 > llm_attempt_count=1`` - the repair
    was billed as a second provider call (the over-counting defect)."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 2,  # ← repair inflated the call counter
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": "schema_repaired",
        "llm_provider_timeout_ms": 60_000,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def fault_llm_repair_counted_as_provider_call_negative(conn):
    """Negative: a repaired success with correct accounting
    (``llm_provider_call_count=1 == llm_attempt_count=1``) -> the repair did
    NOT inflate the physical call counter -> must NOT fire
    LLM_REPAIR_COUNTED_AS_PROVIDER_CALL."""
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "llm_status": "ok",
        "llm_provider_call_count": 1,  # ← correct: repair is not a 2nd call
        "llm_attempt_count": 1,
        "llm_latency_ms": 10,
        "llm_prompt_bytes": 100,
        "llm_continuity_included": True,
        "llm_terminal_reason": "schema_repaired",
        "llm_provider_timeout_ms": 60_000,
    }, analysis_time=at_ms, decision="monitor_only", grade="C")


def assert_caught(result, expected_code):
    """Return (passed, message)."""
    if expected_code in result["codes"]:
        return True, f"PASS: {expected_code} caught"
    return False, f"FAIL: {expected_code} NOT in {result['codes']}"


def assert_caught_with_reason(result, expected_code, expected_reason_substr):
    """R12 P2-2: verify the unhealthy reason, not just the code.

    Pre-R12 the fault seeds for ``batch_time_health_mismatch`` and
    ``batch_time_health_stale_but_ready`` used ``primary_interval="1h"``
    with only ``1h`` in the health dict. The diagnostic fired
    ``missing_required_tf`` (because the 1h batch expected a 5-TF set
    per the fallback) instead of ``not_ready`` / ``stale_by_X_bars``.
    The faults "passed" but for the wrong reason. This assertion
    inspects ``issues[*].details.unhealthy_symbols[*]`` for the
    expected reason substring, ensuring the targeted branch fired.
    """
    if expected_code not in result["codes"]:
        return False, f"FAIL: {expected_code} NOT in {result['codes']}"
    for issue in result["issues"]:
        if issue.get("type") != expected_code:
            continue
        details = issue.get("details") or {}
        unhealthy = details.get("unhealthy_symbols") or []
        for sym_entry in unhealthy:
            if expected_reason_substr in str(sym_entry):
                return True, (
                    f"PASS: {expected_code} caught with reason "
                    f"containing '{expected_reason_substr}' "
                    f"(entry: {sym_entry!r})"
                )
    return False, (
        f"FAIL: {expected_code} fired but no unhealthy_symbols entry "
        f"contained '{expected_reason_substr}'. issues: "
        f"{result['issues']!r}"
    )


def assert_demoted(result, expected_code):
    """For legacy_info cases — code may appear but with severity=legacy_info."""
    for issue in result["issues"]:
        if issue["type"] == expected_code and issue.get("severity") == "legacy_info":
            return True, f"PASS: {expected_code} demoted to legacy_info"
    return False, f"FAIL: {expected_code} not demoted in {result['codes']}"


def assert_not_caught(result, expected_code):
    """Negative assertion — code must NOT appear (P1-8 no_plan case)."""
    if expected_code not in result["codes"]:
        return True, f"PASS: {expected_code} correctly NOT fired"
    return False, f"FAIL: {expected_code} should NOT fire but is in {result['codes']}"


def fault_watch_order_bridge_marker_missing(conn):
    """Fault: the watch->order bridge contract marker row is deleted from
    ``_migration_state``.

    The scratch schema seeds the marker via ``_ensure_all_contract_markers``
    (``_ensure_watch_order_bridge_contract_marker``), so deleting the row makes
    ``diagnose_state_consistency`` emit
    ``watch_order_bridge_contract_marker_missing`` at error severity. Schema
    health must still pass (the health checks do not cover the bridge marker).
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM _migration_state WHERE key = %s",
            (WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY,),
        )
    conn.commit()


def fault_watch_recheck_risk_shape_contract_marker_missing(conn):
    """Fault: the watch-recheck risk-shape contract marker row is deleted from
    ``_migration_state``.

    The scratch schema seeds the marker via ``_ensure_all_contract_markers``
    (``_ensure_watch_recheck_risk_shape_contract_marker``), so deleting the row
    makes ``diagnose_state_consistency`` emit
    ``watch_recheck_risk_shape_contract_marker_missing`` at error severity.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM _migration_state WHERE key = %s",
            (WATCH_RECHECK_RISK_SHAPE_CONTRACT_MARKER_KEY,),
        )
    conn.commit()


def fault_watch_review_payload_serialization_contract_marker_missing(conn):
    """Fault: the watch-review payload-serialization contract marker row is
    deleted from ``_migration_state``, making ``diagnose_state_consistency``
    emit ``watch_review_payload_serialization_contract_marker_missing``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM _migration_state WHERE key = %s",
            (WATCH_REVIEW_PAYLOAD_SERIALIZATION_CONTRACT_MARKER_KEY,),
        )
    conn.commit()


def fault_watch_recheck_funnel_contract_marker_missing(conn):
    """Fault: the watch-recheck funnel-contract marker row is deleted from
    ``_migration_state``, making ``diagnose_state_consistency`` emit
    ``watch_recheck_funnel_contract_marker_missing``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM _migration_state WHERE key = %s",
            (WATCH_RECHECK_FUNNEL_CONTRACT_MARKER_KEY,),
        )
    conn.commit()


def fault_daily_review_delivery_outcome_unknown(conn):
    """Fault: a daily_review:<date> alert_outbox row terminal 'failed' with the
    send-outcome-unknown reason code (the production defect shape: the external
    send happened or may have happened, and the row must never be recycled).

    ``diagnose_daily_review_push_consistency`` classifies it under
    ``daily_review_delivery_outcome_unknown``; a second row with an
    unclassified error exercises the reviewer P2-1 fallback branch, which must
    also surface rather than silently skip. Zero report rows are involved (the
    diagnostic must fire on the outbox state alone).
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alert_outbox (alert_type, symbol, priority, "
            "payload_json, next_retry_at, dedupe_key, status, last_error) "
            "VALUES ('daily_review', NULL, 5, %s, NOW(), %s, 'failed', %s)",
            ('{"a": 1}', "daily_review:2026-08-12",
             "daily_review_send_outcome_unknown_no_retry: timeout reading response"),
        )
        cur.execute(
            "INSERT INTO alert_outbox (alert_type, symbol, priority, "
            "payload_json, next_retry_at, dedupe_key, status, last_error) "
            "VALUES ('daily_review', NULL, 5, %s, NOW(), %s, 'failed', %s)",
            ('{"b": 2}', "daily_review:2026-08-11", "some future reason code"),
        )
    conn.commit()


def main():
    print("=" * 70)
    print("Phase H (07-05) Fault Injection Verification")
    print("=" * 70)
    print("Test DB: isolated crypto_guard_test scratch schemas (app role)")
    print()

    faults = [
        ("missing_candidate_on_llm_failure", fault_missing_candidate,
         MISSING_CANDIDATE_ON_LLM_FAILURE, "caught", None),
        ("withheld_without_blockers", fault_withheld_without_blockers,
         WITHHELD_WITHOUT_BLOCKERS, "caught", None),
        ("missing_analysis_continuity", fault_missing_continuity,
         MISSING_ANALYSIS_CONTINUITY, "caught", None),
        ("oversized_feature_pack", fault_oversized_feature_pack,
         OVERSIZED_FEATURE_PACK, "caught", None),
        ("candidate_effective_plan_mismatch", fault_candidate_effective_mismatch,
         CANDIDATE_EFFECTIVE_PLAN_MISMATCH, "caught", None),
        # R12 P2-2: verify the unhealthy reason, not just the code.
        # ``not_ready`` exercises the ``ready=False`` branch.
        ("batch_time_health_mismatch", fault_batch_time_health_mismatch,
         BATCH_TIME_HEALTH_MISMATCH, "caught_with_reason", "not_ready"),
        # R5 P1-1: ready=True but stale last_close_time — must fire.
        # R12 P2-2: verify ``stale_by`` is in the reason (not just
        # ``missing_required_tf`` from the old broken seed).
        ("batch_time_health_stale_but_ready", fault_batch_time_health_stale_but_ready,
         BATCH_TIME_HEALTH_MISMATCH, "caught_with_reason", "stale_by"),
        ("failed_jobs_outside_window", fault_failed_jobs_outside_window,
         FAILED_JOBS_OUTSIDE_WINDOW, "demoted", None),
        # P1-8 (07-05 final review): no_plan + LLM failed + no candidate
        # must NOT fire the diagnostic. This is the legitimate no-edge path.
        ("no_plan_no_candidate_negative", fault_no_plan_no_candidate_negative,
         MISSING_CANDIDATE_ON_LLM_FAILURE, "not_caught", None),
        # ── Phase I (07-07): LLM retry + hourly accuracy repair fault seeds ──
        ("llm_config_error_http_422", fault_llm_config_error_http_422,
         LLM_CONFIG_ERROR_DETECTED, "caught", None),
        ("llm_retry_exhausted", fault_llm_retry_exhausted,
         LLM_RETRY_EXHAUSTED, "caught", None),
        ("llm_circuit_breaker_open", fault_llm_circuit_breaker_open,
         LLM_CIRCUIT_BREAKER_OPEN, "caught", None),
        ("deterministic_candidate_reported_as_trade_plan",
         fault_deterministic_candidate_reported_as_trade_plan,
         DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN, "caught", None),
        # R6-E P1-3 #5 / AC14: a valid fail-closed unconfirmed candidate
        # (has_trade_plan=False) MUST NOT fire the diagnostic.
        ("deterministic_candidate_reported_as_trade_plan_negative",
         fault_deterministic_candidate_reported_as_trade_plan_negative,
         DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN, "not_caught", None),
        ("raw_grade_exceeds_htf_cap", fault_raw_grade_exceeds_htf_cap,
         EFFECTIVE_GRADE_EXCEEDS_HTF_CAP, "caught", None),
        ("success_batch_missing_completed_symbols",
         fault_success_batch_missing_completed_symbols,
         SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS, "caught", None),
        ("hourly_report_used_partial_running_batch",
         fault_hourly_report_used_partial_running_batch,
         HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH, "caught", None),
        # ── 07-10 P1-1 (design §10): eight Phase F fair-scheduling fault seeds ──
        ("llm_first_attempt_coverage_low", fault_llm_first_attempt_coverage_low,
         LLM_FIRST_ATTEMPT_COVERAGE_LOW, "caught", None),
        ("llm_first_attempt_coverage_low_negative",
         fault_llm_first_attempt_coverage_low_negative,
         LLM_FIRST_ATTEMPT_COVERAGE_LOW, "not_caught", None),
        ("llm_symbol_starvation", fault_llm_symbol_starvation,
         LLM_SYMBOL_STARVATION, "caught", None),
        ("llm_symbol_starvation_negative", fault_llm_symbol_starvation_negative,
         LLM_SYMBOL_STARVATION, "not_caught", None),
        ("llm_report_count_mismatch", fault_llm_report_count_mismatch,
         LLM_REPORT_COUNT_MISMATCH, "caught", None),
        ("llm_report_count_mismatch_negative",
         fault_llm_report_count_mismatch_negative,
         LLM_REPORT_COUNT_MISMATCH, "not_caught", None),
        ("llm_success_missing_attempt_metadata",
         fault_llm_success_missing_attempt_metadata,
         LLM_SUCCESS_MISSING_ATTEMPT_METADATA, "caught", None),
        ("llm_success_missing_attempt_metadata_negative",
         fault_llm_success_missing_attempt_metadata_negative,
         LLM_SUCCESS_MISSING_ATTEMPT_METADATA, "not_caught", None),
        ("llm_continuity_not_included", fault_llm_continuity_not_included,
         LLM_CONTINUITY_NOT_INCLUDED, "caught", None),
        ("llm_continuity_not_included_negative",
         fault_llm_continuity_not_included_negative,
         LLM_CONTINUITY_NOT_INCLUDED, "not_caught", None),
        ("llm_timeout_config_out_of_range",
         fault_llm_timeout_config_out_of_range,
         LLM_TIMEOUT_CONFIG_OUT_OF_RANGE, "caught", None),
        ("llm_timeout_config_out_of_range_negative",
         fault_llm_timeout_config_out_of_range_negative,
         LLM_TIMEOUT_CONFIG_OUT_OF_RANGE, "not_caught", None),
        ("llm_batch_degraded_reported_healthy",
         fault_llm_batch_degraded_reported_healthy,
         LLM_BATCH_DEGRADED_REPORTED_HEALTHY, "caught", None),
        ("llm_batch_degraded_reported_healthy_negative",
         fault_llm_batch_degraded_reported_healthy_negative,
         LLM_BATCH_DEGRADED_REPORTED_HEALTHY, "not_caught", None),
        ("llm_repair_counted_as_provider_call",
         fault_llm_repair_counted_as_provider_call,
         LLM_REPAIR_COUNTED_AS_PROVIDER_CALL, "caught", None),
        ("llm_repair_counted_as_provider_call_negative",
         fault_llm_repair_counted_as_provider_call_negative,
         LLM_REPAIR_COUNTED_AS_PROVIDER_CALL, "not_caught", None),
    ]

    results = []
    for name, fn, expected_code, mode, reason_substr in faults:
        result = _run_one(name, fn)
        if mode == "caught":
            passed, msg = assert_caught(result, expected_code)
        elif mode == "caught_with_reason":
            passed, msg = assert_caught_with_reason(
                result, expected_code, reason_substr,
            )
        elif mode == "demoted":
            passed, msg = assert_demoted(result, expected_code)
        elif mode == "not_caught":
            passed, msg = assert_not_caught(result, expected_code)
        else:
            raise ValueError(f"unknown mode: {mode}")
        results.append((name, passed, msg))
        print(f"[{name}]")
        print(f"  {msg}")
        print(f"  codes: {result['codes']}")
        print()

    # ── 08-06 (release-blocker rework): watch->order bridge contract marker ──
    # This fault runs through diagnose_state_consistency (the report
    # diagnostic does not include the bridge marker check).
    for name, fn, expected_code in [
        ("watch_order_bridge_contract_marker_missing",
         fault_watch_order_bridge_marker_missing,
         WATCH_ORDER_BRIDGE_CONTRACT_MARKER_MISSING),
        # 08-08 Step 7: three watch-recheck contract markers (fail-closed).
        ("watch_recheck_risk_shape_contract_marker_missing",
         fault_watch_recheck_risk_shape_contract_marker_missing,
         WATCH_RECHECK_RISK_SHAPE_CONTRACT_MARKER_MISSING),
        ("watch_review_payload_serialization_contract_marker_missing",
         fault_watch_review_payload_serialization_contract_marker_missing,
         WATCH_REVIEW_PAYLOAD_SERIALIZATION_CONTRACT_MARKER_MISSING),
        ("watch_recheck_funnel_contract_marker_missing",
         fault_watch_recheck_funnel_contract_marker_missing,
         WATCH_RECHECK_FUNNEL_CONTRACT_MARKER_MISSING),
        # 08-12 P1 (Codex P1-2): daily_review:<date> terminal failed with a
        # send-outcome-unknown reason (or any unclassified terminal failure)
        # must surface as delivery_outcome_unknown — never a silent skip.
        ("daily_review_delivery_outcome_unknown",
         fault_daily_review_delivery_outcome_unknown,
         DAILY_REVIEW_DELIVERY_OUTCOME_UNKNOWN),
    ]:
        result = _run_one_state(name, fn)
        passed, msg = assert_caught(result, expected_code)
        results.append((name, passed, msg))
        print(f"[{name}]")
        print(f"  {msg}")
        print(f"  codes: {result['codes']}")
        print()

    print("=" * 70)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"Summary: {passed_count}/{len(results)} faults verified")
    print("=" * 70)
    if passed_count != len(results):
        print("FAILURE: not all faults verified")
        return 1
    print("SUCCESS: all Phase H diagnostics catch (or skip) their target faults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
