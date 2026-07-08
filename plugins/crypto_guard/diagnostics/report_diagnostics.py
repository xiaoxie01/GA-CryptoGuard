"""Hourly report market-accuracy diagnostics.

Covers the ten P2 issue categories enumerated in the Hourly Report Market
Accuracy Fix PRD / research 00-summary. Pure-function helpers operate over
the already-pulled ga_decisions rows plus minimal repo lookups, so they can
be invoked from the report renderer and the state-consistency sweep alike.

Each checker returns a list of issue dicts with:
    {
        "type": <known_code>,
        "severity": "error" | "warning" | "info",
        "details": {...},
        "suggested_action": str,
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import INTERVAL_MS

# Known issue codes — kept in sync with PRD ## P2 diagnostics.
HOURLY_REPORT_INCOMPLETE_BATCH = "hourly_report_incomplete_batch"
HOURLY_REPORT_STALE_DECISION = "hourly_report_stale_decision"
EXECUTABLE_WITHOUT_TRADE_PLAN = "executable_opportunity_without_trade_plan"
EXECUTABLE_RISK_REJECTED = "executable_opportunity_risk_rejected"
OPPORTUNITY_BELOW_CONFIDENCE = "opportunity_below_confidence_threshold"
SUMMARY_EXECUTION_CONFLICT = "summary_execution_state_conflict"
EXCESSIVE_GRADE_FLIP = "excessive_grade_flip"
DIRECTION_FLIP_NO_CLOSED_CANDLE = "direction_flip_without_closed_candle_confirmation"
INVALID_LIQUIDITY_SWEEP = "invalid_liquidity_sweep_semantics"
NEGATIVE_DRAWDOWN_DISPLAY = "negative_drawdown_display"

# Phase E (07-03): five new semantic-accuracy issue codes plus the
# marker-missing code. These use the independent
# ``hourly_market_semantic_accuracy_contract_v1`` marker as the cutoff
# between ``legacy_info`` (pre-marker) and ``error`` / ``warning``
# (post-marker). The R4 marker remains the cutoff for the original ten codes.
BIAS_STAGE_SEMANTIC_CONFLICT = "bias_stage_semantic_conflict"
HTF_COUNTERTREND_OVERCONFIDENCE = "htf_countertrend_overconfidence"
SUMMARY_STRUCTURED_STATE_MISMATCH = "summary_structured_state_mismatch"
OBSERVATION_REASON_MISSING_MARKET_CONTEXT = "observation_reason_missing_market_context"
NO_EDGE_REASON_COVERAGE_MISMATCH = "no_edge_reason_coverage_mismatch"
SEMANTIC_CONTRACT_MARKER_MISSING = "semantic_contract_marker_missing"
# R2-8 (07-03 final review P1): register the three new diagnostic types
# emitted by _check_summary_structured_state_mismatch so they get the
# semantic-accuracy marker cutoff demotion (legacy_info for pre-marker
# rows, error for post-marker rows). Without registration, post-marker
# data with these issues would always be error-severity even when the
# marker has not been deployed, and pre-marker data would never demote.
MISSING_STRUCTURED_FIELD = "missing_structured_field"
CANONICAL_SUMMARY_DRIFT = "canonical_summary_drift"
RENDERED_SUMMARY_DRIFT = "rendered_summary_drift"

# Phase H (07-05): decision-context-continuity contract issue codes. Each
# code corresponds to a Phase A-G contract that must hold for post-marker
# rows. Pre-marker rows are demoted to legacy_info by
# _apply_continuity_marker_cutoff so historical audit findings remain
# visible without failing the diagnostic.
PLAN_LIFECYCLE_CONTRACT_MARKER_MISSING = "plan_lifecycle_contract_marker_missing"
MISSING_CANDIDATE_ON_LLM_FAILURE = "missing_candidate_on_llm_failure"
WITHHELD_WITHOUT_BLOCKERS = "withheld_without_blockers"
MISSING_ANALYSIS_CONTINUITY = "missing_analysis_continuity"
OVERSIZED_FEATURE_PACK = "oversized_feature_pack"
CANDIDATE_EFFECTIVE_PLAN_MISMATCH = "candidate_effective_plan_mismatch"
BATCH_TIME_HEALTH_MISMATCH = "batch_time_health_mismatch"
FAILED_JOBS_OUTSIDE_WINDOW = "failed_jobs_outside_window"

# Phase I (07-07): LLM retry + hourly accuracy repair diagnostic codes. Each
# code corresponds to a Phase B-E contract. These are runtime diagnostics,
# NOT migration gates — markers are NOT written to _migration_state for them.
LLM_FAILURE_RATE_HIGH = "llm_failure_rate_high"
LLM_CONFIG_ERROR_DETECTED = "llm_config_error_detected"
LLM_RETRY_EXHAUSTED = "llm_retry_exhausted"
LLM_CIRCUIT_BREAKER_OPEN = "llm_circuit_breaker_open"
DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN = "deterministic_candidate_reported_as_trade_plan"
RAW_GRADE_EXCEEDS_HTF_CAP = "raw_grade_exceeds_htf_cap"
SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS = "success_batch_missing_completed_symbols"
HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH = "hourly_report_used_partial_running_batch"


def diagnose_report_accuracy(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> dict[str, Any]:
    """Run all hourly-report-accuracy diagnostics.

    Returns the standard state-consistency shape (ok / issues / summary /
    total_issues) so it can be merged into diagnose_state_consistency output
    or rendered standalone in the hourly report.

    FS-5: Issues are classified into three buckets:
      - ``error``: current R4-runtime violations (post-marker)
      - ``warning``: current R4-runtime warnings (post-marker)
      - ``legacy_info``: pre-marker audit findings preserved for traceability

    ``ok`` is True iff ``error_count == 0``. Warnings and legacy_info remain
    visible and must be explained, but do not fail the diagnostic.
    """
    issues: list[dict[str, Any]] = []
    # Phase E: marker-missing check runs first so a missing contract is
    # explicitly surfaced even when all other checks would otherwise pass.
    issues.extend(_check_semantic_contract_markers_missing(repo))
    issues.extend(_check_hourly_report_incomplete_batch(repo, batch_id))
    issues.extend(_check_hourly_report_stale_decision(repo, batch_id=batch_id))
    issues.extend(_check_executable_opportunity_without_trade_plan(repo))
    issues.extend(_check_executable_opportunity_risk_rejected(repo))
    issues.extend(_check_opportunity_below_confidence(repo))
    issues.extend(_check_summary_execution_state_conflict(repo))
    issues.extend(_check_excessive_grade_flip(repo))
    issues.extend(_check_direction_flip_without_closed_candle(repo))
    issues.extend(_check_invalid_liquidity_sweep_semantics(repo))
    issues.extend(_check_negative_drawdown_display(repo))
    # Phase E: five new semantic-accuracy checks. These use the independent
    # ``hourly_market_semantic_accuracy_contract_v1`` marker as the cutoff,
    # applied below via _apply_semantic_marker_cutoff.
    issues.extend(_check_bias_stage_semantic_conflict(repo))
    issues.extend(_check_htf_countertrend_overconfidence(repo))
    issues.extend(_check_summary_structured_state_mismatch(repo))
    issues.extend(_check_observation_reason_missing_market_context(repo))
    issues.extend(_check_no_edge_reason_coverage_mismatch(repo))
    # Phase H (07-05): seven new decision-context-continuity contract
    # checks. These use the independent
    # ``hourly_decision_context_continuity_contract_v1`` marker as the
    # cutoff, applied below via _apply_continuity_marker_cutoff. The
    # marker-missing check runs first so a missing contract is explicitly
    # surfaced even when all other checks would otherwise pass.
    issues.extend(_check_plan_lifecycle_contract_markers_missing(repo))
    issues.extend(_check_missing_candidate_on_llm_failure(repo))
    issues.extend(_check_withheld_without_blockers(repo))
    issues.extend(_check_missing_analysis_continuity(repo))
    issues.extend(_check_oversized_feature_pack(repo))
    issues.extend(_check_candidate_effective_plan_mismatch(repo))
    issues.extend(_check_batch_time_health_mismatch(repo))
    issues.extend(_check_failed_jobs_outside_window(repo))

    # Phase I (07-07): LLM retry + hourly accuracy repair diagnostics. These
    # are runtime diagnostics without a marker cutoff — they fire on any
    # matching data in the latest 24h / latest batch. See PRD AC18.
    issues.extend(_check_llm_failure_rate_high(repo))
    issues.extend(_check_llm_config_error_detected(repo))
    issues.extend(_check_llm_retry_exhausted(repo))
    issues.extend(_check_llm_circuit_breaker_open(repo))
    issues.extend(_check_deterministic_candidate_reported_as_trade_plan(repo))
    issues.extend(_check_raw_grade_exceeds_htf_cap(repo))
    issues.extend(_check_success_batch_missing_completed_symbols(repo))
    issues.extend(_check_hourly_report_used_partial_running_batch(repo))

    # FS-5: re-classify pre-marker issues as legacy_info. The marker is the
    # R4 contract version timestamp written by the migration once the R4
    # postconditions (schema health, batch_symbol_status CHECK, etc.) hold.
    marker_ts = _get_r4_contract_marker_ts(repo)
    if marker_ts is not None:
        for issue in issues:
            decision_id = (issue.get("details") or {}).get("decision_id")
            if decision_id is None:
                continue
            decision_ts = _get_decision_created_ts(repo, decision_id)
            if decision_ts is not None and decision_ts < marker_ts:
                # Pre-marker decision — demote to legacy_info, preserve visibility.
                issue["severity"] = "legacy_info"

    # Phase E: apply the independent semantic-accuracy marker cutoff to the
    # five new semantic checks. Decisions created before the semantic marker
    # are demoted to legacy_info; post-marker errors stay error/warning.
    _apply_semantic_marker_cutoff(repo, issues)

    # Phase H: apply the independent continuity-contract marker cutoff to
    # the seven new Phase A-G contract checks. Decisions created before
    # the continuity marker are demoted to legacy_info; post-marker
    # errors stay error/warning.
    _apply_continuity_marker_cutoff(repo, issues)

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    legacy_info_count = sum(1 for i in issues if i["severity"] == "legacy_info")

    summary = {
        HOURLY_REPORT_INCOMPLETE_BATCH: _count(issues, HOURLY_REPORT_INCOMPLETE_BATCH),
        HOURLY_REPORT_STALE_DECISION: _count(issues, HOURLY_REPORT_STALE_DECISION),
        EXECUTABLE_WITHOUT_TRADE_PLAN: _count(issues, EXECUTABLE_WITHOUT_TRADE_PLAN),
        EXECUTABLE_RISK_REJECTED: _count(issues, EXECUTABLE_RISK_REJECTED),
        OPPORTUNITY_BELOW_CONFIDENCE: _count(issues, OPPORTUNITY_BELOW_CONFIDENCE),
        SUMMARY_EXECUTION_CONFLICT: _count(issues, SUMMARY_EXECUTION_CONFLICT),
        EXCESSIVE_GRADE_FLIP: _count(issues, EXCESSIVE_GRADE_FLIP),
        DIRECTION_FLIP_NO_CLOSED_CANDLE: _count(issues, DIRECTION_FLIP_NO_CLOSED_CANDLE),
        INVALID_LIQUIDITY_SWEEP: _count(issues, INVALID_LIQUIDITY_SWEEP),
        NEGATIVE_DRAWDOWN_DISPLAY: _count(issues, NEGATIVE_DRAWDOWN_DISPLAY),
        BIAS_STAGE_SEMANTIC_CONFLICT: _count(issues, BIAS_STAGE_SEMANTIC_CONFLICT),
        HTF_COUNTERTREND_OVERCONFIDENCE: _count(issues, HTF_COUNTERTREND_OVERCONFIDENCE),
        SUMMARY_STRUCTURED_STATE_MISMATCH: _count(issues, SUMMARY_STRUCTURED_STATE_MISMATCH),
        OBSERVATION_REASON_MISSING_MARKET_CONTEXT: _count(issues, OBSERVATION_REASON_MISSING_MARKET_CONTEXT),
        NO_EDGE_REASON_COVERAGE_MISMATCH: _count(issues, NO_EDGE_REASON_COVERAGE_MISMATCH),
        SEMANTIC_CONTRACT_MARKER_MISSING: _count(issues, SEMANTIC_CONTRACT_MARKER_MISSING),
        PLAN_LIFECYCLE_CONTRACT_MARKER_MISSING: _count(issues, PLAN_LIFECYCLE_CONTRACT_MARKER_MISSING),
        MISSING_CANDIDATE_ON_LLM_FAILURE: _count(issues, MISSING_CANDIDATE_ON_LLM_FAILURE),
        WITHHELD_WITHOUT_BLOCKERS: _count(issues, WITHHELD_WITHOUT_BLOCKERS),
        MISSING_ANALYSIS_CONTINUITY: _count(issues, MISSING_ANALYSIS_CONTINUITY),
        OVERSIZED_FEATURE_PACK: _count(issues, OVERSIZED_FEATURE_PACK),
        CANDIDATE_EFFECTIVE_PLAN_MISMATCH: _count(issues, CANDIDATE_EFFECTIVE_PLAN_MISMATCH),
        BATCH_TIME_HEALTH_MISMATCH: _count(issues, BATCH_TIME_HEALTH_MISMATCH),
        FAILED_JOBS_OUTSIDE_WINDOW: _count(issues, FAILED_JOBS_OUTSIDE_WINDOW),
        LLM_FAILURE_RATE_HIGH: _count(issues, LLM_FAILURE_RATE_HIGH),
        LLM_CONFIG_ERROR_DETECTED: _count(issues, LLM_CONFIG_ERROR_DETECTED),
        LLM_RETRY_EXHAUSTED: _count(issues, LLM_RETRY_EXHAUSTED),
        LLM_CIRCUIT_BREAKER_OPEN: _count(issues, LLM_CIRCUIT_BREAKER_OPEN),
        DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN: _count(issues, DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN),
        RAW_GRADE_EXCEEDS_HTF_CAP: _count(issues, RAW_GRADE_EXCEEDS_HTF_CAP),
        SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS: _count(issues, SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS),
        HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH: _count(issues, HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
    }
    return {
        "ok": error_count == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
    }


# FS-5: R4 contract marker key in _migration_state. The marker is written
# only after the R4 migration postconditions succeed (schema health OK,
# batch_symbol_status CHECK constraint present, etc.). Decisions created
# before this marker are legacy audit findings, not current R4 errors.
R4_CONTRACT_MARKER_KEY = "hourly_report_accuracy_r4_contract_v1"

# Phase E (07-03): independent semantic-accuracy contract marker. The five
# new checks (bias_stage_semantic_conflict, htf_countertrend_overconfidence,
# summary_structured_state_mismatch, observation_reason_missing_market_context,
# no_edge_reason_coverage_mismatch) use this marker as the cutoff between
# ``legacy_info`` (pre-marker) and ``error`` / ``warning`` (post-marker).
SEMANTIC_ACCURACY_MARKER_KEY = "hourly_market_semantic_accuracy_contract_v1"

# Phase H (07-05): independent decision-context-continuity contract marker.
# The seven new Phase A-G contract diagnostics
# (missing_candidate_on_llm_failure, withheld_without_blockers,
# missing_analysis_continuity, oversized_feature_pack,
# candidate_effective_plan_mismatch, batch_time_health_mismatch,
# failed_jobs_outside_window) use this cutoff, not the R4 or semantic-accuracy
# boundary. ``applied_at`` is the cutoff between ``legacy_info`` (pre-marker)
# and ``error`` / ``warning`` (post-marker).
CONTINUITY_CONTRACT_MARKER_KEY = "hourly_decision_context_continuity_contract_v1"

# Phase H (07-05): default serialized size budget for the
# MultiTimeframeFeaturePack. The builder enforces 24 KiB at construction
# time; this diagnostic re-checks the persisted payload so historical rows
# or a regression in the builder's budget enforcement are surfaced.
FEATURE_PACK_SIZE_BUDGET_BYTES = 24 * 1024

# Phase H (07-05): default window for "recent failed jobs" diagnostics. The
# PRD forbids permanently repeating historical errors in the recent-failures
# list — limit to the last 7 days so legacy failures age out.
FAILED_JOBS_RECENT_WINDOW_DAYS = 7

# The set of issue types that fall under the semantic-accuracy contract.
# Used by _apply_semantic_marker_cutoff to demote pre-marker findings.
# R2-8 (07-03 final review P1): includes the three new diagnostic types
# emitted by _check_summary_structured_state_mismatch
# (missing_structured_field, canonical_summary_drift, rendered_summary_drift).
_SEMANTIC_ISSUE_TYPES: frozenset[str] = frozenset({
    BIAS_STAGE_SEMANTIC_CONFLICT,
    HTF_COUNTERTREND_OVERCONFIDENCE,
    SUMMARY_STRUCTURED_STATE_MISMATCH,
    OBSERVATION_REASON_MISSING_MARKET_CONTEXT,
    NO_EDGE_REASON_COVERAGE_MISMATCH,
    MISSING_STRUCTURED_FIELD,
    CANONICAL_SUMMARY_DRIFT,
    RENDERED_SUMMARY_DRIFT,
})

# Phase H (07-05): the set of issue types that fall under the
# decision-context-continuity contract. Used by
# _apply_continuity_marker_cutoff to demote pre-marker findings to
# legacy_info. Excludes the marker-missing code (always error) and the
# failed-jobs-outside-window code (already windowed by query).
_CONTINUITY_ISSUE_TYPES: frozenset[str] = frozenset({
    MISSING_CANDIDATE_ON_LLM_FAILURE,
    WITHHELD_WITHOUT_BLOCKERS,
    MISSING_ANALYSIS_CONTINUITY,
    OVERSIZED_FEATURE_PACK,
    CANDIDATE_EFFECTIVE_PLAN_MISMATCH,
    BATCH_TIME_HEALTH_MISMATCH,
})


def _get_r4_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Return the R4 contract marker's applied_at timestamp, or None."""
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=?",
            (R4_CONTRACT_MARKER_KEY,),
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        return None
    return None


