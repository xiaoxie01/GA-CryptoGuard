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
