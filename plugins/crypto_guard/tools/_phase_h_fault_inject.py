"""Phase H (07-05) fault injection verifier.

For each of the 7 Phase H diagnostic codes, seed a defective row into a fresh
temp DB, run diagnose_report_accuracy, and assert the corresponding code is
reported. Then clean up and report PASS/FAIL for each fault.

Usage:
    python plugins/crypto_guard/tools/_phase_h_fault_inject.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

# Force temp DB before any config import
TMP_DIR = Path(tempfile.mkdtemp(prefix="cg_phase_h_fault_"))
DB_PATH = TMP_DIR / "fault.db"
os.environ["CRYPTO_GUARD_DB"] = str(DB_PATH)

from plugins.crypto_guard.config import load_config
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.storage.migrations import initialize_database
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
    LLM_FAILURE_RATE_HIGH,
    LLM_CONFIG_ERROR_DETECTED,
    LLM_RETRY_EXHAUSTED,
    LLM_CIRCUIT_BREAKER_OPEN,
    DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN,
    RAW_GRADE_EXCEEDS_HTF_CAP,
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
    conn.execute(
        "INSERT INTO ga_decisions ("
        "  symbol, analysis_time, analysis_time_utc, decision_type,"
        "  signal_grade, confidence, decision, market_bias, trend_stage,"
        "  skill_result_refs_json, evidence_json, counter_evidence_json,"
        "  risk_check_json, trade_plan_json, opportunity_watch_json,"
        "  feishu_actions_json, final_summary, raw_decision_json, batch_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    """Reset DB, run fn to seed fault, run diagnostics, return result dict."""
    # Fresh DB for isolation
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        side = DB_PATH.with_suffix(DB_PATH.suffix + suffix)
        if side.exists():
            side.unlink()
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    repo = CryptoGuardRepository(conn)
    try:
        fn(conn)
        result = diagnose_report_accuracy(repo)
        codes = [i["type"] for i in result["issues"]]
        return {"name": name, "codes": codes, "issues": result["issues"]}
    finally:
        conn.close()


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
    conn.execute(
        "INSERT INTO analysis_batches (batch_id, primary_interval, "
        "analysis_time, status, enabled_symbols_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch_id, "15m", analysis_time, "success",
         json.dumps(["BTCUSDT"])),
    )
    # Insert a batch_symbol_status row marked completed
    conn.execute(
        "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
        "VALUES (?, ?, ?)",
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
    conn.execute(
        "INSERT INTO market_snapshots "
        "(symbol, analysis_time, mode, snapshot_json, data_quality_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("BTCUSDT", analysis_time, "scheduled", "{}",
         json.dumps({"health": {
             "1d": {"ready": True, "last_close_time": analysis_time - 86_400_000},
             "4h": {"ready": True, "last_close_time": analysis_time - 14_400_000},
             "1h": {"ready": False, "last_close_time": 0},
             "15m": {"ready": True, "last_close_time": analysis_time - 900_000},
             "5m": {"ready": True, "last_close_time": analysis_time - 300_000},
         }})),
    )
    snap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Insert a ga_decisions row referencing the snapshot via snapshot_id
    _insert_decision(conn, raw={
        "batch_id": batch_id,
    }, batch_id=batch_id, decision="no_edge", grade="C")
    conn.execute(
        "UPDATE ga_decisions SET snapshot_id = ? WHERE batch_id = ?",
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
    conn.execute(
        "INSERT INTO analysis_batches (batch_id, primary_interval, "
        "analysis_time, status, enabled_symbols_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch_id, "15m", analysis_time, "success",
         json.dumps(["BTCUSDT"])),
    )
    conn.execute(
        "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
        "VALUES (?, ?, ?)",
        (batch_id, "BTCUSDT", "completed"),
    )
    # ``ready=True`` (would have passed old check) but last_close is 12h
    # stale, which must be caught by the R5 stale lower bound.
    # R12 P2-2: seed all 5 required TFs. Only ``1h`` is stale; the
    # others are fresh so the only reason the diagnostic fires is the
    # stale lower bound on ``1h``.
    conn.execute(
        "INSERT INTO market_snapshots "
        "(symbol, analysis_time, mode, snapshot_json, data_quality_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("BTCUSDT", analysis_time, "scheduled", "{}",
         json.dumps({"health": {
             "1d": {"ready": True, "last_close_time": analysis_time - 86_400_000},
             "4h": {"ready": True, "last_close_time": analysis_time - 14_400_000},
             "1h": {"ready": True, "last_close_time": stale_close},
             "15m": {"ready": True, "last_close_time": analysis_time - 900_000},
             "5m": {"ready": True, "last_close_time": analysis_time - 300_000},
         }})),
    )
    snap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _insert_decision(conn, raw={
        "batch_id": batch_id,
    }, batch_id=batch_id, decision="no_edge", grade="C")
    conn.execute(
        "UPDATE ga_decisions SET snapshot_id = ? WHERE batch_id = ?",
        (snap_id, batch_id),
    )
    conn.commit()


def fault_failed_jobs_outside_window(conn):
    """Fault: failed batch older than 7 days — should be legacy_info."""
    # Use sqlite CURRENT_TIMESTAMP (UTC now). Set started_at to 8 days ago.
    conn.execute(
        "INSERT INTO analysis_batches ("
        "  batch_id, primary_interval, analysis_time, status, started_at,"
        "  enabled_symbols_json, completed_symbols_json, failed_symbols_json"
        ") VALUES (?, ?, ?, ?, datetime('now', '-8 days'), ?, ?, ?)",
        ("BATCH_OLD", "1h", 2_000_000_000_000, "failed",
         json.dumps(["BTCUSDT"]), json.dumps([]), json.dumps(["BTCUSDT"])),
    )
    conn.commit()


# ── Phase I (07-07): LLM retry + hourly accuracy repair fault seeds ──────────

def _recent_analysis_time_ms() -> int:
    """Return a recent analysis_time (now - 1h in ms) so 24h-lookback
    diagnostics see the seeded row."""
    import time as _time
    return int(_time.time() * 1000) - 3_600_000


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
    conn.execute(
        "INSERT INTO analysis_batches ("
        "  batch_id, primary_interval, analysis_time, status, started_at,"
        "  summary_json"
        ") VALUES (?, ?, ?, ?, datetime('now'), ?)",
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
    """Fault: raw_signal_grade=S but 1D+4H both opposite to candidate.

    Per design §7.1 Step 4b Cap 1: 1D and 4H both opposite to candidate →
    max grade B. A raw S grade in this configuration violates the cap.
    """
    at_ms = _recent_analysis_time_ms()
    _insert_decision(conn, raw={
        "signal_grade": "S",
        "raw_signal_grade": "S",
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
    conn.execute(
        "INSERT INTO analysis_batches ("
        "  batch_id, primary_interval, analysis_time, status, started_at,"
        "  enabled_symbols_json, completed_symbols_json, failed_symbols_json"
        ") VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?)",
        ("BATCH_EMPTY_COMPLETED", "1h", at_ms, "success",
         json.dumps(["BTCUSDT", "ETHUSDT"]),
         json.dumps([]),  # ← raw column empty — the defect
         json.dumps([])),
    )
    # Plant a live completed entry to prove the column is stale (not
    # legitimately empty).
    conn.execute(
        "INSERT INTO batch_symbol_status (batch_id, symbol, status) "
        "VALUES (?, ?, ?)",
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
    conn.execute(
        "INSERT INTO analysis_batches ("
        "  batch_id, primary_interval, analysis_time, status, started_at"
        ") VALUES (?, ?, ?, ?, datetime('now'))",
        ("BATCH_RUNNING", "1h", at_ms, "running"),
    )
    # Plant a recent hourly_summary alert (within last hour).
    conn.execute(
        "INSERT INTO alert_outbox ("
        "  alert_type, symbol, priority, payload_json, status, created_at"
        ") VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("hourly_summary", None, 5,
         json.dumps({"fallback_text": "hourly report test"}), "sent"),
    )
    conn.commit()


# ── 07-10 P1-1 (design §10): eight Phase F fair-scheduling fault seeds ───────
#
# Each seed plants the EXACT production defect into a fresh DB (whose
# ``initialize_database`` writes the ``llm_fair_scheduling_context_contract_v1``
# marker, so findings fire at their real severity - NOT demoted). Each has a
# paired negative control proving the check does NOT fire on the healthy
# shape (so a future widening cannot silently green it).
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
    conn.execute(
        "INSERT INTO analysis_batches ("
        "  batch_id, primary_interval, analysis_time, status, started_at,"
        "  enabled_symbols_json, completed_symbols_json, failed_symbols_json,"
        "  summary_json"
        ") VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)",
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


def main():
    print("=" * 70)
    print("Phase H (07-05) Fault Injection Verification")
    print("=" * 70)
    print(f"Temp DB: {DB_PATH}")
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
         RAW_GRADE_EXCEEDS_HTF_CAP, "caught", None),
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