def _get_semantic_accuracy_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Phase E: return the semantic-accuracy marker's applied_at, or None.

    None means the marker has not been deployed — callers (the five new
    checks) skip themselves in that case so historical data is not flagged
    with ``error`` severity against a contract that has not yet been
    initialized. The marker-missing check separately surfaces the absence.
    """
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=?",
            (SEMANTIC_ACCURACY_MARKER_KEY,),
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        return None
    return None


def _get_continuity_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Phase H: return the decision-context-continuity marker's applied_at, or None.

    None means the marker has not been deployed — callers (the seven new
    Phase A-G contract checks) skip themselves in that case so historical
    data is not flagged with ``error`` severity against a contract that has
    not yet been initialized. The marker-missing check
    (_check_plan_lifecycle_contract_markers_missing) separately surfaces
    the absence as an error.
    """
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=?",
            (CONTINUITY_CONTRACT_MARKER_KEY,),
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        return None
    return None


def _apply_continuity_marker_cutoff(repo: CryptoGuardRepository, issues: list[dict[str, Any]]) -> None:
    """Phase H: demote pre-marker continuity-contract findings to legacy_info.

    Mirrors the R4 / semantic-accuracy marker cutoff pattern but scoped to
    the seven new Phase A-G contract issue types. When the marker is absent
    the function is a no-op — the marker-missing check
    (_check_plan_lifecycle_contract_markers_missing) already surfaces the
    absence as an error.
    """
    marker_ts = _get_continuity_contract_marker_ts(repo)
    if marker_ts is None:
        return
    for issue in issues:
        if issue.get("type") not in _CONTINUITY_ISSUE_TYPES:
            continue
        decision_id = (issue.get("details") or {}).get("decision_id")
        if decision_id is None:
            at_ms = (issue.get("details") or {}).get("analysis_time")
            if at_ms is not None:
                try:
                    dt = datetime.fromtimestamp(int(at_ms) / 1000, tz=timezone.utc)
                    decision_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError, OSError):
                    continue
            else:
                continue
        else:
            decision_ts = _get_decision_created_ts(repo, decision_id)
        if decision_ts is not None and decision_ts < marker_ts:
            issue["severity"] = "legacy_info"


def _apply_semantic_marker_cutoff(repo: CryptoGuardRepository, issues: list[dict[str, Any]]) -> None:
    """Phase E: demote pre-marker semantic-accuracy findings to legacy_info.

    Mirrors the R4 marker cutoff pattern but scoped to the five new
    semantic-accuracy issue types. When the semantic marker is absent the
    function is a no-op — the marker-missing check (_check_semantic_contract_markers_missing)
    already surfaces the absence as an error.
    """
    marker_ts = _get_semantic_accuracy_marker_ts(repo)
    if marker_ts is None:
        return
    for issue in issues:
        if issue.get("type") not in _SEMANTIC_ISSUE_TYPES:
            continue
        decision_id = (issue.get("details") or {}).get("decision_id")
        if decision_id is None:
            # Aggregate-level issues (e.g. no_edge batch mismatches) without a
            # single decision_id fall back to the batch analysis_time lookup
            # via details.analysis_time when available.
            at_ms = (issue.get("details") or {}).get("analysis_time")
            if at_ms is not None:
                try:
                    dt = datetime.fromtimestamp(int(at_ms) / 1000, tz=timezone.utc)
                    decision_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError, OSError):
                    continue
            else:
                continue
        else:
            decision_ts = _get_decision_created_ts(repo, decision_id)
        if decision_ts is not None and decision_ts < marker_ts:
            issue["severity"] = "legacy_info"


def _get_decision_created_ts(repo: CryptoGuardRepository, decision_id: int | None) -> str | None:
    """Return the created_at timestamp for a ga_decisions row, or None."""
    if decision_id is None:
        return None
    try:
        row = repo.conn.execute(
            "SELECT created_at FROM ga_decisions WHERE id=?",
            (int(decision_id),),
        ).fetchone()
        if row and row["created_at"]:
            return str(row["created_at"])
    except Exception:
        return None
    return None


def run_for_report(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> dict[str, Any]:
    """Wrapper for render-time invocation; never raises.

    P1-11e: returns ok=False on error (fail-closed).
    """
    try:
        return diagnose_report_accuracy(repo, batch_id=batch_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "summary": {}, "total_issues": 0, "issues": []}


# ── issue codes omit "rate,"; for schema simplicity keep both forms documented. ──
def _count(issues: list[dict[str, Any]], code: str) -> int:
    return sum(1 for i in issues if i["type"] == code)


def _issue(code: str, severity: str, details: dict[str, Any], action: str) -> dict[str, Any]:
    return {"type": code, "severity": severity, "details": details, "suggested_action": action}


def _check_hourly_report_incomplete_batch(repo: CryptoGuardRepository, batch_id: str | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    # P0-6: also check batches that are not 'running' — a 'success' batch
    # with pending symbols is still incomplete.
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status,
               enabled_symbols_json
        FROM analysis_batches
        ORDER BY started_at DESC
        LIMIT 10
        """
    ).fetchall()
    for row in rows:
        bid = row["batch_id"] if row["batch_id"] else None
        if batch_id and bid != batch_id:
            continue
        enabled = _json_list(row["enabled_symbols_json"])
        # P0-2/6: use batch_symbol_status for accurate counts
        completed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='completed'",
                (bid,),
            ).fetchall()
        ]
        failed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='failed'",
                (bid,),
            ).fetchall()
        ]
        pending_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='pending'",
                (bid,),
            ).fetchall()
        ]
        missing = sorted(set(enabled) - set(completed_syms) - set(failed_syms))
        if missing or pending_syms:
            issues.append(_issue(
                HOURLY_REPORT_INCOMPLETE_BATCH, "warning",
                {
                    "batch_id": bid, "primary_interval": row["primary_interval"],
                    "missing_symbols": missing, "failed_symbols": failed_syms,
                    "pending_symbols": pending_syms,
                },
                "等待批次完成或超时；标记 incomplete 并列 missing/failed/pending symbols",
            ))
    return issues


def _check_hourly_report_stale_decision(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> list[dict[str, Any]]:
    """Flag ga_decisions whose analysis_time is older than one analysis cycle
    when the report renders them as fresh.

    P1-11a: only scan decisions matching the current report batch, not the
    most recent 120 historical records.

    Phase B (07-05): anchor the stale cutoff to ``batch.analysis_time`` when a
    ``batch_id`` is provided so a report rendered at 20:15 for the 19:59:59
    batch does not flag the batch's own decisions as stale. Wall-clock
    ``latest_closed_close_time_ms("15m", utc_ms())`` is only used as a fallback
    when ``batch_id`` is ``None`` or the batch row is absent.
    """
    issues: list[dict[str, Any]] = []
    try:
        from plugins.crypto_guard.utils import latest_closed_close_time_ms, INTERVAL_MS, utc_ms
    except Exception:  # pragma: no cover
        return issues

    # Phase B (07-05): when a batch_id is supplied, anchor the cutoff to the
    # batch's own analysis_time (the authoritative close-time of the 15m
    # candle the report is about). This avoids the "假 stale" bug where a
    # report rendered at 20:15 for the 19:59:59 batch would compute
    # cutoff = 20:14:59.999 from utc_ms() and flag the 19:59:59 decision as
    # 15m1s stale. Only fall back to the wall-clock cutoff when batch_id is
    # None or the batch row cannot be loaded.
    cutoff: int | None = None
    if batch_id:
        try:
            batch_row = repo.conn.execute(
                "SELECT analysis_time FROM analysis_batches WHERE batch_id=? LIMIT 1",
                (batch_id,),
            ).fetchone()
        except Exception:
            batch_row = None
        if batch_row is not None and batch_row["analysis_time"] is not None:
            try:
                cutoff = int(batch_row["analysis_time"])
            except (TypeError, ValueError):
                cutoff = None
    if cutoff is None:
        now_ms = utc_ms()
        cutoff = latest_closed_close_time_ms("15m", now_ms)
    span = INTERVAL_MS["15m"]
    # P1-11a: filter by batch_id if available; otherwise fall back to time window
    if batch_id:
        rows = repo.conn.execute(
            "SELECT id, symbol, analysis_time, signal_grade, batch_id "
            "FROM ga_decisions WHERE batch_id=? ORDER BY id DESC LIMIT 120",
            (batch_id,),
        ).fetchall()
    else:
        min_time = cutoff - span
        rows = repo.conn.execute(
            "SELECT id, symbol, analysis_time, signal_grade, batch_id "
            "FROM ga_decisions WHERE analysis_time >= ? ORDER BY id DESC LIMIT 120",
            (min_time,),
        ).fetchall()
    for r in rows:
        at = int(r["analysis_time"] or 0)
        if at == 0:
            continue
        age_ms = cutoff - at
        # stale if analysis_time is older than one 15m close (REPORTING cutoff)
        if age_ms > span:
            issues.append(_issue(
                HOURLY_REPORT_STALE_DECISION, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "analysis_time": at, "age_minutes": age_ms // 60000,
                    "grade": r["signal_grade"], "batch_id": r["batch_id"],
                    "cutoff": cutoff,
                },
                "该决策超过一个分析周期；不得进入可执行机会分类",
            ))
            # Limit noise: only flag the most recent 50 stale rows per run
            if len([i for i in issues if i["type"] == HOURLY_REPORT_STALE_DECISION]) >= 50:
                break
    return issues


def _check_executable_opportunity_without_trade_plan(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """P1-11b: Only flag decisions that claim create_paper_order or trade_plan_available
    but lack a trade plan — other decisions (monitor_only, etc.) are expected
    to not have one."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, decision,
               trade_plan_json, risk_check_json
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    import json as _json
    for r in rows:
        plan = _safe_json(r["trade_plan_json"])
        if not isinstance(plan, dict) or not plan:
            issues.append(_issue(
                EXECUTABLE_WITHOUT_TRADE_PLAN, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                },
                "评级较高但 trade_plan 缺失；必须降级为观察候选",
            ))
    return issues


def _check_executable_opportunity_risk_rejected(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """P1-11b: Only flag decisions that claim create_paper_order or trade_plan_available
    but risk_check failed — other decisions (monitor_only, etc.) already know."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, risk_check_json
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    for r in rows:
        risk = _safe_json(r["risk_check_json"]) or {}
        if isinstance(risk, dict) and risk.get("ok") is False:
            issues.append(_issue(
                EXECUTABLE_RISK_REJECTED, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                    "reasons": risk.get("reasons") or [],
                },
                "风控未通过对项目禁止说明'可执行/风控全部满足'",
            ))
    return issues


def _check_opportunity_below_confidence(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """FS-5 #4: Only warn when structured state claims executable eligibility.

    A non-executable ``opportunity_watch`` decision (one whose ``decision`` is
    NOT ``create_paper_order`` / ``trade_plan_available``) is expected to be
    below the execution confidence threshold — that is the very reason it is
    classified as watch-only rather than executable. Warning on those rows
    produces noise that obscures real executable-threshold failures.

    Only warn when:
      - ``signal_grade IN ('S','A','B')`` AND
      - ``decision IN ('create_paper_order', 'trade_plan_available')`` AND
      - ``confidence < MIN_CONFIDENCE_FOR_PAPER_ORDER``
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, decision
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    for r in rows:
        conf = float(r["confidence"] or 0)
        if conf < MIN_CONFIDENCE_FOR_PAPER_ORDER:
            issues.append(_issue(
                OPPORTUNITY_BELOW_CONFIDENCE, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "confidence": conf,
                    "threshold": MIN_CONFIDENCE_FOR_PAPER_ORDER,
                    "decision": r["decision"],
                },
                f"置信度 {conf:.2f} 低于 min_confidence {MIN_CONFIDENCE_FOR_PAPER_ORDER:.2f}；不进入可执行",
            ))
    return issues



def _check_summary_execution_state_conflict(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    from plugins.crypto_guard.notify.report_consistency import FORBIDDEN_EXECUTABLE_PHRASES, is_valid_trade_plan
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, final_summary,
               rendered_summary, risk_check_json, trade_plan_json
        FROM ga_decisions
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    for r in rows:
        risk = _safe_json(r["risk_check_json"]) or {}
        plan = _safe_json(r["trade_plan_json"])
        # P1-7 (Round 3): use is_valid_trade_plan instead of simple truthiness
        plan_ok = is_valid_trade_plan(plan)
        risk_ok = bool(isinstance(risk, dict) and risk.get("ok"))
        if risk_ok and plan_ok:
            continue
        text = (r["final_summary"] or "")
        hit = [p for p in FORBIDDEN_EXECUTABLE_PHRASES if p in text]
        rendered = r["rendered_summary"] or ""
        rendered_hit = [p for p in FORBIDDEN_EXECUTABLE_PHRASES if p in rendered]
        if hit or rendered_hit:
            issues.append(_issue(
                SUMMARY_EXECUTION_CONFLICT, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                    "risk_ok": risk_ok, "has_trade_plan": plan_ok,
                    "forbidden_phrases_in_final_summary": hit,
                    "forbidden_phrases_in_rendered_summary": rendered_hit,
                },
                "summary 与结构化字段冲突；deterministic validator 必须覆盖文案",
            ))
    return issues


def _check_excessive_grade_flip(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Detect S→D→S style jumps within the last 4 hours per symbol."""
    issues: list[dict[str, Any]] = []
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - 4 * 3600) * 1000)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, analysis_time, signal_grade, previous_grade
        FROM ga_decisions
        WHERE analysis_time >= ?
        ORDER BY symbol, analysis_time ASC
        """,
        (cutoff_ms,),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append({
            "id": int(r["id"]), "grade": r["signal_grade"],
            "previous_grade": r["previous_grade"], "ts": int(r["analysis_time"]),
        })
    for symbol, seq in by_symbol.items():
        grades = [g["grade"] for g in seq]
        # Detect wild swing: at least one grade S/A followed by D/C within the window.
        saw_top = any(gr in {"S", "A"} for gr in grades)
        saw_bottom = any(gr in {"D", "C"} for gr in grades)
        if saw_top and saw_bottom and len(grades) >= 2:
            issues.append(_issue(
                EXCESSIVE_GRADE_FLIP, "warning",
                {"symbol": symbol, "grade_sequence": grades,
                 "window_hours": 4, "decision_ids": [g["id"] for g in seq]},
                "短时间内高/低评级跳变；按 grade hysteresis 做迟滞并记录 previous_grade",
            ))
    return issues


def _check_direction_flip_without_closed_candle(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag symbol-level direction flips that lack a closed candle
    breakthrough evidence.

    FR-5: Uses structured evidence from the snapshot's module results.
    A valid confirmation requires an event dict with:
      - matching symbol and snapshot
      - event_type in the canonical structural-break set
      - supported non-empty timeframe
      - strict closed status
      - parseable event/close time (seconds, milliseconds, or ISO UTC)
      - event_time after previous decision and not after current decision
      - direction matching the new side
    Text evidence is NEVER accepted.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - 4 * 3600) * 1000)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, analysis_time, market_bias, counter_evidence_json,
               trade_plan_json, evidence_json, snapshot_id
        FROM ga_decisions
        WHERE analysis_time >= ?
        ORDER BY symbol, analysis_time ASC
        """,
        (cutoff_ms,),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append({
            "id": int(r["id"]), "bias": r["market_bias"],
            "ts": int(r["analysis_time"]),
            "counter": _safe_json(r["counter_evidence_json"]) or [],
            "side": (_safe_json(r["trade_plan_json"]) or {}).get("side"),
            "evidence": _safe_json(r["evidence_json"]) or [],
            "snapshot_id": r["snapshot_id"],
            "symbol": r["symbol"],
        })
    for symbol, seq in by_symbol.items():
        for prev, cur in zip(seq, seq[1:]):
            prev_side = prev.get("side") or _bias_side(prev.get("bias"))
            cur_side = cur.get("side") or _bias_side(cur.get("bias"))
            if prev_side and cur_side and prev_side != cur_side:
                # FR-5: structured evidence from snapshot's module results
                structural_confirmation = _has_structured_confirmation(
                    repo, cur, cur_side, prev_ts=prev.get("ts", 0),
                )
                if not structural_confirmation:
                    issues.append(_issue(
                        DIRECTION_FLIP_NO_CLOSED_CANDLE, "warning",
                        {
                            "symbol": symbol, "prev_side": prev_side, "cur_side": cur_side,
                            "prev_decision_id": prev["id"], "cur_decision_id": cur["id"],
                        },
                        "方向翻转必须以已收盘 K 线突破作为证据；缺突破证据时记录诊断",
                    ))
    return issues


# Canonical structural-break event types for direction flip confirmation.
# All upper-case — comparison uses .upper() on the event_type value.
_STRUCTURAL_BREAK_TYPES = frozenset({
    "BOS", "BREAK_OF_STRUCTURE",
    "CHOCH", "CHANGE_OF_CHARACTER",
    "BREAKOUT", "BREAKDOWN",
})

# Supported timeframes for structured evidence
_SUPPORTED_TIMEFRAMES = frozenset({
    "1m", "5m", "15m", "1h", "4h", "1d",
})

# R8 P1 fix: required timeframes for an analysis batch. From
# ``config/scheduler.yaml:analyze_market_15m.timeframes``. A success batch
# whose snapshots only have ``5m`` healthy (missing ``1d/4h/1h/15m``)
# must NOT be judged healthy — the hourly report's multi-TF bias depends
# on all five TFs being ready at ``batch.analysis_time``.
# R9 P2-5 fix: previously this was a hardcoded literal. Now it's loaded
# from config keyed by ``primary_interval`` (with a fallback to the
# 15m default) so changes to ``scheduler.yaml`` propagate automatically.
# A future batch type with a different TF set (e.g. ``primary_interval='1h'``
# with ``timeframes=['1d','4h','1h','15m']``) will use its own required
# set instead of being incorrectly flagged as missing ``5m``.
_REQUIRED_TIMEFRAMES_FALLBACK = frozenset({"1d", "4h", "1h", "15m", "5m"})


def _required_timeframes_for_batch(primary_interval: str | None) -> frozenset[str]:
    """R9 P2-5: load required TFs from ``scheduler.yaml`` keyed by
    ``primary_interval``. Falls back to ``_REQUIRED_TIMEFRAMES_FALLBACK``
    if config is unavailable or the job is not found.

    Why: ``_REQUIRED_TIMEFRAMES_FOR_BATCH`` was a hardcoded literal that
    could drift from ``scheduler.yaml``. If someone changes the config
    to add/remove a TF, the diagnostic would silently become stale.
    Loading from config ensures the diagnostic stays in sync with the
    actual production batch definition.

    R10 Rec fix: narrowed the ``except`` clause from ``Exception`` to
    ``(KeyError, TypeError, AttributeError)`` so a real config error
    (e.g. YAML syntax error, missing file) is NOT silently swallowed.
    Pre-R10 the broad ``except Exception`` could mask a config loading
    failure — the diagnostic would fall back to the hardcoded default
    instead of surfacing the config error at startup. The narrowed
    clause still prevents crashes from expected shapes (missing keys,
    wrong types) but lets unexpected exceptions propagate so they can
    be diagnosed.
    """
    if not primary_interval:
        return _REQUIRED_TIMEFRAMES_FALLBACK
    try:
        from plugins.crypto_guard.config.loader import load_config
        cfg = load_config()
        jobs = cfg.scheduler.get("jobs") or {}
        # scheduler.yaml top-level is ``jobs:`` mapping (loader wraps the
        # full YAML — check both the wrapped and unwrapped shapes for
        # robustness).
        if not jobs:
            jobs = cfg.scheduler.get("analyze_market_15m") or {}
            if jobs:
                jobs = {"analyze_market_15m": jobs}
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            if job.get("task") != "analyze_market":
                continue
            params = job.get("params") or {}
            if params.get("primary_interval") != primary_interval:
                continue
            tfs = params.get("timeframes")
            if isinstance(tfs, list) and tfs:
                return frozenset(str(t) for t in tfs)
        # Fallback: if no matching job found, try the 15m default.
        return _REQUIRED_TIMEFRAMES_FALLBACK
    except (KeyError, TypeError, AttributeError):
        # Defensive: expected-shape errors (missing keys, wrong types)
        # fall back to the default. Unexpected exceptions (e.g. YAML
        # syntax error) propagate so they can be diagnosed at startup.
        return _REQUIRED_TIMEFRAMES_FALLBACK


def _has_structured_confirmation(
    repo: CryptoGuardRepository, cur: dict[str, Any], new_side: str, *, prev_ts: int = 0,
) -> bool:
    """FR-5: Check for structured event confirmation of a direction flip.

    Text evidence is NEVER accepted. A valid confirmation must come from
    structured events associated with the decision's snapshot/module result
    in the database. Inline evidence from ga_decisions.evidence_json /
    counter_evidence_json is NOT trusted — it could be fabricated by the LLM.

    Every accepted event must have:
    - matching symbol and snapshot (looked up from module_analysis_results)
    - event_type in the canonical structural-break set
    - supported non-empty timeframe (from module_analysis_results.timeframe)
    - strict closed status (snapshot events are by definition closed)
    - required parseable event/close time (from module_analysis_results.analysis_time)
    - event_time after previous decision and not after current decision
    - direction matching the new side
    """
    snapshot_id = cur.get("snapshot_id")
    symbol = cur.get("symbol")
    analysis_time = cur.get("ts", 0)

    # FR-5 (P1 fix): ONLY events looked up from the snapshot's module results
    # are accepted. Inline evidence from cur["evidence"]/cur["counter"] is
    # explicitly rejected — it lives in ga_decisions JSON columns that the
    # LLM could populate with arbitrary dicts.
    events = _lookup_snapshot_events(repo, snapshot_id, symbol)

    for event in events:
        # Must have a structural-break event_type (mapped from production shape)
        event_type = event.get("event_type", "")
        if not event_type:
            continue
        if str(event_type).upper() not in _STRUCTURAL_BREAK_TYPES:
            continue

        # FR-5: must have supported non-empty timeframe
        timeframe = str(event.get("timeframe", "")).lower().strip()
        if not timeframe or timeframe not in _SUPPORTED_TIMEFRAMES:
            continue

        # FR-5: strict closed status — snapshot events are by definition on
        # closed candles; explicit closed=False rejects, otherwise accepted.
        closed = event.get("closed", True)
        if closed is not True and str(closed).lower().strip() not in {"true", "1", "yes"}:
            continue

        # FR-5: required parseable event/close time
        event_time = _parse_event_time(event)
        if event_time is None:
            continue

        # FR-5: event_time must be after previous decision and not after current
        event_time_ms = int(event_time)
        if prev_ts > 0 and event_time_ms <= prev_ts:
            continue
        if analysis_time > 0 and event_time_ms > analysis_time:
            continue

        # FR-5: direction must match new side
        direction = str(event.get("direction", "")).lower().strip()
        side_lower = new_side.lower()
        direction_match = (
            (side_lower == "long" and direction in {"bullish", "long", "up", "多"})
            or (side_lower == "short" and direction in {"bearish", "short", "down", "空"})
        )
        if direction_match:
            return True

    return False


# Production modules that emit structural-break events. The legacy
# `smc_orderflow` module has 0 rows in production — real modules are
# `price_action` (with `structure_events` list) and `smc`.
_SNAPSHOT_EVENT_MODULES: tuple[str, ...] = ("price_action", "smc", "smc_orderflow")

# Map production event names to canonical structural-break types.
# Production `price_action.structure_events[].event` uses names like
# `bullish_bos`, `bearish_choch`. The `type` field is `BOS`/`CHoCH`/`none`.
_EVENT_NAME_TO_TYPE: dict[str, str] = {
    "bullish_bos": "BOS",
    "bearish_bos": "BOS",
    "bullish_choch": "CHOCH",
    "bearish_choch": "CHOCH",
    "bullish_breakout": "BREAKOUT",
    "bearish_breakout": "BREAKOUT",
    "bullish_breakdown": "BREAKDOWN",
    "bearish_breakdown": "BREAKDOWN",
}


def _normalize_snapshot_event(raw: dict[str, Any], *, timeframe: str, analysis_time: int) -> dict[str, Any] | None:
    """FS-1 / FR-5: Map a production-shape event dict to the canonical shape.

    Production `price_action.structure_events` rows emitted by
    ``price_action_engine._structure_events()`` look like:
        {"event": "bullish_bos", "type": "BOS", "event_type": "BOS",
         "direction": "bullish", "timeframe": "1h",
         "reference_high": 6.267, "reference_low": 5.957, "close": 6.308,
         "close_time": <source candle close_time>, "closed": True}

    Canonical shape required by ``_has_structured_confirmation``:
        {"event_type": "BOS", "timeframe": "1h", "closed": True,
         "time": <close_time_ms>, "direction": "bullish"}

    FS-1: The event time MUST come from the source event's ``close_time``
    (the actual candle close time written by ``price_action_engine``). The
    module row's ``analysis_time`` MUST NOT be used as an event-time
    fallback — module analysis time is when the analyzer ran, not when the
    candle closed. Repeated analysis of the same higher-timeframe candle
    therefore retains the same event time.

    FS-1: The ``closed`` flag MUST come from the source event. It MUST NOT
    be invented as ``True`` when the source event does not prove it.
    """
    event_name = str(raw.get("event", "")).lower().strip()
    # Direct event_type field wins if present
    direct_type = raw.get("event_type")
    if direct_type:
        event_type = str(direct_type).upper()
    elif event_name in _EVENT_NAME_TO_TYPE:
        event_type = _EVENT_NAME_TO_TYPE[event_name]
    else:
        # Fall back to the `type` field (BOS / CHoCH / none)
        type_field = str(raw.get("type", "")).upper().strip()
        if not type_field or type_field == "NONE":
            return None
        event_type = type_field

    # Derive direction from event_name prefix or explicit direction field
    direction = ""
    if event_name.startswith("bullish") or event_name.startswith("bos_bull") or event_name.startswith("choch_bull"):
        direction = "bullish"
    elif event_name.startswith("bearish") or event_name.startswith("bos_bear") or event_name.startswith("choch_bear"):
        direction = "bearish"
    elif raw.get("direction"):
        direction = str(raw.get("direction")).lower().strip()

    # FS-1: event time MUST come from the source event. NEVER fall back to
    # module analysis_time — that is when the analyzer ran, not when the
    # candle closed.
    event_time = raw.get("close_time")
    if event_time is None:
        event_time = raw.get("time")
    if event_time is None:
        event_time = raw.get("event_time")
    if event_time is None:
        # FS-1: no real event time — reject instead of substituting analysis_time
        return None

    # FS-1: closed flag MUST come from the source event. NEVER invent True.
    closed_raw = raw.get("closed")
    if closed_raw is None:
        # Source event did not prove closed status — reject.
        return None
    if closed_raw is True or str(closed_raw).lower().strip() in {"true", "1", "yes"}:
        closed = True
    else:
        # Explicit closed=False — reject.
        return None

    return {
        "event_type": event_type,
        "timeframe": timeframe,
        "closed": closed,
        "time": event_time,
        "direction": direction,
    }


def _lookup_snapshot_events(
    repo: CryptoGuardRepository, snapshot_id: int | None, symbol: str,
) -> list[dict[str, Any]]:
    """FR-5: Look up structured events from the snapshot's module results.

    Production modules with structural-break events:
    - `price_action`: result_json.structure_events (list of dicts)
    - `smc`: result_json.events / structure_breaks (list of dicts)
    - `smc_orderflow`: legacy module (0 rows in production as of 2026-06-29)

    Each returned event is normalized to the canonical shape with event_type,
    timeframe, closed, time, direction fields populated from the module row.
    """
    if snapshot_id is None:
        return []
    try:
        rows = repo.conn.execute(
            """
            SELECT module, timeframe, analysis_time, result_json
            FROM module_analysis_results
            WHERE snapshot_id=? AND symbol=? AND module IN (?, ?, ?)
            ORDER BY timeframe
            """,
            (int(snapshot_id), symbol, *_SNAPSHOT_EVENT_MODULES),
        ).fetchall()
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for row in rows:
        result = _safe_json(row["result_json"]) or {}
        if not isinstance(result, dict):
            continue
        timeframe = str(row["timeframe"] or "").lower().strip()
        analysis_time = int(row["analysis_time"] or 0)
        # price_action: structure_events list; smc: events/structure_breaks
        items: list[Any] = []
        for key in ("structure_events", "events", "structure_breaks", "breakouts", "breakdowns"):
            v = result.get(key)
            if isinstance(v, list):
                items.extend(v)
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_snapshot_event(item, timeframe=timeframe, analysis_time=analysis_time)
            if normalized is not None:
                events.append(normalized)
    return events


def _parse_event_time(event: dict[str, Any]) -> int | None:
    """FR-5: Parse event time from seconds, milliseconds, or ISO UTC.

    Returns milliseconds since epoch, or None if unparseable.
    Threshold: values < 1e12 are seconds, >= 1e12 are milliseconds.
    """
    # Try multiple time field names
    for field in ("event_time", "close_time", "time", "timestamp", "candle_close_time"):
        raw = event.get(field)
        if raw is None:
            continue

        # Integer/float: distinguish seconds from milliseconds
        if isinstance(raw, (int, float)):
            val = int(raw)
            if val <= 0:
                continue
            if val < 1_000_000_000_000:
                # Seconds — convert to milliseconds
                return val * 1000
            else:
                # Already milliseconds
                return val

        # String: try ISO UTC format
        if isinstance(raw, str):
            raw_str = raw.strip()
            if not raw_str:
                continue
            try:
                dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except (ValueError, TypeError):
                # Try as numeric string
                try:
                    val = int(float(raw_str))
                    if val <= 0:
                        continue
                    if val < 1_000_000_000_000:
                        return val * 1000
                    else:
                        return val
                except (ValueError, TypeError):
                    continue

    return None


def _check_invalid_liquidity_sweep_semantics(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Only used in unit-test contexts: validate smc_engine direction semantics
    on a known candle sequence. The diagnostic runner asserts the mapping is
    exercised (sweep_low → sell-side → bullish) by querying recent ga_decisions'
    smc evidence for inconsistent wording."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, final_summary, raw_decision_json
        FROM ga_decisions
        ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    for r in rows:
        text = (r["final_summary"] or "")
        # P1-11d: sell_side sweep sweeps low (price goes down) which is
        # the NORMAL direction for a sell-side sweep. "向下" (downward)
        # with sell_side is correct behavior — only flag explicit direction
        # contradictions like "看空" (bearish conviction) with sell_side.
        # Similarly, buy_side sweeps high ("向上" = upward) is normal —
        # only flag "看多" (bullish conviction) with buy_side.
        if "sell_side" in text and ("看空" in text or "bearish" in text.lower()):
            issues.append(_issue(
                INVALID_LIQUIDITY_SWEEP, "warning",
                {"decision_id": int(r["id"]), "symbol": r["symbol"], "snippet": text[:200]},
                "sell_side liquidity sweep 应映射 bullish reclaim；勿反向解读",
            ))
        elif "buy_side" in text and ("看多" in text or "bullish" in text.lower()):
            issues.append(_issue(
                INVALID_LIQUIDITY_SWEEP, "warning",
                {"decision_id": int(r["id"]), "symbol": r["symbol"], "snippet": text[:200]},
                "buy_side liquidity sweep 应映射 bearish reclaim；勿反向解读",
            ))
    return issues


def _check_negative_drawdown_display(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag paper_equity_snapshots whose stored drawdown_percent is positive
    when account equity is below initial — internal sign convention must be
    <= 0 (loss negative)."""
    issues: list[dict[str, Any]] = []
    # Get initial_balance from paper_accounts for relative comparison
    try:
        init_row = repo.conn.execute(
            "SELECT initial_balance FROM paper_accounts ORDER BY id LIMIT 1"
        ).fetchone()
        initial_balance = float(init_row["initial_balance"]) if init_row and init_row["initial_balance"] else 0.0
    except Exception:
        initial_balance = 0.0
    rows = repo.conn.execute(
        """
        SELECT id, account_equity, unrealized_pnl, realized_pnl, snapshot_json
        FROM paper_equity_snapshots
        ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    import json as _json
    for r in rows:
        snap = _safe_json(r["snapshot_json"]) or {}
        dd = snap.get("drawdown_percent")
        if dd is None:
            continue
        try:
            dd = float(dd)
        except (TypeError, ValueError):
            continue
        # positive drawdown_display while equity shows loss below initial is the bug pattern
        equity = float(r["account_equity"] or 0)
        is_loss = equity < initial_balance if initial_balance > 0 else equity < 10000
        if dd > 0 and is_loss:
            issues.append(_issue(
                NEGATIVE_DRAWDOWN_DISPLAY, "warning",
                {"snapshot_id": int(r["id"]), "account_equity": equity,
                 "initial_balance": initial_balance,
                 "drawdown_percent_internal": dd},
                "对外显示回撤需统一为非负幅度；内部保留 sign 语义",
            ))
    return issues


# ── Phase E (07-03): semantic-accuracy diagnostics ──────────────────────────
# Five new checks + a marker-missing check. Each uses the independent
# ``hourly_market_semantic_accuracy_contract_v1`` marker as the cutoff
# between ``legacy_info`` (pre-marker) and ``error`` / ``warning``
# (post-marker), applied by ``_apply_semantic_marker_cutoff`` after all
# checks have run.

# Directional vs non-directional bias/stage enums (mirrors market_semantics).
_NON_DIRECTIONAL_BIAS_E: frozenset[str] = frozenset({"neutral", "mixed", "unknown"})
_DIRECTIONAL_STAGE_E: frozenset[str] = frozenset({"early", "middle", "late"})

# Phrases that indicate a summary is gate-only (no market context).
_GATE_ONLY_PHRASES: tuple[str, ...] = (
    "交易计划尚未形成",
    "缺少有效交易计划",
    "尚未形成交易计划",
)

# Market-context phrases that, if present in the summary, exempt it from the
# observation_reason_missing_market_context check.
_MARKET_CONTEXT_PHRASES: tuple[str, ...] = (
    "日线", "4H", "4h", "1H", "1h", "15M", "15m", "高周期", "偏空", "偏多",
    "偏热", "追价", "震荡", "反弹", "反趋势", "冲突", "混合", "趋势",
    "结构", "动量", "突破", "回踩", "空头", "多头", "压力", "支撑",
)


def _check_semantic_contract_markers_missing(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag missing semantic-accuracy AND R4 contract markers.

    Both markers must exist in ``_migration_state``. If either is absent,
    emit an ``error`` issue whose ``type`` contains "marker" and whose
    ``suggested_action`` contains "未部署" so callers can detect the missing
    contract rather than receiving a silently-healthy report.
    """
    issues: list[dict[str, Any]] = []
    required_markers = (
        (R4_CONTRACT_MARKER_KEY, "R4"),
        (SEMANTIC_ACCURACY_MARKER_KEY, "semantic-accuracy"),
    )
    for key, label in required_markers:
        try:
            row = repo.conn.execute(
                "SELECT applied_at FROM _migration_state WHERE key=? LIMIT 1",
                (key,),
            ).fetchone()
        except Exception:
            row = None
        if not row or not row["applied_at"]:
            issues.append(_issue(
                SEMANTIC_CONTRACT_MARKER_MISSING, "error",
                {
                    "marker_key": key,
                    "contract": label,
                    "issue": "marker_absent",
                },
                f"{label} contract marker 未部署。运行 initialize_database() 部署 marker；"
                f"marker 缺失时语义诊断被跳过，可能导致假绿。",
            ))
    return issues


def _check_bias_stage_semantic_conflict(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag ga_decisions where market_bias is non-directional
    (neutral/mixed/unknown) but trend_stage is directional (early/middle/late).

    Per design §4 / PRD FR-2, this combination is illegal and must be
    corrected at the normalization layer. The diagnostic surfaces rows that
    slipped through (e.g. legacy data or a faulty fixture) so they can be
    audited. Severity: ``error`` post-marker, ``legacy_info`` pre-marker
    (applied by ``_apply_semantic_marker_cutoff``).
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, market_bias, trend_stage, created_at
        FROM ga_decisions
        WHERE market_bias IN ('neutral', 'mixed', 'unknown')
          AND trend_stage IN ('early', 'middle', 'late')
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    for r in rows:
        bias = str(r["market_bias"] or "").lower()
        stage = str(r["trend_stage"] or "").lower()
        if bias in _NON_DIRECTIONAL_BIAS_E and stage in _DIRECTIONAL_STAGE_E:
            issues.append(_issue(
                BIAS_STAGE_SEMANTIC_CONFLICT, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "market_bias": bias, "trend_stage": stage,
                },
                "bias+stage 语义冲突：非方向性 bias 不得与方向性 stage 组合；"
                "必须在归一化层修正为 range/transition/unknown。",
            ))
    return issues


def _check_htf_countertrend_overconfidence(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag ga_decisions where 1D bias opposes 1H/15M bias in the
    raw_decision_json snapshot profiles AND confidence >= the paper-order
    threshold.

    Per PRD FR-3, a countertrend rebound must not reach executable-level
    confidence purely on 15M/1H momentum. The fault test seeds
    ``raw_decision_json.snapshot.profiles`` with ``1d.market_structure=bearish``
    + ``1h.market_structure=bullish`` and ``confidence=0.85``.

    Detection parses ``raw_decision_json`` (the structured audit JSON), NOT
    the free-text ``final_summary``. Severity: ``error`` post-marker.

    Production ``raw_decision_json`` stores ``timeframe_context`` at the top
    level (set by ``controller_decision_from_legacy`` from the normalized
    legacy decision). Older rows or test fixtures may nest the data under
    ``snapshot.profiles`` or ``raw_legacy_decision.snapshot.profiles`` —
    fall back to those shapes so the diagnostic stays robust.
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, market_bias,
               raw_decision_json, created_at
        FROM ga_decisions
        WHERE confidence >= ?
        ORDER BY id DESC LIMIT 200
        """,
        (MIN_CONFIDENCE_FOR_PAPER_ORDER,),
    ).fetchall()
    for r in rows:
        conf = float(r["confidence"] or 0)
        if conf < MIN_CONFIDENCE_FOR_PAPER_ORDER:
            continue
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        # Production path: top-level timeframe_context with 1d/4h/1h/15m
        # entries carrying {bias, structure, closed}.
        tf_ctx = raw.get("timeframe_context")
        if isinstance(tf_ctx, dict) and tf_ctx:
            bias_1d = _tf_ctx_bias(tf_ctx.get("1d"))
            bias_4h = _tf_ctx_bias(tf_ctx.get("4h"))
            bias_1h = _tf_ctx_bias(tf_ctx.get("1h"))
            bias_15m = _tf_ctx_bias(tf_ctx.get("15m"))
        else:
            # Legacy / fixture path: snapshot.profiles (or nested under
            # raw_legacy_decision.snapshot.profiles for rows written
            # before the Phase E controller fix).
            snapshot = raw.get("snapshot")
            if not isinstance(snapshot, dict):
                legacy = raw.get("raw_legacy_decision")
                if isinstance(legacy, dict):
                    snapshot = legacy.get("snapshot") or {}
            if not isinstance(snapshot, dict):
                continue
            profiles = snapshot.get("profiles") or {}
            if not isinstance(profiles, dict):
                continue
            bias_1d = _profile_structure_bias(profiles.get("1d"))
            bias_4h = _profile_structure_bias(profiles.get("4h"))
            bias_1h = _profile_structure_bias(profiles.get("1h"))
            bias_15m = _profile_structure_bias(profiles.get("15m"))
        if not bias_1d or bias_1d not in {"bullish", "bearish"}:
            continue
        opposite = "bullish" if bias_1d == "bearish" else "bearish"
        low_tf_opposite = opposite in {bias_1h, bias_15m}
        if not low_tf_opposite:
            continue
        # 4H must NOT confirm the low-TF direction to count as a conflict.
        if bias_4h == opposite:
            continue
        issues.append(_issue(
            HTF_COUNTERTREND_OVERCONFIDENCE, "error",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "confidence": conf,
                "threshold": MIN_CONFIDENCE_FOR_PAPER_ORDER,
                "1d_bias": bias_1d, "1h_bias": bias_1h,
                "15m_bias": bias_15m, "4h_bias": bias_4h,
            },
            "高周期空头中的低周期反弹不得获得执行级置信度；"
            "htf_conflict 须触发置信度上限并降级。",
        ))
    return issues


def _tf_ctx_bias(entry: Any) -> str:
    """Read a directional bias from a timeframe_context entry.

    Each entry has {bias, structure, closed}. Prefers the explicit ``bias``
    field; falls back to ``structure`` when it carries a directional value.
    Returns "" for non-directional or missing entries.
    """
    if not isinstance(entry, dict):
        return ""
    bias = str(entry.get("bias") or "").lower()
    if bias in {"bullish", "bearish"}:
        return bias
    struct = str(entry.get("structure") or "").lower()
    if struct in {"bullish", "bearish"}:
        return struct
    return ""


def _profile_structure_bias(profile: Any) -> str:
    """Read a directional bias from a snapshot profile dict.

    Prefers an explicit ``bias`` field; falls back to ``market_structure``
    when it carries a directional value (bullish/bearish). Returns "" for
    non-directional or missing profiles. This reads structured audit JSON,
    not LLM free text.
    """
    if not isinstance(profile, dict):
        return ""
    bias = str(profile.get("bias") or "").lower()
    if bias in {"bullish", "bearish"}:
        return bias
    struct = str(profile.get("market_structure") or "").lower()
    if struct in {"bullish", "bearish"}:
        return struct
    return ""


def _check_summary_structured_state_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag ga_decisions whose ``final_summary`` / ``rendered_summary``
    text mentions a grade letter (S/A/B 级) that disagrees with the structured
    ``signal_grade``.

    Per PRD FR-5, the persisted summary must be the canonical deterministic
    summary and must not contradict the structured grade. The fault test
    seeds ``grade=B`` with ``final_summary="A 级 具备模拟盘条件"``.

    This is a regex match on the summary *text* to detect inconsistent
    statements — it is NOT inferring structured state from text. Severity:
    ``error`` post-marker.

    R1-6 (07-03 final review): also rebuild the canonical summary from the
    structured fields in ``raw_decision_json`` and compare against the
    persisted ``final_summary`` / ``rendered_summary``. Any drift is flagged
    as ``canonical_summary_drift`` / ``rendered_summary_drift``. Missing
    structured fields (timeframe_context/alignment/htf_conflict/
    market_reason_codes) are flagged as ``missing_structured_field``. This
    closes the gap where the regex miss non-grade-text drift (e.g. plain
    summary text that diverges from the structured bias/stage).
    """
    import re as _re
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, market_bias, trend_stage, decision,
               confidence, analysis_time, final_summary, rendered_summary,
               raw_decision_json, created_at
        FROM ga_decisions
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    # Match grade letters with optional Chinese "级" suffix or ASCII space.
    # We look for explicit grade-letter mentions like "A 级", "S级", "B 级".
    grade_pattern = _re.compile(r"([SABCD])\s*级")
    from plugins.crypto_guard.reasoning.summary_builder import (
        build_canonical_market_summary,
    )
    for r in rows:
        grade = str(r["signal_grade"] or "").upper().strip()
        if not grade:
            continue
        for field_name in ("final_summary", "rendered_summary"):
            text = r[field_name] or ""
            if not text:
                continue
            mentioned = grade_pattern.findall(text)
            # Filter to the canonical grade letters we care about.
            mentioned = [m for m in mentioned if m in {"S", "A", "B", "C", "D"}]
            if not mentioned:
                continue
            if grade not in mentioned:
                issues.append(_issue(
                    SUMMARY_STRUCTURED_STATE_MISMATCH, "error",
                    {
                        "decision_id": int(r["id"]), "symbol": r["symbol"],
                        "structured_grade": grade,
                        "summary_grades_mentioned": mentioned,
                        "field": field_name,
                        "snippet": text[:200],
                    },
                    "final_summary/rendered_summary 与 signal_grade 不一致；"
                    "必须使用 canonical deterministic summary 覆盖文案。",
                ))
                break

        # R1-6: canonical drift detection via rebuild. Rebuild canonical
        # from the structured fields in raw_decision_json and compare.
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        rebuilt = {
            "symbol": r["symbol"],
            "analysis_time_utc": int(r["analysis_time"] or 0),
            "signal_grade": r["signal_grade"],
            "market_bias": r["market_bias"],
            "trend_stage": r["trend_stage"],
            "decision": r["decision"],
            "confidence": r["confidence"],
            "timeframe_context": raw.get("timeframe_context"),
            "alignment": raw.get("alignment"),
            "htf_conflict": raw.get("htf_conflict"),
            "market_reason_codes": raw.get("market_reason_codes"),
            "risk_check": raw.get("risk_check"),
            "trade_plan": raw.get("trade_plan"),
            "has_trade_plan": raw.get("has_trade_plan"),
            "opportunity_watch": raw.get("opportunity_watch"),
        }
        # Missing structured field check
        missing_fields = [
            fname for fname in (
                "timeframe_context", "alignment", "htf_conflict",
                "market_reason_codes",
            )
            if rebuilt.get(fname) is None
        ]
        if missing_fields:
            issues.append(_issue(
                MISSING_STRUCTURED_FIELD, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "missing_fields": missing_fields,
                },
                "结构化字段缺失：raw_decision_json 缺少 "
                f"{missing_fields}，无法重建 canonical summary。",
            ))
            continue  # cannot rebuild canonical reliably
        try:
            recomputed = build_canonical_market_summary(rebuilt)
        except Exception:
            continue
        final_text = (r["final_summary"] or "")
        rendered_text = (r["rendered_summary"] or "")
        if final_text and final_text != recomputed:
            issues.append(_issue(
                CANONICAL_SUMMARY_DRIFT, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "persisted_final_summary": final_text[:200],
                    "recomputed_canonical": recomputed[:200],
                },
                "final_summary 与重算 canonical summary 不一致；"
                "必须使用 build_canonical_market_summary 覆盖。",
            ))
        if rendered_text and final_text and rendered_text != final_text:
            issues.append(_issue(
                RENDERED_SUMMARY_DRIFT, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "final_summary": final_text[:200],
                    "rendered_summary": rendered_text[:200],
                },
                "rendered_summary 与 final_summary 不一致；"
                "R1-5 要求两者都等于 canonical。",
            ))
    return issues


def _check_observation_reason_missing_market_context(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag observation decisions whose summary is ONLY gate
    terminology (e.g. "交易计划尚未形成") with no market context.

    Per PRD FR-4, the observation reason must explain the market, not only
    the gate. The fault test seeds ``market_bias=bullish, trend_stage=middle,
    final_summary="交易计划尚未形成"``. Severity: ``warning`` per design §7
    (the diagnostic must surface the gap, but it is not a hard error).
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, market_bias, trend_stage,
               final_summary, rendered_summary, created_at
        FROM ga_decisions
        WHERE decision IN ('monitor_only', 'opportunity_watch', 'no_edge',
                           'watch_only', 'add_to_watchlist', 'ignore')
           OR decision NOT IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    for r in rows:
        text = (r["final_summary"] or "") + " " + (r["rendered_summary"] or "")
        text = text.strip()
        if not text:
            continue
        has_gate_only = any(p in text for p in _GATE_ONLY_PHRASES)
        if not has_gate_only:
            continue
        has_market_context = any(p in text for p in _MARKET_CONTEXT_PHRASES)
        if has_market_context:
            continue
        issues.append(_issue(
            OBSERVATION_REASON_MISSING_MARKET_CONTEXT, "warning",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "decision": r["decision"],
                "market_bias": r["market_bias"], "trend_stage": r["trend_stage"],
                "snippet": (r["final_summary"] or "")[:200],
            },
            "观察原因缺少市场上下文：'交易计划尚未形成' 不得成为唯一解释；"
            "必须先写市场原因（多周期冲突/趋势后段/结构未确认等）。",
        ))
    return issues


def _check_no_edge_reason_coverage_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase E: flag C/D no_edge batches where >3 rows exist but some rows
    have empty reason text, so the renderer cannot label "前 N 项，另有 M 项".

    Per PRD FR-6, when the C/D list is truncated the report must explicitly
    label "重点原因（前 N 项，另有 M 项）". The label is added at render time
    by ``_format_cd_reasons`` (Phase D), so it is never persisted in
    ``rendered_summary``. The data-level fault this diagnostic can catch is:
    a C/D batch with >3 rows where some rows have empty reason text, meaning
    the renderer has nothing to truncate and the user sees incomplete reasons.
    The fault test seeds 6 no_edge rows with empty ``final_summary`` and
    ``rendered_summary``.

    Detection: group recent C/D no_edge rows by a 15-minute time bucket
    (the primary report interval). For each bucket with >3 rows, verify every
    row has non-empty reason text. Flag the bucket when any row is missing a
    reason. Severity: ``warning`` per design §7.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, batch_id,
               final_summary, rendered_summary, analysis_time, created_at
        FROM ga_decisions
        WHERE signal_grade IN ('C', 'D')
           OR decision = 'no_edge'
        ORDER BY id DESC LIMIT 300
        """
    ).fetchall()
    # Group by a 15-minute time bucket (900000 ms). This mirrors how the
    # hourly report renders C/D symbols — by time window, not by exact
    # batch_id string. Tight-loop test seeding that spans a few milliseconds
    # still lands in the same bucket.
    _BUCKET_MS = 900_000
    by_bucket: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        at = int(r["analysis_time"] or 0)
        bucket = at // _BUCKET_MS if at > 0 else 0
        by_bucket.setdefault(bucket, []).append({
            "id": int(r["id"]), "symbol": r["symbol"],
            "grade": r["signal_grade"], "decision": r["decision"],
            "batch_id": r["batch_id"] or "",
            "final_summary": r["final_summary"] or "",
            "rendered_summary": r["rendered_summary"] or "",
            "analysis_time": at,
        })
    for bucket, group in by_bucket.items():
        if len(group) <= 3:
            continue
        # The "前 N 项" truncation label is added at render time by
        # _format_cd_reasons whenever the reason count exceeds max_items.
        # The label is never persisted in rendered_summary. So the data-level
        # fault we can detect is: rows with empty reason text, which leaves
        # the renderer with nothing to truncate or label.
        missing_reasons = [
            i for i in group
            if not (i["rendered_summary"] or "").strip()
            and not (i["final_summary"] or "").strip()
        ]
        if missing_reasons:
            issues.append(_issue(
                NO_EDGE_REASON_COVERAGE_MISMATCH, "warning",
                {
                    "batch_id": group[0]["batch_id"] if group else "",
                    "time_bucket": bucket,
                    "no_edge_count": len(group),
                    "missing_reason_count": len(missing_reasons),
                    "missing_symbols": [i["symbol"] for i in missing_reasons],
                    "symbols": [i["symbol"] for i in group],
                    "analysis_time": group[0]["analysis_time"] if group else 0,
                },
                "C/D 原因覆盖不一致：>3 个 no_edge 品种中存在空原因行；"
                "报告渲染必须为每个 C/D 品种提供非空原因并由 _compact_items 标注截断。",
            ))
    return issues


# ── helpers ─────────────────────────────────────────────────────────────────
def _json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        import json
        data = json.loads(raw)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def _bias_side(bias: str | None) -> str | None:
    b = (bias or "").lower()
    if b in ("bullish", "long", "多"):
        return "LONG"
    if b in ("bearish", "short", "空"):
        return "SHORT"
    return None


# ── Phase H (07-05): decision-context-continuity contract diagnostics ──


def _check_plan_lifecycle_contract_markers_missing(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H: flag missing decision-context-continuity contract marker.

    Mirrors the semantic-accuracy marker-missing check. The marker
    ``hourly_decision_context_continuity_contract_v1`` must exist in
    ``_migration_state``. If absent, emit an ``error`` issue so callers
    can detect the missing contract rather than receiving a silently-
    healthy report. When the marker is absent, the seven Phase A-G
    contract checks skip themselves (no historical data is flagged).
    """
    issues: list[dict[str, Any]] = []
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=? LIMIT 1",
            (CONTINUITY_CONTRACT_MARKER_KEY,),
        ).fetchone()
    except Exception:
        row = None
    if not row or not row["applied_at"]:
        issues.append(_issue(
            PLAN_LIFECYCLE_CONTRACT_MARKER_MISSING, "error",
            {
                "marker_key": CONTINUITY_CONTRACT_MARKER_KEY,
                "contract": "decision-context-continuity",
                "issue": "marker_absent",
            },
            "decision-context-continuity contract marker 未部署。运行 "
            "initialize_database() 部署 marker；marker 缺失时 Phase A-G "
            "契约诊断被跳过，可能导致假绿。",
        ))
    return issues


def _check_missing_candidate_on_llm_failure(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase E contract): flag ga_decisions where
    ``llm_status="failed"`` but ``candidate_trade_plan`` is missing.

    Per PRD FR-4 / Phase E, when the LLM fails the deterministic candidate
    plan must be preserved under ``candidate_trade_plan`` for audit. The
    controller's fail-closed path sets ``plan_status="withheld"`` and
    stashes the candidate. A missing candidate on LLM failure means the
    audit trail is broken — the report cannot display "候选计划已生成但被
    LLM failure + grade hysteresis 阻断" (PRD Fact 4).

    Detection parses ``raw_decision_json`` for the top-level
    ``candidate_trade_plan`` and ``llm_status`` fields. Severity:
    ``error`` post-marker, ``legacy_info`` pre-marker.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 500
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        llm_status = str(raw.get("llm_status") or "ok").lower()
        if llm_status not in {"failed", "disabled"}:
            continue
        # P1-8 (07-05 final review): the previous logic fired whenever
        # LLM failed AND candidate_trade_plan was missing, but in the
        # real production path a low-score / no-edge decision legitimately
        # has NO deterministic candidate (``plan_status="no_plan"``) — the
        # LLM never had anything to fail over. Only ``plan_status="withheld"``
        # means "we had a candidate and blocked it", which requires
        # candidate_trade_plan to be present for audit. ``plan_status="no_plan"``
        # is the legitimate no-candidate path; do not flag it.
        plan_status = str(raw.get("plan_status") or "").lower()
        if plan_status == "no_plan":
            # Low-score / no-edge decision: deterministic SOP did not
            # produce a candidate. LLM failure is irrelevant here.
            continue
        if plan_status not in {"withheld", "executable"}:
            # Unknown / legacy plan_status — be conservative and skip.
            # Re-evaluate if a new plan_status value is added.
            continue
        candidate = raw.get("candidate_trade_plan")
        if candidate is None:
            # PRD FR-4: when LLM fails AND a candidate was expected
            # (plan_status=withheld or executable), the deterministic
            # candidate plan MUST be preserved under candidate_trade_plan
            # for audit. controller_decision_from_legacy does not persist
            # has_trade_plan at the top level, so we cannot rely on it
            # here. The candidate alone is the audit anchor.
            issues.append(_issue(
                MISSING_CANDIDATE_ON_LLM_FAILURE, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                    "llm_status": llm_status,
                    "plan_status": plan_status,
                },
                "LLM 失败且 plan_status=" + plan_status + " 但 candidate_trade_plan 缺失："
                "候选计划未保留为审计字段；controller 必须在 fail-closed 路径保留 deterministic candidate。",
            ))
    return issues


def _check_withheld_without_blockers(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase E contract): flag ga_decisions where
    ``plan_status="withheld"`` but ``plan_blockers`` is empty.

    Per PRD FR-4 / Phase E, a withheld plan must carry structured
    ``plan_blockers`` so the report can identify the real blocking stage
    (LLM parse / grade hysteresis / risk rejection / continuity
    invalidated). An empty blockers list means the report cannot fulfill
    PRD FR-8 ("观察项不得统一退化为'交易计划尚未形成'").

    Detection parses ``raw_decision_json`` for the top-level
    ``plan_status`` and ``plan_blockers`` fields. Severity: ``warning``
    post-marker (the plan is correctly withheld, but the reason is missing),
    ``legacy_info`` pre-marker.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 500
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        plan_status = str(raw.get("plan_status") or "").lower()
        if plan_status != "withheld":
            continue
        blockers = raw.get("plan_blockers")
        if isinstance(blockers, list) and len(blockers) > 0:
            continue
        issues.append(_issue(
            WITHHELD_WITHOUT_BLOCKERS, "warning",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "decision": r["decision"],
                "plan_status": plan_status,
            },
            "plan_status=withheld 但 plan_blockers 为空：报告无法指明真实阻断阶段；"
            "必须填入 llm_parse_failed / grade_hysteresis / risk_rejected / "
            "continuity_invalidated 等 reason code。",
        ))
    return issues


def _check_missing_analysis_continuity(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase D contract): flag ga_decisions that lack the
    ``analysis_continuity`` block on the persisted raw_decision_json.

    Per PRD FR-3 / Phase D, every decision must carry the previous-round
    compact state and the structured delta (grade/bias/stage change,
    trigger_progress). A missing continuity block means the LLM prompt
    lacked prior context and the deterministic continuity gate could not
    consume confirmed/invalidated trigger status.

    Detection parses ``raw_decision_json`` for the top-level
    ``analysis_continuity`` field. The check skips the very first analysis
    of a symbol (no prior row exists) by joining against analysis_states
    — but for simplicity here, any row missing the block is flagged;
    the controller always sets a sentinel ``analysis_continuity.previous=None``
    when no prior state exists, so absence is always a defect.

    Severity: ``warning`` post-marker (the decision may still be correct,
    but the audit trail is incomplete), ``legacy_info`` pre-marker.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json,
               analysis_time, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 500
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        continuity = raw.get("analysis_continuity")
        if isinstance(continuity, dict) and continuity:
            continue
        issues.append(_issue(
            MISSING_ANALYSIS_CONTINUITY, "warning",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "decision": r["decision"],
                "analysis_time": int(r["analysis_time"] or 0),
            },
            "analysis_continuity 块缺失：上一轮状态与 delta 未进入本轮审计；"
            "controller 必须在 persistence 前调用 build_analysis_continuity。",
        ))
    return issues


def _check_oversized_feature_pack(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase C contract): flag ga_decisions where the persisted
    ``multi_timeframe_feature_pack`` exceeds the size budget.

    Per PRD FR-2 / Phase C, the feature pack is bounded to 24 KiB by the
    builder. The persisted payload may exceed this if the builder regresses
    or a downstream consumer attaches extra fields. An oversized pack
    means the LLM prompt may have received more than the budgeted context,
    violating the "no raw candle arrays to LLM" constraint.

    Detection parses ``raw_decision_json`` for the top-level
    ``multi_timeframe_feature_pack`` and measures its serialized JSON size.
    Severity: ``error`` post-marker, ``legacy_info`` pre-marker.
    """
    import json as _json
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 500
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        pack = raw.get("multi_timeframe_feature_pack")
        if pack is None:
            # Phase C not yet deployed on this row — skip, not a defect.
            continue
        try:
            serialized = _json.dumps(pack, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        size_bytes = len(serialized.encode("utf-8"))
        if size_bytes <= FEATURE_PACK_SIZE_BUDGET_BYTES:
            continue
        issues.append(_issue(
            OVERSIZED_FEATURE_PACK, "error",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "decision": r["decision"],
                "size_bytes": size_bytes,
                "budget_bytes": FEATURE_PACK_SIZE_BUDGET_BYTES,
            },
            f"multi_timeframe_feature_pack 体积超预算：{size_bytes} > "
            f"{FEATURE_PACK_SIZE_BUDGET_BYTES} bytes；builder 必须在 24 KiB 内"
            "裁剪 verbose 文本，禁止 raw candle arrays 进入 LLM prompt。",
        ))
    return issues


def _check_candidate_effective_plan_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase E contract): flag ga_decisions where the candidate
    plan and the effective trade_plan disagree on side/entry/stop.

    Per PRD FR-4 / Phase E, the candidate is the deterministic geometry
    output; the effective trade_plan is the candidate after passing all
    execution gates. When both are present they MUST agree on side,
    entry price, stop loss, and take profit — otherwise the audit trail
    is inconsistent and the report cannot explain "候选计划 vs 执行计划".

    Detection parses ``raw_decision_json`` for the top-level
    ``candidate_trade_plan`` and ``trade_plan`` fields and compares the
    key fields. Severity: ``error`` post-marker, ``legacy_info`` pre-marker.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 500
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        candidate = raw.get("candidate_trade_plan")
        effective = raw.get("trade_plan")
        if not isinstance(candidate, dict) or not isinstance(effective, dict):
            continue
        # Both must be present for the mismatch check to apply. If only
        # the candidate is present (withheld), no mismatch is possible —
        # the effective trade_plan is correctly None.
        c_side = str(candidate.get("side") or "").upper()
        e_side = str(effective.get("side") or "").upper()
        c_entry = candidate.get("entry_price") or candidate.get("entry")
        e_entry = effective.get("entry_price") or effective.get("entry")
        c_stop = candidate.get("stop_loss") or candidate.get("stop")
        e_stop = effective.get("stop_loss") or effective.get("stop")
        mismatch_fields: list[str] = []
        if c_side and e_side and c_side != e_side:
            mismatch_fields.append("side")
        if c_entry is not None and e_entry is not None and float(c_entry) != float(e_entry):
            mismatch_fields.append("entry_price")
        if c_stop is not None and e_stop is not None and float(c_stop) != float(e_stop):
            mismatch_fields.append("stop_loss")
        if not mismatch_fields:
            continue
        issues.append(_issue(
            CANDIDATE_EFFECTIVE_PLAN_MISMATCH, "error",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "decision": r["decision"],
                "candidate_side": c_side, "effective_side": e_side,
                "mismatch_fields": mismatch_fields,
            },
            "candidate_trade_plan 与 trade_plan 关键字段不一致："
            f"{','.join(mismatch_fields)}；执行门禁不得修改 side/entry/stop，"
            "只能整体接受或拒绝。",
        ))
    return issues


def _check_batch_time_health_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (Phase B contract): flag analysis_batches marked
    ``status='success'`` but whose symbols' market-data-quality is not
    "ready" pinned to ``batch.analysis_time``.

    Per PRD FR-1 / Phase B, the hourly report's data quality must use the
    selected batch's ``analysis_time``, not the report send wall-clock.
    A success batch whose symbols are not "ready" at the batch time means
    the report's stale/gap checks were evaluated against the wrong time
    and the batch may have been marked complete with unhealthy data.

    Detection joins ``analysis_batches`` to ``batch_symbol_status`` and
    ``market_snapshots`` via snapshot_id (when present). For each success
    batch, sample up to 50 completed symbols and verify the snapshot's
    data_quality_json has all TFs ``ready=True`` at ``analysis_time``.
    Severity: ``error`` post-marker, ``legacy_info`` pre-marker.

    R5 P1-1 fix: previously the check had four fail-open paths and only
    sampled 5 symbols — a ``ready=True`` but 12h-stale snapshot would
    pass silently. Now:
      - Sample 50 completed symbols (up from 5) for broader coverage.
      - Missing snapshot / malformed data_quality / malformed health
        are fail-closed (recorded as unhealthy, not skipped).
      - ``last_close`` must be within ``2 * INTERVAL_MS[tf]`` of
        ``batch.analysis_time`` — i.e. the most recent bar plus one
        tolerance interval. Stale-but-ready snapshots now flag.

    R8 P1 fix: previously the check validated *each present* TF's
    readiness but not the *required TF set*. A snapshot with only
    ``5m`` healthy (missing ``1d/4h/1h/15m``) was judged healthy
    because the loop iterated only the TFs present in ``tf_health``.
    Now require all five required TFs (``1d/4h/1h/15m/5m`` per
    ``config/scheduler.yaml:analyze_market_15m.timeframes``) to be
    present — otherwise fail-closed with ``missing_required_tf:<list>``.
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT ab.batch_id, ab.primary_interval, ab.analysis_time,
               ab.status, ab.enabled_symbols_json
        FROM analysis_batches ab
        WHERE ab.status = 'success'
        ORDER BY ab.started_at DESC LIMIT 20
        """
    ).fetchall()
    for ab in rows:
        bid = ab["batch_id"] if ab["batch_id"] else None
        if not bid:
            continue
        # R5 P1-1: sample 50 (up from 5) for broader coverage.
        completed = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status "
                "WHERE batch_id=? AND status='completed' LIMIT 50",
                (bid,),
            ).fetchall()
        ]
        if not completed:
            continue
        unhealthy_syms: list[str] = []
        for sym in completed:
            snap_row = repo.conn.execute(
                """
                SELECT ms.data_quality_json, ms.analysis_time AS snapshot_time
                FROM market_snapshots ms
                JOIN ga_decisions gd ON gd.snapshot_id = ms.id
                WHERE gd.batch_id=? AND gd.symbol=?
                LIMIT 1
                """,
                (bid, sym),
            ).fetchone()
            # R5 P1-1: fail-closed — missing snapshot is a real gap,
            # not a skip.
            if not snap_row:
                unhealthy_syms.append(f"{sym} (missing_snapshot)")
                continue
            dq_raw = snap_row["data_quality_json"]
            dq = _safe_json(dq_raw) or {}
            # R5 P1-1: fail-closed — malformed data_quality is a real gap.
            if not isinstance(dq, dict):
                unhealthy_syms.append(f"{sym} (malformed_data_quality)")
                continue
            # P1-7 (07-05 final review): production market_state_builder
            # persists per-TF health under ``data_quality.health[tf]`` (see
            # market_state_builder.py:_data_quality). The previous code read
            # ``timeframes`` / ``health_by_tf``, which do not exist in
            # production — fault injection seeded the wrong shape and so
            # 7/7 was a false positive. Read the production path first,
            # keep the legacy paths as fallbacks for older rows.
            tf_health = dq.get("health") or dq.get("timeframes") or dq.get("health_by_tf") or {}
            # R5 P1-1: fail-closed — malformed health is a real gap.
            if not isinstance(tf_health, dict):
                unhealthy_syms.append(f"{sym} (malformed_health)")
                continue
            # R7 P1 fix: empty health dict is a real gap, not "healthy".
            # Pre-R7 an empty ``{}`` (or ``{"health": {}}`` / ``{"health":
            # {"1h": "broken"}}`` with non-dict TF entries) zero-iterated
            # the loop below and was silently treated as healthy. Now
            # require a non-empty dict and that every TF entry is itself
            # a dict — otherwise fail-closed.
            if not tf_health:
                unhealthy_syms.append(f"{sym} (empty_health)")
                continue
            # R8 P1 fix: validate the *required* timeframe set, not just
            # "any non-empty health". Pre-R8 a snapshot with only ``5m``
            # healthy (missing ``1d/4h/1h/15m``) passed because the loop
            # only iterated present TFs. The hourly report's multi-TF
            # bias depends on all five TFs being ready at
            # ``batch.analysis_time`` — a partial set means the LLM
            # was missing major-TF context.
            # R9 P2-5 fix: load required TFs from config keyed by
            # ``primary_interval`` (no longer a hardcoded literal).
            required_tfs = _required_timeframes_for_batch(
                ab["primary_interval"] if "primary_interval" in ab.keys() else None
            )
            present_tfs = set(tf_health.keys())
            missing_required = required_tfs - present_tfs
            if missing_required:
                # Missing required TFs → fail-closed. Sort for stable
                # issue text.
                missing_sorted = sorted(missing_required)
                unhealthy_syms.append(
                    f"{sym} (missing_required_tf:{','.join(missing_sorted)})"
                )
                continue
            batch_at = int(ab["analysis_time"] or 0)
            unhealthy = False
            stale_reason = ""
            for tf_key, tf_info in tf_health.items():
                # R7 P1 fix: non-dict TF entry is malformed, fail-closed.
                if not isinstance(tf_info, dict):
                    unhealthy = True
                    stale_reason = f"{tf_key}:malformed_entry"
                    break
                ready = bool(tf_info.get("ready"))
                last_close = int(tf_info.get("last_close_time") or 0)
                # Phase B contract: ready=True AND last_close <= batch_at
                # AND last_close within 1 interval of batch_at (not stale).
                if not ready:
                    unhealthy = True
                    stale_reason = f"{tf_key}:not_ready"
                    break
                if last_close <= 0 or last_close > batch_at:
                    unhealthy = True
                    stale_reason = f"{tf_key}:future_close"
                    break
                # R5 P1-1 fix: stale lower bound. ``ready=True`` but
                # stale-by-12h data was passing because only ``last_close
                # <= batch_at`` was checked. Require ``last_close`` to be
                # within 2 intervals of ``batch_at`` (1 just-closed bar +
                # 1 tolerance bar). For 1h, tolerance = 2 * 3_600_000 =
                # 7_200_000 ms = 2h — so a 12h-stale snapshot now fails.
                tf_ms = INTERVAL_MS.get(str(tf_key))
                if tf_ms and last_close < batch_at - 2 * tf_ms:
                    unhealthy = True
                    stale_reason = f"{tf_key}:stale_by_{(batch_at - last_close) // tf_ms}_bars"
                    break
            if unhealthy:
                unhealthy_syms.append(f"{sym} ({stale_reason})" if stale_reason else sym)
        if unhealthy_syms:
            issues.append(_issue(
                BATCH_TIME_HEALTH_MISMATCH, "error",
                {
                    "batch_id": bid,
                    "primary_interval": ab["primary_interval"],
                    "analysis_time": int(ab["analysis_time"] or 0),
                    "unhealthy_symbols": unhealthy_syms,
                    "checked_symbols": completed,
                },
                "成功批次的品种在 batch.analysis_time 健康检查未通过："
                "Phase B 要求小时报告使用批次时间而非墙钟；"
                "检查 _fetch_market_data_quality 是否传入 batch.analysis_time。",
            ))
    return issues


def _check_failed_jobs_outside_window(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Phase H (PRD FR-8): flag failed analysis_batches older than the
    documented recent window so they do not permanently repeat as
    "current risk" in the hourly report.

    Per PRD FR-8, "最近失败任务"应有明确时间窗口；历史失败不得永久
    重复冒充当前风险. The default window is 7 days
    (FAILED_JOBS_RECENT_WINDOW_DAYS). Batches older than the window that
    are still in ``status='failed'`` are surfaced as ``legacy_info`` so
    they remain visible for audit but do not count against current risk.

    Detection queries ``analysis_batches`` with
    ``status='failed'`` AND ``started_at`` older than the window. Each
    batch becomes a single issue. Severity: ``legacy_info`` (always —
    these are by definition historical and not current).
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, started_at, status
        FROM analysis_batches
        WHERE status = 'failed'
          AND datetime(started_at) < datetime('now', ?)
        ORDER BY started_at DESC LIMIT 50
        """,
        (f"-{FAILED_JOBS_RECENT_WINDOW_DAYS} days",),
    ).fetchall()
    for r in rows:
        issues.append(_issue(
            FAILED_JOBS_OUTSIDE_WINDOW, "legacy_info",
            {
                "batch_id": r["batch_id"] if r["batch_id"] else "",
                "primary_interval": r["primary_interval"],
                "analysis_time": int(r["analysis_time"] or 0),
                "started_at": r["started_at"],
                "window_days": FAILED_JOBS_RECENT_WINDOW_DAYS,
            },
            f"失败批次超出 {FAILED_JOBS_RECENT_WINDOW_DAYS} 天窗口："
            "归类为 legacy_info，不计入当前风险；"
            "诊断必须区分当前问题、warning 和 legacy history。",
        ))
    return issues


# ── Phase I (07-07): LLM retry + hourly accuracy repair diagnostics ──────────
# Each function returns a list of issue dicts. These are runtime diagnostics
# — they do NOT write markers to _migration_state and they do NOT use the
# continuity/semantic marker cutoff (they fire on any matching data in the
# latest 24h / latest batch). See PRD AC18 and design §11.

# 24h lookback window for LLM-related diagnostics (ms).
_LLM_DIAGNOSTIC_WINDOW_MS = 24 * 3600 * 1000


def _check_llm_failure_rate_high(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag batches whose LLM failure rate >= 50% over the latest 10 calls.

    Per PRD AC18 / R3, the batch-level circuit breaker opens when the failure
    rate exceeds 50% over the latest 10 LLM calls. This diagnostic surfaces
    that condition post-hoc so operators can see breaker-quality failures
    even when the breaker itself was not queried (e.g., legacy batches).

    Detection reads ``analysis_batches.summary_json.llm_health`` from the
    latest 5 batches. If ``recent_10_failure_rate >= 0.5`` (the breaker-open
    condition over the latest 10 LLM calls), emit ``error``. Falls back to
    whole-batch rate only when ``recent_10_failure_rate`` is missing
    (legacy batches pre-Phase-I) AND ``total_attempts >= 10``.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status, summary_json
        FROM analysis_batches
        WHERE analysis_time >= ?
        ORDER BY started_at DESC LIMIT 5
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        summary = _safe_json(r["summary_json"]) or {}
        if not isinstance(summary, dict):
            continue
        health = summary.get("llm_health") or {}
        if not isinstance(health, dict):
            continue
        total = int(health.get("total_attempts") or 0)
        failed = int(health.get("failed") or 0)
        recent_10_calls = int(health.get("recent_10_calls") or 0)
        recent_10_failed = int(health.get("recent_10_failed") or 0)
        recent_10_rate = float(health.get("recent_10_failure_rate") or 0.0)
        # Primary: latest-10 failure rate (matches breaker-open condition).
        # Fallback: whole-batch rate for legacy batches missing recent_10_*
        # fields, only when total >= 10.
        if recent_10_calls >= 3 and recent_10_rate >= 0.5:
            rate = recent_10_rate
            window = f"latest {recent_10_calls} calls"
        elif total >= 10:
            rate = failed / total
            window = f"whole batch ({total} calls, legacy)"
            if rate < 0.5:
                continue
        else:
            continue  # not enough samples to evaluate
        issues.append(_issue(
            LLM_FAILURE_RATE_HIGH, "error",
            {
                "batch_id": r["batch_id"] if r["batch_id"] else "",
                "primary_interval": r["primary_interval"],
                "analysis_time": int(r["analysis_time"] or 0),
                "total_attempts": total,
                "failed": failed,
                "failure_rate": round(rate, 3),
                "window": window,
                "dominant_error_category": health.get("dominant_error_category") or "",
            },
            "LLM 失败率 ≥ 50%：检查 LLM 配置、网关、模型可用性；"
            "如已熔断，确认 breaker_state=open 并验证后续 symbol 走 deterministic fallback。",
        ))
    return issues


def _check_llm_config_error_detected(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag any ga_decisions row with ``llm_error_category=llm_config_error``.

    Per PRD AC18 / R1, ``llm_config_error`` is the HTTP 422 / model-not-found
    / auth-failure category — it is non-retryable and triggers an immediate
    breaker open. Any occurrence in the last 24h is an ``error``.

    Detection parses ``raw_decision_json.llm_error_category`` from the latest
    200 ga_decisions rows created in the last 24h.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= ?
        ORDER BY id DESC LIMIT 200
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if str(raw.get("llm_error_category") or "") != "llm_config_error":
            continue
        issues.append(_issue(
            LLM_CONFIG_ERROR_DETECTED, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_error": str(raw.get("llm_error") or "")[:300],
                "llm_config_name": str(raw.get("llm_config_name") or ""),
                "llm_model": str(raw.get("llm_model") or ""),
            },
            "LLM 配置错误（model not found / auth / invalid request）："
            "不可重试，breaker 必须 open；检查 llm_config 解析、model 名称拼写、apikey 有效性。",
        ))
    return issues


def _check_llm_retry_exhausted(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag ga_decisions rows with ``llm_fallback_reason=retry_exhausted``.

    Per PRD AC18 / R2, when all 3 retry attempts fail with retryable
    categories, ``llm_fallback_reason=retry_exhausted`` is recorded. Any
    occurrence in the last 24h is a ``warning`` (not error — retry-exhausted
    is expected behavior under degraded LLM service, not a contract violation).
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= ?
        ORDER BY id DESC LIMIT 200
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if str(raw.get("llm_fallback_reason") or "") != "retry_exhausted":
            continue
        issues.append(_issue(
            LLM_RETRY_EXHAUSTED, "warning",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_attempt_count": int(raw.get("llm_attempt_count") or 0),
                "llm_error_category": str(raw.get("llm_error_category") or ""),
                "llm_error": str(raw.get("llm_error") or "")[:300],
            },
            "LLM retry 配额耗尽（3 次尝试均失败）："
            "确认 fail-closed 路径生效，candidate_trade_plan 已保留并标记 plan_execution_state=unconfirmed。",
        ))
    return issues


def _check_llm_circuit_breaker_open(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag batches whose ``summary_json.llm_health.breaker_state=open``.

    Per PRD AC18 / R3, breaker open is a batch-level signal — once open, all
    remaining symbols in that batch must use deterministic fallback. Any
    batch in the latest 5 with ``breaker_state=open`` is an ``error`` (the
    underlying config/transport issue must be addressed).
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status, summary_json
        FROM analysis_batches
        WHERE analysis_time >= ?
        ORDER BY started_at DESC LIMIT 5
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        summary = _safe_json(r["summary_json"]) or {}
        if not isinstance(summary, dict):
            continue
        health = summary.get("llm_health") or {}
        if not isinstance(health, dict):
            continue
        if str(health.get("breaker_state") or "") != "open":
            continue
        issues.append(_issue(
            LLM_CIRCUIT_BREAKER_OPEN, "error",
            {
                "batch_id": r["batch_id"] if r["batch_id"] else "",
                "primary_interval": r["primary_interval"],
                "analysis_time": int(r["analysis_time"] or 0),
                "total_attempts": int(health.get("total_attempts") or 0),
                "successful": int(health.get("successful") or 0),
                "failed": int(health.get("failed") or 0),
                "dominant_error_category": str(health.get("dominant_error_category") or ""),
            },
            "LLM 熔断器已 open：本批剩余 symbol 应走 deterministic fallback，"
            "禁止自动执行候选计划；检查 LLM 配置/网关并修复根因后等待下一批 breaker reset。",
        ))
    return issues


def _check_deterministic_candidate_reported_as_trade_plan(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """Flag decisions that have a rule candidate but ``plan_execution_state``
    is NOT ``confirmed`` and NOT ``no_candidate`` — i.e., the candidate is
    being held but not confirmed, which the hourly report must NOT render as
    "候选计划已生成（LLM 已确认）".

    Per PRD AC18 / R4 and design §11.1, this diagnostic is data-driven: it
    reads ``ga_decisions.raw_decision_json`` fields directly, NOT rendered
    report text. Rendered text correctness is verified separately by the
    renderer unit test on ``_render_plan_state_label``.

    Conditions for the diagnostic to fire (all must hold):
    - ``candidate_trade_plan`` is a non-empty dict (rule SOP produced a
      candidate).
    - ``has_trade_plan`` is False (no executable plan was confirmed).
    - ``plan_execution_state`` is not None, not ``confirmed``, not
      ``no_candidate`` (i.e., the decision is in ``unconfirmed`` /
      ``risk_rejected`` / ``invalidated`` state).
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= ?
        ORDER BY id DESC LIMIT 200
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        candidate = raw.get("candidate_trade_plan")
        if not isinstance(candidate, dict) or not candidate:
            continue
        if raw.get("has_trade_plan"):
            continue  # has a confirmed plan, not a deterministic-only candidate
        state = str(raw.get("plan_execution_state") or "")
        if state in ("", "confirmed", "no_candidate"):
            continue
        issues.append(_issue(
            DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "analysis_time": int(r["analysis_time"] or 0),
                "plan_execution_state": state,
                "plan_origin": str(raw.get("plan_origin") or ""),
                "has_trade_plan": False,
                "candidate_trade_plan_present": True,
            },
            "规则候选计划存在但 plan_execution_state={state}："
            "小时报告必须渲染为 '规则候选计划已生成，LLM 未确认，禁止执行' 而非 '候选计划已生成（LLM 已确认）'。"
            f" (state={state})",
        ))
    return issues


def _check_raw_grade_exceeds_htf_cap(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag ga_decisions whose ``raw_signal_grade`` exceeds the HTF-alignment
    cap allowed by Step 4b/4c/4d rules.

    Per PRD AC18 / R5 and design §7.1, the caps are:
    - Step 4b Cap 1: 1D and 4H both opposite to candidate → max B.
    - Step 4b Cap 2: 4H range/transition/mixed/unknown → max B.
    - Step 4b Cap 3: 1H and 15M both not aligned with candidate → max B.
    - Step 4b Cap 4: only 5M supports, 4H and 1H don't → max C.

    Detection recomputes the cap from ``raw_decision_json.timeframe_context``
    and asserts ``raw_signal_grade`` does not exceed. The GRADE_ORDER is
    ``S > A > B > C > D``.
    """
    issues: list[dict[str, Any]] = []
    grade_rank = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= ?
        ORDER BY id DESC LIMIT 200
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        raw_grade = str(raw.get("raw_signal_grade") or r["signal_grade"] or "D").upper()
        if raw_grade not in grade_rank:
            continue
        ctx = raw.get("timeframe_context") or {}
        if not isinstance(ctx, dict):
            continue
        bias_1d = str((ctx.get("1d") or {}).get("bias") or "").lower()
        bias_4h = str((ctx.get("4h") or {}).get("bias") or "").lower()
        bias_1h = str((ctx.get("1h") or {}).get("bias") or "").lower()
        bias_15m = str((ctx.get("15m") or {}).get("bias") or "").lower()
        # 5M bias is surfaced on the top-level ``m5_bias`` field by
        # _apply_htf_alignment_caps (market_semantics.py). 5M is excluded
        # from timeframe_context (data-only per schema), so it lives
        # outside ctx. For pre-fix decisions (no m5_bias), this cap
        # cannot be evaluated.
        bias_5m = str(raw.get("m5_bias") or "").lower()
        market_bias = str(raw.get("market_bias") or "").lower()
        candidate_side = "LONG" if market_bias == "bullish" else "SHORT" if market_bias == "bearish" else None
        opposite = "bearish" if candidate_side == "LONG" else "bullish" if candidate_side == "SHORT" else None
        # R6 fix: compare bias values ("bullish"/"bearish") against bias values,
        # NOT against candidate_side.lower() ("long"/"short"). The
        # market_semantics.py implementation uses candidate_side_lower which
        # is "bullish"/"bearish" (the bias value), not "long"/"short" (the
        # side value). The previous diagnostic compared bias against side,
        # which never matched -> Cap 3 and Cap 4 were dead branches.
        candidate_bias = "bullish" if candidate_side == "LONG" else "bearish" if candidate_side == "SHORT" else None

        # Implementation uses INDEPENDENT if-statements (market_semantics.py
        # _apply_htf_alignment_caps). When multiple caps apply, the most
        # severe (lowest max_allowed) wins. Replicate that here.
        max_allowed = "S"  # default: no cap
        applied_reasons: list[str] = []
        if candidate_side and bias_1d == opposite and bias_4h == opposite:
            if grade_rank[max_allowed] > grade_rank["B"]:
                max_allowed = "B"
            applied_reasons.append("htf_countertrend_cap")
        if bias_4h in ("", "neutral", "mixed", "unknown"):
            if grade_rank[max_allowed] > grade_rank["B"]:
                max_allowed = "B"
            applied_reasons.append("htf_4h_nondirectional_cap")
        if candidate_bias and bias_1h != candidate_bias and bias_15m != candidate_bias:
            if grade_rank[max_allowed] > grade_rank["B"]:
                max_allowed = "B"
            applied_reasons.append("mtf_misalignment_cap")
        if (
            candidate_bias
            and bias_5m == candidate_bias
            and bias_4h != candidate_bias
            and bias_1h != candidate_bias
        ):
            if grade_rank[max_allowed] > grade_rank["C"]:
                max_allowed = "C"
            applied_reasons.append("low_tf_rebound_only_cap")

        if grade_rank[raw_grade] > grade_rank[max_allowed]:
            issues.append(_issue(
                RAW_GRADE_EXCEEDS_HTF_CAP, "error",
                {
                    "decision_id": int(r["id"]),
                    "symbol": r["symbol"],
                    "analysis_time": int(r["analysis_time"] or 0),
                    "raw_signal_grade": raw_grade,
                    "max_allowed_grade": max_allowed,
                    "applied_cap_reasons": applied_reasons or ["none"],
                    "timeframe_context_bias": {
                        "1d": bias_1d, "4h": bias_4h, "1h": bias_1h,
                        "15m": bias_15m, "5m": bias_5m,
                    },
                    "market_bias": market_bias,
                },
                f"raw_signal_grade={raw_grade} 超过 HTF 对齐上限 {max_allowed}（{', '.join(applied_reasons)}）："
                "Step 4b/4c/4d cap 未生效；检查 normalize_market_semantics 的 cap 逻辑是否被绕过。",
            ))
    return issues


def _check_success_batch_missing_completed_symbols(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """Flag ``analysis_batches`` with ``status='success'`` but
    ``completed_symbols_json=[]`` (raw column, NOT the read-time
    compensation).

    Per PRD AC18 / R8 and design §10.1, this is the write-link gap root cause:
    ``finish_analysis_batch`` previously wrote only ``status`` + ``summary_json``
    and never materialized ``completed_symbols_json`` / ``failed_symbols_json``
    from ``batch_symbol_status``. The fix materializes those columns inside
    the repo method. This diagnostic catches any batch that still shows the
    inconsistent state (e.g., pre-fix batches, or regressions).

    Detection reads the raw ``completed_symbols_json`` column (not
    ``get_analysis_batch`` which compensates at read time). Any ``status=
    success`` batch in the latest 20 with empty raw column is an ``error``.

    R7 P2-1 fix: apply 24h cutoff matching the other 07-07 diagnostics.
    Design §11 line 864 mandates "Each diagnostic has a cutoff: only fires
    on batches from the last 24h (not historical)." Without this filter,
    pre-fix historical batches (where finish_analysis_batch didn't
    materialize the column) fire as ``error`` forever.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status,
               completed_symbols_json, failed_symbols_json, summary_json
        FROM analysis_batches
        WHERE status = 'success' AND analysis_time >= ?
        ORDER BY started_at DESC LIMIT 20
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        completed_raw = _safe_json(r["completed_symbols_json"])
        if isinstance(completed_raw, list) and len(completed_raw) > 0:
            continue  # properly materialized
        # Either None, malformed, or empty list — all are defects when
        # status=success.
        failed_raw = _safe_json(r["failed_symbols_json"])
        # Cross-check: does batch_symbol_status have completed entries? If
        # so, the column is genuinely stale (write-link gap). If not, the
        # batch may have been marked success erroneously.
        bid = r["batch_id"] if r["batch_id"] else ""
        live_completed = 0
        if bid:
            try:
                live_completed = repo.conn.execute(
                    "SELECT COUNT(*) AS c FROM batch_symbol_status WHERE batch_id=? AND status='completed'",
                    (bid,),
                ).fetchone()["c"]
            except Exception:
                live_completed = 0
        issues.append(_issue(
            SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS, "error",
            {
                "batch_id": bid,
                "primary_interval": r["primary_interval"],
                "analysis_time": int(r["analysis_time"] or 0),
                "status": r["status"],
                "completed_symbols_json_raw": r["completed_symbols_json"] or "",
                "failed_symbols_json_raw": r["failed_symbols_json"] or "",
                "live_completed_count": int(live_completed),
            },
            "status=success 但 completed_symbols_json 为空（write-link gap）："
            "finish_analysis_batch 必须从 batch_symbol_status 物化 completed/failed 列；"
            "若 live_completed_count > 0，则列确实 stale；若 = 0，则批次被误标 success。",
        ))
    return issues


def _check_hourly_report_used_partial_running_batch(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """Flag when the latest ``analysis_batches.status='running'`` AND a
    hourly report was generated AFTER that batch started.

    Per PRD AC18 / R7-R8 and design §9.4, the hourly report must select the
    latest *complete* batch (``status='success'`` AND enabled_count > 0 AND
    completed_count == enabled_count AND matching GA decision count ==
    enabled_count). Rendering against a running/partial batch violates the
    contract.

    Detection checks: (1) is the latest batch (by ``started_at``) marked
    ``running``? (2) was there an ``alert_outbox`` row with
    ``alert_type='hourly_summary'`` created in the last hour? (3) was the
    alert created AFTER the running batch's ``started_at`` (i.e., the
    renderer had this running batch available but still emitted a report)?
    Only when all three hold do we emit ``warning``.

    The ``created_at`` vs ``started_at`` cross-reference replaces the
    earlier ``payload_json.batch_id`` comparison (which was dead code:
    production hourly_summary payloads do not include ``batch_id``).
    """
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status, started_at
        FROM analysis_batches
        ORDER BY started_at DESC LIMIT 5
        """
    ).fetchall()
    if not rows:
        return issues
    latest = rows[0]
    if str(latest["status"] or "") != "running":
        return issues
    # Check for a recent hourly_summary alert.
    try:
        alert_row = repo.conn.execute(
            """
            SELECT id, alert_type, created_at, status
            FROM alert_outbox
            WHERE alert_type = 'hourly_summary'
              AND datetime(created_at) >= datetime('now', '-1 hour')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    except Exception:
        alert_row = None
    if not alert_row:
        return issues  # no hourly report in last hour - nothing to flag
    # P2-5 fix: cross-reference alert created_at vs running batch started_at.
    # If the alert was created BEFORE the running batch started, the
    # renderer correctly used a previous complete batch (no defect). Only
    # flag when the alert was created AFTER the running batch started,
    # indicating the renderer had the running batch available but still
    # emitted a report.
    running_started_at = str(latest["started_at"] or "")
    alert_created_at = str(alert_row["created_at"] or "")
    if running_started_at and alert_created_at:
        try:
            from datetime import datetime as _dt
            # SQLite stores started_at as ISO string; alert_outbox.created_at
            # is also ISO. Compare via datetime parsing.
            t_running = _dt.fromisoformat(running_started_at.replace("Z", "+00:00"))
            t_alert = _dt.fromisoformat(alert_created_at.replace("Z", "+00:00"))
            if t_alert < t_running:
                # Alert fired before the running batch started - renderer
                # correctly used a previous complete batch. Not a defect.
                return issues
        except (ValueError, TypeError):
            pass  # fall through to emit if timestamps unparseable
    running_batch_id = str(latest["batch_id"] or "")
    # Downgraded from "error" to "warning": a same-batch render while status=running
    # is anomalous but recoverable; the next batch will produce a fresh report.
    issues.append(_issue(
        HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH, "warning",
        {
            "batch_id": running_batch_id,
            "primary_interval": latest["primary_interval"],
            "analysis_time": int(latest["analysis_time"] or 0),
            "batch_status": "running",
            "batch_started_at": running_started_at,
            "hourly_alert_id": int(alert_row["id"]),
            "hourly_alert_created_at": alert_created_at,
            "hourly_alert_status": str(alert_row["status"]),
            "window": "latest_running_batch_and_last_1h_alert",
        },
        "小时报告使用了 running 批次：必须使用最新 status=success 的完整批次；"
        "检查 _select_latest_complete_batch 是否生效，禁用 running/partial 渲染路径。"
        "（已降级为 warning：仅当 hourly alert 创建于 running 批次 started_at 之后时触发。）",
    ))
    return issues