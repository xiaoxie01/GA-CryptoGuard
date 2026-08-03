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
from datetime import datetime, timedelta, timezone
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
# 07-22 Phase-2 contract correction: ``raw_signal_grade`` is a pre-gate
# audit value and MAY exceed the HTF cap (it records the uncapped LLM/SOP
# grade). The cap must constrain the effective / canonical grade only.
# ``raw_grade_exceeds_htf_cap`` is retained as a deprecated alias string so
# historical fault-injection / log greps still resolve, but the active
# diagnostic code is ``effective_grade_exceeds_htf_cap``.
RAW_GRADE_EXCEEDS_HTF_CAP = "raw_grade_exceeds_htf_cap"  # deprecated alias
EFFECTIVE_GRADE_EXCEEDS_HTF_CAP = "effective_grade_exceeds_htf_cap"
SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS = "success_batch_missing_completed_symbols"
HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH = "hourly_report_used_partial_running_batch"
# 07-10 R6-E (P1-4): a batch whose status is still 'running' but every enabled
# symbol has a terminal row (completed or failed) in batch_symbol_status is a
# terminalization leak - finish_analysis_batch never flipped the batch to a
# terminal status (success/failed). See plan §4 P1-4 / AC5.
BATCH_STUCK_RUNNING_ALL_TERMINAL = "batch_stuck_running_all_terminal"
# 07-10 S7 (P1 #7): fair-scheduling + context-continuity contract codes.
LLM_FAIR_SCHEDULING_CONTRACT_MARKER_MISSING = "llm_fair_scheduling_contract_marker_missing"
FAIR_PATH_CONTINUITY_REAL_INJECTION = "fair_path_continuity_real_injection"
PER_JOB_FAILURE_CONSISTENCY = "per_job_failure_consistency"
# 07-10 P1-1 (design §10): eight formal Phase F diagnostic codes. Each
# corresponds to a post-marker production-chain contract (design §10).
# Seven of the eight (all except ``llm_timeout_config_out_of_range``) are
# fair-scheduling-marker-cutoff-scoped: pre-marker findings demote to
# ``legacy_info`` via ``_apply_llm_fair_scheduling_marker_cutoff``.
# ``llm_timeout_config_out_of_range`` uses the independent
# ``llm_provider_timeout_envelope_contract_v2`` marker instead: pre-marker
# rows are SQL-excluded (no error, no ``legacy_info``); post-marker stays
# error; marker-missing is fail-closed.
LLM_FIRST_ATTEMPT_COVERAGE_LOW = "llm_first_attempt_coverage_low"
LLM_SYMBOL_STARVATION = "llm_symbol_starvation"
LLM_REPORT_COUNT_MISMATCH = "llm_report_count_mismatch"
LLM_SUCCESS_MISSING_ATTEMPT_METADATA = "llm_success_missing_attempt_metadata"
LLM_CONTINUITY_NOT_INCLUDED = "llm_continuity_not_included"
LLM_TIMEOUT_CONFIG_OUT_OF_RANGE = "llm_timeout_config_out_of_range"
LLM_BATCH_DEGRADED_REPORTED_HEALTHY = "llm_batch_degraded_reported_healthy"
DIAGNOSTIC_QUERY_FAILED = "diagnostic_query_failed"
LLM_REPAIR_COUNTED_AS_PROVIDER_CALL = "llm_repair_counted_as_provider_call"
# 07-22 Codex P1-1 / P2 exclude-only: independent provider-timeout envelope
# contract marker. ``llm_timeout_config_out_of_range`` uses this marker's
# applied_at as the SQL lower bound (NOT the fair-scheduling marker).
# Pre-marker pcc>=1 / timeout_ms=0 rows remain in ga_decisions for audit but
# are EXCLUDED from current diagnose_report_accuracy issues (no error and no
# legacy_info). Post-marker violations stay error. Marker-missing is
# fail-closed (error).
LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_MISSING = (
    "llm_provider_timeout_envelope_contract_marker_missing"
)
# 07-14 R8 P2-NEW-1 (contract #4): crash-residue diagnostic. A producer that
# died between Phase 1 (prepared skill-execution log autocommit write) and
# Phase 2 (the BEGIN IMMEDIATE commit/abort) leaves ``skill_execution_logs``
# rows stuck at ``commit_state='prepared'``. The restart recovery hook
# (``recover_stale_prepared_skill_logs`` wired in ``start_all_services``)
# terminalizes long-prepared rows to ``aborted``, but a prepared row that is
# NOT yet stale (younger than the threshold) -- or a row on a node where the
# restart hook has not yet run -- signals an in-flight / mid-crash producer.
# This runtime diagnostic surfaces ANY long-prepared row so an operator can
# see stuck state without restarting; it is NOT marker-cutoff-scoped (it is
# a live runtime invariant, not a historical contract).
STUCK_PREPARED_SKILL_LOGS = "stuck_prepared_skill_logs"

# 08-02 P1-3: execution-funnel report-contract diagnostics. The hourly report
# must show the four aspects of the LLM execution funnel SEPARATELY (call
# succeeded / plan confirmed / risk passed / final executable), and "LLM not
# confirmed" must come ONLY from immutable synthesis evidence — never inferred
# from a final trade_plan cleared by later gates. These six codes are the new
# diagnostics plus their fail-closed marker-missing gate. The five per-decision
# codes are marker-cutoff-scoped (pre-contract rows demote to legacy_info via
# _apply_execution_funnel_marker_cutoff); execution_funnel_starvation is an
# aggregate/live 24h invariant (NOT cut off).
EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING = (
    "execution_funnel_report_contract_marker_missing"
)
CONFIRMED_WITHOUT_EXECUTABLE_PLAN = "confirmed_without_executable_plan"
NO_CANDIDATE_WITH_CANDIDATE_PLAN = "no_candidate_with_candidate_plan"
EXECUTABLE_STATUS_WITHOUT_PLAN = "executable_status_without_plan"
OPPORTUNITY_WATCH_NOT_MATERIALIZED = "opportunity_watch_not_materialized"
OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION = "opportunity_watch_untriggerable_condition"
# 08-02 Finding 5 (P2): companion to OPPORTUNITY_WATCH_NOT_MATERIALIZED — a
# decision that ADVERTISES create_opportunity_watch but carries NO structured
# watch can never honor the action (the P0-2 wire-in requires a structured
# watch). The materialization check skips these (unstructured watch is its
# "skipped-by-design" path), so this companion owns them: a broken promise at
# the decision layer. Firing on a current row means a producer path still
# persists the broken promise (the Finding-2 controller fix strips the action
# for unstructured watches); pre-marker rows are excluded by the SQL bound.
OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH = (
    "opportunity_watch_advertised_without_watch"
)
EXECUTION_FUNNEL_STARVATION = "execution_funnel_starvation"


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

    # 07-31 P1-4: the schema/breaker/preset integrity marker-missing check
    # runs FIRST so a missing contract is explicitly surfaced (fail-closed)
    # even when the two LLM diagnostics skip themselves.
    issues.extend(_check_llm_schema_breaker_preset_integrity_marker_missing(repo))

    # Phase I (07-07): LLM retry + hourly accuracy repair diagnostics. These
    # are runtime diagnostics without a marker cutoff — they fire on any
    # matching data in the latest 24h / latest batch. See PRD AC18. P1-4
    # (07-31): the two batch-level checks are now scoped to the
    # llm_schema_breaker_preset_integrity_v1 marker (skip when absent;
    # pre-marker batches demoted below).
    issues.extend(_check_llm_failure_rate_high(repo))
    issues.extend(_check_llm_config_error_detected(repo))
    issues.extend(_check_llm_retry_exhausted(repo))
    issues.extend(_check_llm_circuit_breaker_open(repo))
    issues.extend(_check_deterministic_candidate_reported_as_trade_plan(repo))
    issues.extend(_check_effective_grade_exceeds_htf_cap(repo))
    issues.extend(_check_success_batch_missing_completed_symbols(repo))
    issues.extend(_check_hourly_report_used_partial_running_batch(repo))
    # 07-10 R6-E (P1-4): a running batch whose every enabled symbol is
    # terminal is a terminalization leak. Runtime diagnostic (no marker
    # cutoff), scoped to the latest 24h. See plan §4 P1-4 / AC5.
    issues.extend(_check_batch_stuck_running_all_terminal(repo))
    # 07-10 S7 (P1 #7): fair-scheduling + context-continuity contract checks.
    # The marker-missing check runs first so a missing contract is explicitly
    # surfaced even when the continuity / per-job checks would otherwise pass
    # (or skip). These verify the S1-S6 production-chain postconditions
    # survive in persisted decisions + batch_symbol_status.
    issues.extend(_check_llm_fair_scheduling_contract_markers_missing(repo))
    issues.extend(_check_fair_path_continuity_real_injection(repo))
    issues.extend(_check_per_job_failure_consistency(repo))
    # 07-10 P1-1 (design §10): eight formal Phase F diagnostics. Each verifies
    # a post-marker fair-scheduling + context-continuity contract on the latest
    # batches / decisions. Marker-cutoff-scoped above.
    # 07-22 Codex P1-1 / P2 exclude-only: timeout envelope has its OWN marker
    # (independent of fair-scheduling). Marker-missing is fail-closed. The
    # timeout-config check applies SQL lower bound = envelope marker applied_at
    # so pre-marker rows never enter issues (no error, no legacy_info). No
    # demotion pass for this code — unique contract is exclude-only.
    issues.extend(_check_llm_provider_timeout_envelope_contract_markers_missing(repo))
    issues.extend(_check_llm_first_attempt_coverage_low(repo))
    issues.extend(_check_llm_symbol_starvation(repo))
    issues.extend(_check_llm_report_count_mismatch(repo))
    issues.extend(_check_llm_success_missing_attempt_metadata(repo))
    issues.extend(_check_llm_continuity_not_included(repo))
    issues.extend(_check_llm_timeout_config_out_of_range(repo))
    issues.extend(_check_llm_batch_degraded_reported_healthy(repo))
    issues.extend(_check_llm_repair_counted_as_provider_call(repo))
    # 07-14 R8 P2-NEW-1 (contract #4): surface long-prepared skill_execution_logs
    # left behind by a producer that crashed between Phase 1 and Phase 2. This is
    # a live runtime invariant (no marker cutoff): a prepared row older than the
    # staleness threshold means a stuck producer the restart hook has not yet
    # recovered. It is NOT historical audit -- every prepared row is in-flight.
    issues.extend(_check_stuck_prepared_skill_logs(repo))

    # 08-02 P1-3: execution-funnel report contract. The marker-missing check
    # runs FIRST (fail-closed: absent marker = explicit error, not silent
    # green); then the six per-decision/aggregate funnel checks, each of which
    # self-skips when the contract marker is absent.
    issues.extend(_check_execution_funnel_report_contract_marker_missing(repo))
    issues.extend(_check_confirmed_without_executable_plan(repo))
    issues.extend(_check_no_candidate_with_candidate_plan(repo))
    issues.extend(_check_executable_status_without_plan(repo))
    issues.extend(_check_opportunity_watch_not_materialized(repo))
    issues.extend(_check_opportunity_watch_advertised_without_watch(repo))
    issues.extend(_check_opportunity_watch_untriggerable_condition(repo))
    issues.extend(_check_execution_funnel_starvation(repo))

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
                # Pre-marker decision - demote to legacy_info, preserve visibility.
                _demote_to_legacy_info(issue)

    # Phase E: apply the independent semantic-accuracy marker cutoff to the
    # five new semantic checks. Decisions created before the semantic marker
    # are demoted to legacy_info; post-marker errors stay error/warning.
    _apply_semantic_marker_cutoff(repo, issues)

    # Phase H: apply the independent continuity-contract marker cutoff to
    # the seven new Phase A-G contract checks. Decisions created before
    # the continuity marker are demoted to legacy_info; post-marker
    # errors stay error/warning.
    _apply_continuity_marker_cutoff(repo, issues)

    # 07-10 S7 (P1 #7): apply the independent fair-scheduling + context-
    # continuity marker cutoff to the two new S1-S6 contract checks. Decisions
    # created before this marker are demoted to legacy_info; post-marker errors
    # stay error/warning.
    _apply_llm_fair_scheduling_marker_cutoff(repo, issues)

    # 07-31 P1-4: apply the independent schema/breaker/preset integrity marker
    # cutoff to the two LLM diagnostics (llm_failure_rate_high /
    # llm_circuit_breaker_open). Batches analysed BEFORE the marker are
    # historical audit (legacy_info) — symptom #4: the pre-fix breaker-open
    # batch must not repeat as a current error; post-marker errors stay error.
    _apply_llm_schema_breaker_preset_integrity_marker_cutoff(repo, issues)

    # 08-02 P1-3: apply the independent execution-funnel report-contract marker
    # cutoff to the five per-decision funnel checks (starvation is aggregate/
    # live over 24h and is NOT cut off). Decisions/watches created BEFORE the
    # marker are historical audit (legacy_info) — the four per-decision checks
    # are SQL-bound so this is a redundant safety net; the untriggerable-watch
    # scan is NOT SQL-bound so this cutoff is its real demotion path.
    _apply_execution_funnel_marker_cutoff(repo, issues)

    # 07-22 Codex P2: NO _apply_llm_timeout_envelope_marker_cutoff here.
    # Timeout-envelope unique contract is SQL exclude-only (pre-marker rows
    # never enter issues). Demotion-to-legacy_info is not part of this code.

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    legacy_info_count = sum(1 for i in issues if i["severity"] == "legacy_info")
    # Phase E (07-09): layer_counts groups issues by diagnostic layer so
    # report renderers can surface current-batch issues separately from
    # warning trends and legacy_audit historical records. The mapping
    # mirrors _layer_for_severity.
    layer_counts = {
        "current": sum(1 for i in issues if i.get("layer") == "current"),
        "warning": sum(1 for i in issues if i.get("layer") == "warning"),
        "legacy_audit": sum(1 for i in issues if i.get("layer") == "legacy_audit"),
    }

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
        LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING: _count(
            issues, LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING
        ),
        DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN: _count(issues, DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN),
        EFFECTIVE_GRADE_EXCEEDS_HTF_CAP: _count(issues, EFFECTIVE_GRADE_EXCEEDS_HTF_CAP),
        # deprecated alias kept at 0 unless a legacy emitter still uses it
        RAW_GRADE_EXCEEDS_HTF_CAP: _count(issues, RAW_GRADE_EXCEEDS_HTF_CAP),
        SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS: _count(issues, SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS),
        HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH: _count(issues, HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH),
        BATCH_STUCK_RUNNING_ALL_TERMINAL: _count(issues, BATCH_STUCK_RUNNING_ALL_TERMINAL),
        LLM_FAIR_SCHEDULING_CONTRACT_MARKER_MISSING: _count(issues, LLM_FAIR_SCHEDULING_CONTRACT_MARKER_MISSING),
        FAIR_PATH_CONTINUITY_REAL_INJECTION: _count(issues, FAIR_PATH_CONTINUITY_REAL_INJECTION),
        PER_JOB_FAILURE_CONSISTENCY: _count(issues, PER_JOB_FAILURE_CONSISTENCY),
        LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_MISSING: _count(
            issues, LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_MISSING
        ),
        LLM_FIRST_ATTEMPT_COVERAGE_LOW: _count(issues, LLM_FIRST_ATTEMPT_COVERAGE_LOW),
        LLM_SYMBOL_STARVATION: _count(issues, LLM_SYMBOL_STARVATION),
        LLM_REPORT_COUNT_MISMATCH: _count(issues, LLM_REPORT_COUNT_MISMATCH),
        LLM_SUCCESS_MISSING_ATTEMPT_METADATA: _count(issues, LLM_SUCCESS_MISSING_ATTEMPT_METADATA),
        LLM_CONTINUITY_NOT_INCLUDED: _count(issues, LLM_CONTINUITY_NOT_INCLUDED),
        LLM_TIMEOUT_CONFIG_OUT_OF_RANGE: _count(issues, LLM_TIMEOUT_CONFIG_OUT_OF_RANGE),
        LLM_BATCH_DEGRADED_REPORTED_HEALTHY: _count(issues, LLM_BATCH_DEGRADED_REPORTED_HEALTHY),
        LLM_REPAIR_COUNTED_AS_PROVIDER_CALL: _count(issues, LLM_REPAIR_COUNTED_AS_PROVIDER_CALL),
        EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING: _count(
            issues, EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING
        ),
        CONFIRMED_WITHOUT_EXECUTABLE_PLAN: _count(issues, CONFIRMED_WITHOUT_EXECUTABLE_PLAN),
        NO_CANDIDATE_WITH_CANDIDATE_PLAN: _count(issues, NO_CANDIDATE_WITH_CANDIDATE_PLAN),
        EXECUTABLE_STATUS_WITHOUT_PLAN: _count(issues, EXECUTABLE_STATUS_WITHOUT_PLAN),
        OPPORTUNITY_WATCH_NOT_MATERIALIZED: _count(issues, OPPORTUNITY_WATCH_NOT_MATERIALIZED),
        OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH: _count(
            issues, OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH
        ),
        OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION: _count(issues, OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION),
        EXECUTION_FUNNEL_STARVATION: _count(issues, EXECUTION_FUNNEL_STARVATION),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
        "layer_counts": layer_counts,
    }
    # 07-10 R6-E (P1-3 #6): reproducibility contract. The explicit per-code keys
    # above are a hand-maintained allowlist that historically drifted -- the
    # three semantic-accuracy structured-state codes (MISSING_STRUCTURED_FIELD,
    # CANONICAL_SUMMARY_DRIFT, RENDERED_SUMMARY_DRIFT) are emitted by
    # _check_summary_structured_state_mismatch yet were never indexed into the
    # summary, so their counts were silently lost AND never rendered in the
    # hourly report (the §3.6 "rendered summary counters disagree with the real
    # decisions" defect class). The authoritative reproducible view is
    # ``per_code``: derived directly from ``issues`` so it can never drift, and
    # every code present in issues is lifted to the top-level summary so the
    # hourly-report renderer (which iterates summary items and prints
    # ``{code}={count}``) surfaces every fired code. Existing explicit keys are
    # preserved for backward compatibility and pinned to their recount.
    per_code: dict[str, int] = {}
    for i in issues:
        per_code[i["type"]] = per_code.get(i["type"], 0) + 1
    summary["per_code"] = per_code
    for _code, _recount in per_code.items():
        # Pin every existing explicit key to its recount (defense-in-depth: a
        # future allowlist drift surfaces loudly here, not silently).
        if _code in summary and summary[_code] != _recount:
            summary[_code] = _recount
        # Lift any fired code that the allowlist forgot to the top level so the
        # renderer displays it (fixes the three structured-state codes).
        elif _code not in summary:
            summary[_code] = _recount
    return {
        "ok": error_count == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
        "layer_counts": layer_counts,
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

# 07-10 S7 (P1 #7): independent fair-scheduling + context-continuity contract
# marker. The three new S1-S6 production-chain checks use this cutoff, NOT the
# R4 / semantic-accuracy / continuity boundary. Rows persisted before this
# marker are demoted to ``legacy_info``; rows after are evaluated against the
# full S1-S6 contract.
LLM_FAIR_SCHEDULING_CONTRACT_MARKER_KEY = "llm_fair_scheduling_context_contract_v1"

# 07-31 P1-4: independent schema-repair / breaker / preset integrity marker.
# Production evidence #4: the pre-fix batch 15m:1785487499999 (5 schema
# failures polluting the breaker rate window -> breaker open -> 10
# breaker_skipped rows with provider_call_count=0) repeated every hour as
# current llm_failure_rate_high + llm_circuit_breaker_open errors. Post-fix
# those failures are repairable/isolated, so pre-deployment historical
# batches must NOT repeat as current errors. This marker's applied_at is the
# cutoff for the two LLM diagnostics (_check_llm_failure_rate_high /
# _check_llm_circuit_breaker_open): marker-BEFORE batches demote to
# legacy_info; marker-AFTER stay current errors; marker-MISSING is
# fail-closed (marker-missing error + the two checks SKIPPED - no silent
# green, no false current errors against an undeployed contract). Written by
# initialize_database via _ensure_llm_schema_breaker_preset_integrity_marker
# (release path only).
LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY = (
    "llm_schema_breaker_preset_integrity_v1"
)
LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING = (
    "llm_schema_breaker_preset_integrity_marker_missing"
)

# 08-02 P1-3: independent execution-funnel report-contract marker. Written by
# initialize_database via _ensure_execution_funnel_report_contract_marker
# (release path only — production is NOT written this round). Its applied_at is
# the cutoff for the five per-decision execution-funnel codes:
# marker-BEFORE rows demote to legacy_info; marker-AFTER stay current errors;
# marker-MISSING is fail-closed (marker-missing error + the six execution-funnel
# checks SKIPPED — no silent green, no false current errors against an
# undeployed contract).
EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY = "execution_funnel_report_contract_v1"

# 07-22 Codex P1-1 / P2 exclude-only: independent provider-timeout envelope
# contract marker. Written by initialize_database via
# _ensure_llm_provider_timeout_envelope_contract_marker. SQL lower bound for
# llm_timeout_config_out_of_range: pre-marker rows stay in ga_decisions for
# audit but never enter current issues (no error, no legacy_info); post-marker
# pcc>=1 timeout_ms not in (0, cap] = error. Distinct from the fair-scheduling
# marker so historical d49 is excluded without coupling Phase F cutoffs.
LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_KEY = (
    "llm_provider_timeout_envelope_contract_v2"
)

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

# 08-02 P1-3: the set of issue types that fall under the execution-funnel
# report contract. Used by _apply_execution_funnel_marker_cutoff to demote
# pre-contract findings to legacy_info. Excludes the marker-missing code
# (always error) and execution_funnel_starvation (aggregate/live over a 24h
# window — recency is already bounded, so it is NOT cut off).
_EXECUTION_FUNNEL_ISSUE_TYPES: frozenset[str] = frozenset({
    CONFIRMED_WITHOUT_EXECUTABLE_PLAN,
    NO_CANDIDATE_WITH_CANDIDATE_PLAN,
    EXECUTABLE_STATUS_WITHOUT_PLAN,
    OPPORTUNITY_WATCH_NOT_MATERIALIZED,
    OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH,
    OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION,
})


def _get_r4_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Return the R4 contract marker's applied_at timestamp, or None."""
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (R4_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _get_semantic_accuracy_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Phase E: return the semantic-accuracy marker's applied_at, or None.

    None means the marker has not been deployed — callers (the five new
    checks) skip themselves in that case so historical data is not flagged
    with ``error`` severity against a contract that has not yet been
    initialized. The marker-missing check separately surfaces the absence.
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (SEMANTIC_ACCURACY_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _semantic_check_created_at_lower_bound(repo: CryptoGuardRepository) -> str:
    """07-10 R6-E (P1-3 #4): lower bound on ``ga_decisions.created_at`` for
    the five semantic-accuracy checks, applied in SQL BEFORE ``LIMIT``.

    When the semantic-accuracy marker is deployed, the bound is the marker's
    ``applied_at`` — only post-marker rows are current; pre-marker rows are
    historical audit and MUST NOT be fetched at all. The pre-fix SQL
    ``ORDER BY id DESC LIMIT 200`` had no time/marker bound, so on a fresh DB
    (no marker, no demotion) it fetched 200 historical rows and emitted them as
    current ``error`` — the ``bias_stage_semantic_conflict=200`` noise in the
    incident report.

    When the marker is absent (fresh DB / pre-deployment), the bound is
    ``now_utc - 24h`` — the same window the Phase-I LLM-attempt checks use
    (``_LLM_DIAGNOSTIC_WINDOW_MS``). This prevents a stale historical conflict
    row from being fetched and emitted as a current ``error`` against a
    contract that has not been initialized.

    Returns an ISO-ish timestamp string. Callers compare with
    ``created_at >= %s::timestamptz`` so the bound is format-agnostic:
    PostgreSQL casts the ISO-8601 param string to ``timestamptz`` before
    comparing against the ``TIMESTAMPTZ`` column, so a raw string comparison
    cannot be fooled by the separator/zone difference. With the SQL bound in
    place, ``_apply_semantic_marker_cutoff`` becomes a redundant safety net
    (pre-marker rows are no longer fetched) — it is retained as
    defense-in-depth and stays a no-op.
    """
    marker_ts = _get_semantic_accuracy_marker_ts(repo)
    if marker_ts is not None:
        return marker_ts
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    return datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_continuity_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Phase H: return the decision-context-continuity marker's applied_at, or None.

    None means the marker has not been deployed — callers (the seven new
    Phase A-G contract checks) skip themselves in that case so historical
    data is not flagged with ``error`` severity against a contract that has
    not yet been initialized. The marker-missing check
    (_check_plan_lifecycle_contract_markers_missing) separately surfaces
    the absence as an error.
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (CONTINUITY_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _get_llm_fair_scheduling_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """07-10 S7 (P1 #7): return the fair-scheduling + context-continuity
    marker's applied_at, or None.

    None means the marker has not been deployed - callers (the three new
    S1-S6 contract checks) skip themselves so historical data is not flagged
    with ``error`` severity against a contract that has not been initialized.
    The marker-missing check
    (_check_llm_fair_scheduling_contract_markers_missing) separately surfaces
    the absence as an error.
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (LLM_FAIR_SCHEDULING_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _llm_attempt_check_created_at_lower_bound(repo: CryptoGuardRepository) -> str:
    """07-10 R6-E (P1-3 #4 second half): lower bound on
    ``ga_decisions.created_at`` for the attempt-metadata checks
    (_check_llm_success_missing_attempt_metadata,
    _check_llm_continuity_not_included,
    _check_llm_repair_counted_as_provider_call), applied in SQL BEFORE
    ``LIMIT``.

    NOTE (07-22 Codex P1-1 / P2): ``_check_llm_timeout_config_out_of_range`` no
    longer uses this bound. It uses
    ``_llm_timeout_envelope_check_created_at_lower_bound`` keyed to the
    independent ``llm_provider_timeout_envelope_contract_v2`` marker so
    historical d49 is SQL-excluded (not demoted) without coupling to the
    fair-scheduling cutoff.

    The attempt-metadata checks read ``ga_decisions ORDER BY id DESC LIMIT 200``
    with no time/marker bound and relied SOLELY on
    ``_apply_llm_fair_scheduling_marker_cutoff`` to demote pre-marker rows to
    ``legacy_info`` after fetch. But the per-code summary count
    ``_count(issues, CODE)`` counts ALL severities (including ``legacy_info``),
    so pre-marker rows still inflated the rendered summary count - the
    ``llm_success_missing_attempt_metadata=32`` cumulative-count noise in the
    incident report (§3.6 "report diagnostics mix current failures with
    history").

    This bound mirrors ``_semantic_check_created_at_lower_bound`` but uses the
    fair-scheduling contract marker: when the marker is deployed, the bound is
    the marker's ``applied_at`` (only post-marker rows are current; pre-marker
    rows are historical audit and MUST NOT be fetched). When the marker is
    absent (fresh DB / pre-deployment), the bound is ``now_utc - 24h``
    (``_LLM_DIAGNOSTIC_WINDOW_MS``) so a stale historical row is not fetched and
    emitted as a current ``error`` against an uninitialized contract.

    With the SQL bound in place, ``_apply_llm_fair_scheduling_marker_cutoff``
    becomes a redundant safety net (pre-marker rows are no longer fetched) - it
    is retained as defense-in-depth and stays a no-op for these codes.
    """
    marker_ts = _get_llm_fair_scheduling_contract_marker_ts(repo)
    if marker_ts is not None:
        return marker_ts
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    return datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_llm_provider_timeout_envelope_contract_marker_ts(
    repo: CryptoGuardRepository,
) -> str | None:
    """07-22 Codex P1-1: return the provider-timeout envelope marker's
    applied_at, or None.

    None means the marker has not been deployed — the marker-missing check
    (_check_llm_provider_timeout_envelope_contract_markers_missing) surfaces
    the absence as an error (fail-closed). Callers of the timeout-config check
    use this as the SQL lower bound so pre-marker rows are never counted as
    current errors.
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _get_llm_schema_breaker_preset_integrity_marker_ts(
    repo: CryptoGuardRepository,
) -> str | None:
    """07-31 P1-4: return the schema/breaker/preset integrity marker's
    applied_at, or None.

    None means the marker has not been deployed — the two LLM diagnostics
    (_check_llm_failure_rate_high / _check_llm_circuit_breaker_open) skip
    themselves so an undeployed contract is never evaluated as current, and
    the marker-missing check
    (_check_llm_schema_breaker_preset_integrity_marker_missing) surfaces the
    absence as a fail-closed error (no silent green).
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _get_execution_funnel_report_contract_marker_ts(
    repo: CryptoGuardRepository,
) -> str | None:
    """08-02 P1-3: return the execution-funnel report-contract marker's
    applied_at, or None.

    None means the contract has not been deployed — the six execution-funnel
    checks skip themselves so an undeployed contract is never evaluated as
    current, and the marker-missing check
    (_check_execution_funnel_report_contract_marker_missing) surfaces the
    absence as a fail-closed error (no silent green).
    """
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if row and row["applied_at"]:
        return str(row["applied_at"])
    return None


def _execution_funnel_check_created_at_lower_bound(repo: CryptoGuardRepository) -> str:
    """08-02 P1-3: lower bound on ``ga_decisions.created_at`` for the four
    per-decision execution-funnel checks, applied in SQL BEFORE ``LIMIT``.

    When the execution-funnel report-contract marker is deployed, the bound is
    the marker's ``applied_at`` — only post-marker rows are current; pre-marker
    rows are historical audit and MUST NOT be fetched (mirrors
    ``_semantic_check_created_at_lower_bound``). When the marker is absent
    (fresh DB / pre-deployment), the bound is ``now_utc - 24h``
    (``_LLM_DIAGNOSTIC_WINDOW_MS``) so a stale historical row is not fetched and
    emitted as a current ``error`` against an uninitialized contract.

    Codex P2-1 (terminal-review rework): a CORRUPT/unparseable marker value
    FAILS CLOSED to ``now`` (nothing is provably post-marker), mirroring
    ``_execution_funnel_starvation_lower_bound_ts``. The raw string is never
    interpolated into ``created_at >= %s::timestamptz`` unvalidated (a garbage
    literal would crash psycopg); the corruption itself is surfaced by
    ``_check_execution_funnel_report_contract_marker_missing``.
    """
    marker_ts = _get_execution_funnel_report_contract_marker_ts(repo)
    if marker_ts is not None:
        try:
            datetime.fromisoformat(str(marker_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return marker_ts
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    return datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_execution_funnel_report_contract_marker_missing(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: fail-closed marker-missing check for the execution-funnel
    report contract. Runs FIRST (before the six execution-funnel checks) so a
    missing contract is explicitly surfaced even when the other checks would
    otherwise pass (or skip).

    Codex P2-1 (terminal-review rework): a PRESENT but CORRUPT/unparseable
    marker is surfaced too (``issue=marker_corrupt``) — never SILENT GREEN.
    With a corrupt marker the six funnel checks' lower bound fails closed to
    ``now`` (evaluate nothing), so without this issue the corruption would be
    invisible; the fail-closed contract requires it be surfaced.
    """
    issues: list[dict[str, Any]] = []
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s",
        (EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,),
    ).fetchone()
    if not row or not row["applied_at"]:
        issues.append(_issue(
            EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING, "error",
            {
                "marker_key": EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,
                "contract": "execution-funnel-report-contract",
                "issue": "marker_absent",
            },
            "执行漏斗报告契约 marker 未部署。运行 initialize_database() 写入 "
            f"{EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY}；marker 缺失时 6 项"
            "执行漏斗诊断被 SKIP（未部署契约不得评估为当前错误），避免历史行重复成当前错误。",
        ))
        return issues
    try:
        datetime.fromisoformat(str(row["applied_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        issues.append(_issue(
            EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING, "error",
            {
                "marker_key": EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,
                "contract": "execution-funnel-report-contract",
                "issue": "marker_corrupt",
                "applied_at": str(row["applied_at"]),
            },
            "执行漏斗报告契约 marker 值损坏（不可解析为时间戳）。运行 "
            "initialize_database() 重写正确的 applied_at；损坏 marker 下 4 项"
            "逐行执行漏斗诊断与 starvation 的 lower bound 均 fail-closed 到 now，"
            "不评估任何行（无静默 fail-open）。",
        ))
    return issues


def _apply_execution_funnel_marker_cutoff(
    repo: CryptoGuardRepository,
    issues: list[dict[str, Any]],
) -> None:
    """08-02 P1-3: demote pre-contract execution-funnel findings to legacy_info.

    Scoped to the five per-decision execution-funnel codes in
    ``_EXECUTION_FUNNEL_ISSUE_TYPES`` (starvation is aggregate/live over 24h
    and is NOT cut off). Per-decision findings carry a ``decision_id`` ->
    demote via ``_get_decision_created_ts``; the opportunity-watch-untriggerable
    finding carries NO decision_id -> demote via ``details.watch_created_at``
    (int ms, from the opportunity_watches.created_at the check emits). With
    the SQL lower bound in place this is a redundant safety net for the four
    per-decision checks and the ONLY demotion path for the watch-table scan.
    """
    marker_ts = _get_execution_funnel_report_contract_marker_ts(repo)
    if marker_ts is None:
        return
    try:
        # psycopg TIMESTAMPTZ -> str(datetime) is space- or T-separated ISO;
        # normalize to a real datetime so the comparison never depends on the
        # string separator (mirrors the P1-4 cutoff, not a lexicographic str
        # compare across mixed formats).
        marker_dt = datetime.fromisoformat(str(marker_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return
    for issue in issues:
        if issue.get("type") not in _EXECUTION_FUNNEL_ISSUE_TYPES:
            continue
        decision_id = (issue.get("details") or {}).get("decision_id")
        if decision_id is None:
            # Watch-table scan finding: demote via the watch's created_at (int
            # ms) — the only demotion path for the NOT-SQL-bound scan.
            at_ms = (issue.get("details") or {}).get("watch_created_at")
            if at_ms is None:
                at_ms = (issue.get("details") or {}).get("analysis_time")
            if at_ms is None:
                continue
            try:
                dt = datetime.fromtimestamp(int(at_ms) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
        else:
            decision_ts = _get_decision_created_ts(repo, decision_id)
            if decision_ts is None:
                continue
            try:
                dt = datetime.fromisoformat(str(decision_ts).replace("Z", "+00:00"))
            except (TypeError, ValueError, OSError):
                continue
        if dt < marker_dt:
            _demote_to_legacy_info(issue)


def _apply_llm_schema_breaker_preset_integrity_marker_cutoff(
    repo: CryptoGuardRepository,
    issues: list[dict[str, Any]],
) -> None:
    """07-31 P1-4 + final review P1-3: demote pre-marker LLM-diagnostic
    findings to legacy_info.

    Scoped to the two LLM diagnostics (_check_llm_failure_rate_high /
    _check_llm_circuit_breaker_open) driven by production evidence #4. Both
    checks read ``analysis_batches.summary_json.llm_health`` and emit
    ``details.runtime_timestamp_ms`` (int ms) — the batch's RUNTIME/outcome
    timeline (COALESCE(started_at, finished_at, created_at)) — with no
    ``decision_id``, so the cutoff compares that runtime timestamp against
    the marker's ``applied_at``. ``analysis_time`` is ONLY a market-data
    snapshot and must never drive the split (P1-3: it can disagree with the
    runtime clock by hours; production batches also carry started_at /
    finished_at / created_at).

    A batch whose runtime timeline is before the marker is historical audit,
    never a current error (symptom #4: no hourly repeat of the pre-fix
    breaker-open batch). A MISSING or UNPARSEABLE ``runtime_timestamp_ms``
    fails CLOSED: the finding stays a current error — it is never silently
    archived on a guess (P1-3 ④). When the marker is absent this is a no-op
    — the marker-missing check already surfaces the absence as an error and
    the two checks skip themselves.
    """
    marker_ts = _get_llm_schema_breaker_preset_integrity_marker_ts(repo)
    if marker_ts is None:
        return
    try:
        # psycopg TIMESTAMPTZ -> str(datetime) is space- or T-separated ISO;
        # normalize the same way the P1-4 test parses the cutoff.
        marker_dt = datetime.fromisoformat(str(marker_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return
    scoped = {LLM_FAILURE_RATE_HIGH, LLM_CIRCUIT_BREAKER_OPEN}
    for issue in issues:
        if issue.get("type") not in scoped:
            continue
        runtime_ms = (issue.get("details") or {}).get("runtime_timestamp_ms")
        if runtime_ms is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(runtime_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if dt < marker_dt:
            _demote_to_legacy_info(issue)


def _check_llm_schema_breaker_preset_integrity_marker_missing(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """07-31 P1-4: flag a missing schema/breaker/preset integrity marker.

    Mirrors ``_check_llm_fair_scheduling_contract_markers_missing``. The
    marker ``llm_schema_breaker_preset_integrity_v1`` must exist in
    ``_migration_state`` (written by ``initialize_database`` on the release
    path). If absent, emit an ``error`` (fail-closed) so callers detect the
    missing contract rather than receiving a silently-healthy report — the
    two LLM diagnostics skip themselves while the marker is absent, so this
    error is the only LLM signal.
    """
    issues: list[dict[str, Any]] = []
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
        (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,),
    ).fetchone()
    if not row or not row["applied_at"]:
        issues.append(_issue(
            LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING, "error",
            {
                "marker_key": LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,
                "contract": "llm-schema-repair-breaker-preset-integrity",
                "issue": "marker_absent",
            },
            "schema-repair/breaker/preset integrity marker 未部署。"
            "运行 initialize_database() 部署 llm_schema_breaker_preset_integrity_v1；"
            "marker 缺失时 llm_failure_rate_high / llm_circuit_breaker_open "
            "诊断被 SKIP（未部署契约不得作为当前评估），避免假绿。",
        ))
    return issues


def _llm_timeout_envelope_check_created_at_lower_bound(
    repo: CryptoGuardRepository,
) -> str:
    """07-22 Codex P1-1 / P2 exclude-only: lower bound on
    ``ga_decisions.created_at`` for ``_check_llm_timeout_config_out_of_range``,
    applied in SQL BEFORE ``LIMIT``.

    Unique contract for the timeout envelope: pre-marker rows remain in
    ``ga_decisions`` for audit but must NEVER enter current
    ``diagnose_report_accuracy`` issues — not as ``error`` and not as
    ``legacy_info``. The SQL bound is the sole mechanism (exclude-only; no
    demotion pass). When the marker is deployed, the bound is its
    ``applied_at``. When absent, the bound is ``now_utc - 24h`` so ancient
    rows cannot silently re-surface; the marker-missing check still emits
    fail-closed error independently.
    """
    marker_ts = _get_llm_provider_timeout_envelope_contract_marker_ts(repo)
    if marker_ts is not None:
        return marker_ts
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    return datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_llm_fair_scheduling_marker_cutoff(repo: CryptoGuardRepository, issues: list[dict[str, Any]]) -> None:
    """07-10 S7 (P1 #7): demote pre-marker fair-scheduling-contract findings
    to ``legacy_info``.

    Scoped to the three new S1-S6 contract issue types
    (fair_path_continuity_real_injection, per_job_failure_consistency; the
    batch_claim_ownership_integrity check lives in state_consistency.py and
    applies its own cutoff in SQL). When the marker is absent this is a no-op
    - the marker-missing check already surfaces the absence as an error.
    """
    marker_ts = _get_llm_fair_scheduling_contract_marker_ts(repo)
    if marker_ts is None:
        return
    # 07-10 P1-1: the formal Phase F diagnostic codes (design §10) are scoped
    # to this same marker cutoff so pre-marker findings demote to legacy_info
    # alongside the three S1-S6 contract checks.
    # 07-22 Codex P1-1 / P2: LLM_TIMEOUT_CONFIG_OUT_OF_RANGE is NO LONGER in
    # this set — it has its own envelope marker and SQL exclude-only bound
    # (no demotion-to-legacy_info path for that code).
    scoped = {
        FAIR_PATH_CONTINUITY_REAL_INJECTION,
        PER_JOB_FAILURE_CONSISTENCY,
        LLM_FIRST_ATTEMPT_COVERAGE_LOW,
        LLM_SYMBOL_STARVATION,
        LLM_REPORT_COUNT_MISMATCH,
        LLM_SUCCESS_MISSING_ATTEMPT_METADATA,
        LLM_CONTINUITY_NOT_INCLUDED,
        LLM_BATCH_DEGRADED_REPORTED_HEALTHY,
        LLM_REPAIR_COUNTED_AS_PROVIDER_CALL,
    }
    for issue in issues:
        if issue.get("type") not in scoped:
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
            _demote_to_legacy_info(issue)


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
            _demote_to_legacy_info(issue)


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
            _demote_to_legacy_info(issue)


def _get_decision_created_ts(repo: CryptoGuardRepository, decision_id: int | None) -> str | None:
    """Return the created_at timestamp for a ga_decisions row, or None."""
    if decision_id is None:
        return None
    row = repo.conn.execute(
        "SELECT created_at FROM ga_decisions WHERE id=%s",
        (int(decision_id),),
    ).fetchone()
    if row and row["created_at"]:
        return str(row["created_at"])
    return None


def run_for_report(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> dict[str, Any]:
    """Wrapper for render-time invocation; never raises.

    P1-11e: returns ok=False on error (fail-closed).
    """
    try:
        return diagnose_report_accuracy(repo, batch_id=batch_id)
    except Exception as exc:
        # A PostgreSQL statement error aborts the current transaction until an
        # explicit rollback. Reporting fail-closed is not enough if we return
        # an unusable pooled connection to the caller.
        try:
            repo.conn.rollback()
        except Exception:
            pass
        issue = _issue(
            DIAGNOSTIC_QUERY_FAILED,
            "error",
            {"error_type": type(exc).__name__},
            "报告准确性诊断查询失败；已故障关闭。检查 PostgreSQL 连接、事务状态和 SQL。",
        )
        return {
            "ok": False,
            "error": f"diagnostic query failed ({type(exc).__name__})",
            "summary": {DIAGNOSTIC_QUERY_FAILED: 1, "error_count": 1},
            "total_issues": 1,
            "error_count": 1,
            "warning_count": 0,
            "legacy_info_count": 0,
            "issues": [issue],
        }


# ── issue codes omit "rate,"; for schema simplicity keep both forms documented. ──
def _count(issues: list[dict[str, Any]], code: str) -> int:
    return sum(1 for i in issues if i["type"] == code)


def _issue(code: str, severity: str, details: dict[str, Any], action: str) -> dict[str, Any]:
    # Phase E (07-09): surface a ``layer`` field so report renderers can
    # group issues into current / warning / legacy_audit buckets. The
    # mapping is derived from ``severity`` (which is already demoted to
    # ``legacy_info`` by the marker cutoff functions for pre-marker rows).
    return {
        "type": code,
        "severity": severity,
        "layer": _layer_for_severity(severity),
        "details": details,
        "suggested_action": action,
    }


def _layer_for_severity(severity: str) -> str:
    """Map a severity value to its diagnostic layer bucket."""
    if severity == "legacy_info":
        return "legacy_audit"
    if severity == "warning":
        return "warning"
    return "current"


def _demote_to_legacy_info(issue: dict[str, Any]) -> None:
    """Demote an issue's severity to legacy_info and refresh its layer."""
    issue["severity"] = "legacy_info"
    issue["layer"] = "legacy_audit"


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
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='completed'",
                (bid,),
            ).fetchall()
        ]
        failed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='failed'",
                (bid,),
            ).fetchall()
        ]
        pending_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='pending'",
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
                "SELECT analysis_time FROM analysis_batches WHERE batch_id=%s LIMIT 1",
                (batch_id,),
            ).fetchone()
        except Exception:
            raise
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
            "FROM ga_decisions WHERE batch_id=%s ORDER BY id DESC LIMIT 120",
            (batch_id,),
        ).fetchall()
    else:
        min_time = cutoff - span
        rows = repo.conn.execute(
            "SELECT id, symbol, analysis_time, signal_grade, batch_id "
            "FROM ga_decisions WHERE analysis_time >= %s ORDER BY id DESC LIMIT 120",
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
        WHERE analysis_time >= %s
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
        WHERE analysis_time >= %s
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
    rows = repo.conn.execute(
        """
        SELECT module, timeframe, analysis_time, result_json
        FROM module_analysis_results
        WHERE snapshot_id=%s AND symbol=%s AND module IN (%s, %s, %s)
        ORDER BY timeframe
        """,
        (int(snapshot_id), symbol, *_SNAPSHOT_EVENT_MODULES),
    ).fetchall()

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
                dt = datetime.fromisoformat(str(raw_str).replace("Z", "+00:00"))
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
        raise
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
                "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
                (key,),
            ).fetchone()
        except Exception:
            raise
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
    # 07-10 R6-E (P1-3 #4): apply the marker/time bound BEFORE LIMIT so a
    # stale historical conflict row is not fetched at all. See
    # _semantic_check_created_at_lower_bound for the rationale.
    bound = _semantic_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, market_bias, trend_stage, created_at
        FROM ga_decisions
        WHERE market_bias IN ('neutral', 'mixed', 'unknown')
          AND trend_stage IN ('early', 'middle', 'late')
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
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
    """Phase E / conventions §38.7: flag LONG/SHORT decisions with directional
    S/A/B grade while ``htf_conflict=True`` and confidence is not capped at or
    below the paper-order threshold.

    Per Contract 38.7 and PRD FR-3, a countertrend rebound must not reach
    executable-level confidence purely on 15M/1H momentum. Phase B P1-3
    (07-22) tightens the check to match §38.7 literally; Codex P2-1 (07-22)
    further requires a directional ``market_bias`` on the production shape:

    - require explicit ``htf_conflict is True`` on the persisted decision
      (do not infer solely from TF bias opposition — production d57 was
      grade C / htf_conflict=False / monitor_only and was over-flagged);
    - require signal_grade in {S, A, B} (C/D observation grades are out of
      scope for this executable-overconfidence code);
    - require confidence >= MIN_CONFIDENCE_FOR_PAPER_ORDER (uncapped);
    - production shape (``timeframe_context`` present): also require
      ``market_bias ∈ {bullish, bearish}``. A-grade neutral/unknown +
      htf_conflict=True must NOT fire (not a directional countertrend
      overconfidence). Legacy snapshot.profiles fixtures keep the pre-P2-1
      path so FAULTHTF still fires without a top-level market_bias.

    TF bias opposition is retained as a corroborating signal when present
    (legacy snapshot.profiles fixtures that omit the explicit flag still
    need a path to fire when the row is S/A/B + high confidence AND either
    the flag is set OR the production-shape TF conflict is present). The
    fault tests seed grade A + conf 0.85 + htf_conflict=True + directional
    market_bias (FAULTHTF2) or the legacy profile opposition (FAULTHTF).

    Detection parses ``raw_decision_json`` (the structured audit JSON), NOT
    the free-text ``final_summary``. Severity: ``error`` post-marker.
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    issues: list[dict[str, Any]] = []
    # 07-10 R6-E (P1-3 #4): marker/time bound BEFORE LIMIT.
    bound = _semantic_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, market_bias,
               raw_decision_json, created_at
        FROM ga_decisions
        WHERE confidence >= %s
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (MIN_CONFIDENCE_FOR_PAPER_ORDER, bound),
    ).fetchall()
    for r in rows:
        conf = float(r["confidence"] or 0)
        if conf < MIN_CONFIDENCE_FOR_PAPER_ORDER:
            continue
        grade = str(r["signal_grade"] or "").upper()
        # §38.7: only directional S/A/B grades are in scope.
        if grade not in {"S", "A", "B"}:
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
        # Explicit htf_conflict flag (production shape) OR nested copies.
        htf_conflict = raw.get("htf_conflict")
        if htf_conflict is None:
            for _blk_key in ("flags", "risk", "judge", "execution"):
                _blk = raw.get(_blk_key)
                if isinstance(_blk, dict) and "htf_conflict" in _blk:
                    htf_conflict = _blk.get("htf_conflict")
                    break
        htf_explicit = htf_conflict is True
        # Corroborating TF opposition (legacy fixtures without the flag).
        tf_conflict = False
        if bias_1d in {"bullish", "bearish"}:
            opposite = "bullish" if bias_1d == "bearish" else "bearish"
            low_tf_opposite = opposite in {bias_1h, bias_15m}
            # 4H must NOT confirm the low-TF direction to count as a conflict.
            if low_tf_opposite and bias_4h != opposite:
                tf_conflict = True
        # §38.7: require explicit htf_conflict=True. Legacy snapshot.profiles
        # fixtures historically omit the flag; accept TF-derived conflict only
        # for that legacy shape (no top-level timeframe_context) so FAULTHTF
        # still fires. Production shape (timeframe_context present) MUST carry
        # the explicit flag — TF inference alone is not enough (d57 FP).
        if isinstance(tf_ctx, dict) and tf_ctx:
            if not htf_explicit:
                continue
            # Codex P2-1: production shape also requires a directional
            # market_bias. Prefer the row column, fall back to raw envelope.
            market_bias = str(
                r["market_bias"] if r["market_bias"] is not None
                else raw.get("market_bias") or ""
            ).lower()
            if market_bias not in {"bullish", "bearish"}:
                continue
        else:
            if not (htf_explicit or tf_conflict):
                continue
        issues.append(_issue(
            HTF_COUNTERTREND_OVERCONFIDENCE, "error",
            {
                "decision_id": int(r["id"]), "symbol": r["symbol"],
                "grade": r["signal_grade"], "confidence": conf,
                "threshold": MIN_CONFIDENCE_FOR_PAPER_ORDER,
                "htf_conflict": bool(htf_explicit),
                "1d_bias": bias_1d, "1h_bias": bias_1h,
                "15m_bias": bias_15m, "4h_bias": bias_4h,
            },
            "§38.7: S/A/B 级且 htf_conflict=True 时不得保留执行级置信度；"
            "须触发置信度上限并降级。C/D、htf_conflict=False 或非方向性 "
            "market_bias（neutral/unknown/mixed）不在本码范围（生产 shape）。",
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
    # 07-10 R6-E (P1-3 #4): marker/time bound BEFORE LIMIT.
    bound = _semantic_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, market_bias, trend_stage, decision,
               confidence, analysis_time, final_summary, rendered_summary,
               raw_decision_json, created_at
        FROM ga_decisions
        WHERE created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
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
    # 07-10 R6-E (P1-3 #4): marker/time bound BEFORE LIMIT.
    bound = _semantic_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, market_bias, trend_stage,
               final_summary, rendered_summary, created_at
        FROM ga_decisions
        WHERE (decision IN ('monitor_only', 'opportunity_watch', 'no_edge',
                           'watch_only', 'add_to_watchlist', 'ignore')
           OR decision NOT IN ('create_paper_order', 'trade_plan_available'))
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
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
    # 07-10 R6-E (P1-3 #4): marker/time bound BEFORE LIMIT.
    bound = _semantic_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, batch_id,
               final_summary, rendered_summary, analysis_time, created_at
        FROM ga_decisions
        WHERE (signal_grade IN ('C', 'D')
           OR decision = 'no_edge')
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 300
        """,
        (bound,),
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
    # JSONB columns come back from psycopg3 as already-decoded list/dict (NOT
    # str); ``json.loads(list)`` raises TypeError inside the bare ``except`` ->
    # returns [] -> the check silently skips every row (false green, the SC-4
    # bug). Pass list/dict through; only parse str. Mirrors state_consistency.
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, dict):
        # enabled_symbols_json is a list at the JSON root; a dict root is not a
        # list-shape contract, so treat as empty rather than guessing keys.
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
    # JSONB pass-through: psycopg3 decodes JSONB columns to dict/list already,
    # so ``json.loads(dict)`` would raise TypeError and (via the bare except)
    # return None -> every JSONB-backed check silently skips its rows. Accept
    # dict/list as-is; only str needs parsing. Mirrors state_consistency.
    if isinstance(raw, (dict, list)):
        return raw
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
            "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
            (CONTINUITY_CONTRACT_MARKER_KEY,),
        ).fetchone()
    except Exception:
        raise
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
                "WHERE batch_id=%s AND status='completed' LIMIT 50",
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
                WHERE gd.batch_id=%s AND gd.symbol=%s
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
          AND started_at < NOW() + %s::interval
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


def _batch_runtime_ms(row: dict[str, Any]) -> int | None:
    """07-31 final review P1-3: the runtime/outcome timestamp of an
    ``analysis_batches`` row, in epoch ms.

    Uses ``COALESCE(started_at, finished_at, created_at)`` — the deployment
    cutoff compares this RUNTIME timeline against the marker applied_at,
    NEVER ``analysis_time`` (a market-data snapshot that can disagree with
    the runtime clock). Returns None when no runtime column is populated or
    a value fails to convert (e.g. ``'infinity'::timestamptz``) — callers
    must fail CLOSED on None (keep the finding current), never archive it.
    """
    for col in ("started_at", "finished_at", "created_at"):
        value = row.get(col)
        if value is None:
            continue
        try:
            return int(value.timestamp() * 1000)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    return None


def _check_llm_failure_rate_high(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag batches whose LLM failure rate >= 50% over the latest 10 calls.

    Per PRD AC18 / R3, the batch-level circuit breaker opens when the failure
    rate exceeds 50% over the latest 10 LLM calls. This diagnostic surfaces
    that condition post-hoc so operators can see breaker-quality failures
    even when the breaker itself was not queried (e.g., legacy batches).

    Detection reads ``analysis_batches.summary_json.llm_health`` from the
    latest 5 batches.

    07-31 final review P2-1: the recent_10_* family is evaluated by FIELD
    PRESENCE, never by the value 0 masquerading as "missing":

    - recent_10_calls / recent_10_failure_rate PRESENT (any value, incl. 0)
      -> the recent-10 path governs EXCLUSIVELY; a healthy (< 0.5) or
      under-sampled (< 3 calls) recent window is a current fact -> no issue,
      no whole-batch fallback, never labelled legacy.
    - family genuinely MISSING (legacy pre-Phase-I shapes) AND total >= 10
      -> whole-batch fallback allowed, explicitly labelled legacy.
    - the breaker-driving rate (recent_10_failure_rate) and the overall LLM
      outcome rate (whole_batch_failure_rate) are separately named with an
      explicit rate_source; the ambiguous single ``failure_rate`` key is
      gone. Each issue also carries ``runtime_timestamp_ms`` (P1-3) for the
      marker cutoff.

    P1-4 (07-31): scoped to the schema/breaker/preset integrity marker. When
    the marker is ABSENT the check SKIPS entirely (returns []) — an
    undeployed contract must not be evaluated as current (no silent green,
    no false current errors against pre-deployment data). The marker-missing
    check surfaces the absence as a fail-closed error.

    Reviewer note (07-31, deliberate divergence): the primary-window floor is
    ``recent_10_calls >= 3``, STRICTER than the breaker's ``min_rate_samples``
    (5), so the diagnostic flags degraded infrastructure EARLIER than the
    breaker can open. It only ever flags MORE (conservative direction), never
    fewer, than the breaker itself.
    """
    issues: list[dict[str, Any]] = []
    if _get_llm_schema_breaker_preset_integrity_marker_ts(repo) is None:
        return issues
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time,
               started_at, finished_at, created_at, status, summary_json
        FROM analysis_batches
        WHERE analysis_time >= %s
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
        has_recent_10 = (
            health.get("recent_10_calls") is not None
            and health.get("recent_10_failure_rate") is not None
        )
        if has_recent_10:
            recent_10_calls = int(health["recent_10_calls"])
            recent_10_rate = float(health["recent_10_failure_rate"])
            # A PRESENT recent_10 family governs exclusively: fewer than 3
            # samples or a healthy rate is a current fact — never fall back
            # to the whole-batch rate (P2-1: presence is not 0-as-missing).
            if recent_10_calls < 3 or recent_10_rate < 0.5:
                continue
            rate = recent_10_rate
            window = f"latest {recent_10_calls} calls"
            rate_source = "recent_10"
        elif total >= 10:
            # Fields genuinely MISSING (legacy pre-Phase-I shape): whole-batch
            # fallback, explicitly labelled legacy.
            rate = failed / total
            window = f"whole batch ({total} calls, legacy)"
            rate_source = "whole_batch"
            if rate < 0.5:
                continue
        else:
            continue  # not enough samples to evaluate
        details: dict[str, Any] = {
            "batch_id": r["batch_id"] if r["batch_id"] else "",
            "primary_interval": r["primary_interval"],
            "analysis_time": int(r["analysis_time"] or 0),
            "total_attempts": total,
            "failed": failed,
            "window": window,
            "rate_source": rate_source,
            "runtime_timestamp_ms": _batch_runtime_ms(r),
            "dominant_error_category": health.get("dominant_error_category") or "",
        }
        if rate_source == "recent_10":
            details["recent_10_failure_rate"] = round(rate, 3)
        else:
            details["whole_batch_failure_rate"] = round(rate, 3)
        issues.append(_issue(
            LLM_FAILURE_RATE_HIGH, "error", details,
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
          AND analysis_time >= %s
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
          AND analysis_time >= %s
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

    P1-4 (07-31): scoped to the schema/breaker/preset integrity marker. When
    the marker is ABSENT the check SKIPS entirely (returns []) — an
    undeployed contract must not be evaluated as current (no silent green,
    no false current errors against pre-deployment data). The marker-missing
    check surfaces the absence as a fail-closed error.
    """
    issues: list[dict[str, Any]] = []
    if _get_llm_schema_breaker_preset_integrity_marker_ts(repo) is None:
        return issues
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time,
               started_at, finished_at, created_at, status, summary_json
        FROM analysis_batches
        WHERE analysis_time >= %s
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
                # 07-31 final review P1-3: the named runtime/outcome
                # timestamp for the marker cutoff (COALESCE(started_at,
                # finished_at, created_at)), never analysis_time.
                "runtime_timestamp_ms": _batch_runtime_ms(r),
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
    """Flag decisions where an executable plan was persisted
    (``has_trade_plan=True``) but ``plan_execution_state`` is NOT
    ``confirmed`` — i.e. the row is rendered/executable as a trade plan while
    the lifecycle state says it was never confirmed. This is the genuine
    report contradiction.

    Per PRD AC18 / R4, AC14 (R6-E P1-3 #5) and design §11.1, this diagnostic
    is data-driven: it reads ``ga_decisions.raw_decision_json`` fields
    directly, NOT rendered report text. Rendered text correctness is verified
    separately by the renderer unit test on ``_render_plan_state_label``.

    A valid unconfirmed deterministic candidate (``candidate_trade_plan``
    present, ``has_trade_plan=False``, ``plan_execution_state`` in
    ``unconfirmed`` / ``risk_rejected`` / ``invalidated``) is the fail-closed
    path and is NOT an error — the renderer already labels it
    "规则候选计划已生成，LLM 未确认，禁止执行". The pre-fix logic fired
    ``error`` on ANY non-confirmed/non-no_candidate state, counting a valid
    fail-closed decision as a defect (AC14 noise). It now fires ONLY on the
    genuine contradiction: ``has_trade_plan=True`` but
    ``plan_execution_state != "confirmed"``.

    Conditions for the diagnostic to fire (all must hold):
    - ``candidate_trade_plan`` is a non-empty dict (rule SOP produced a
      candidate).
    - ``has_trade_plan`` is True (an executable plan was persisted — the row
      can be rendered as a confirmed trade plan).
    - ``plan_execution_state`` is not ``confirmed`` (the lifecycle says it was
      not confirmed — the contradiction).
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= %s
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
        # AC14 (R6-E P1-3 #5): only flag when an executable plan was persisted
        # (has_trade_plan=True) yet the lifecycle state is not confirmed — the
        # genuine contradiction. A valid unconfirmed candidate
        # (has_trade_plan=False, state=unconfirmed/risk_rejected/invalidated)
        # is the fail-closed path and MUST NOT be an error.
        if not raw.get("has_trade_plan"):
            continue
        state = str(raw.get("plan_execution_state") or "")
        if state == "confirmed":
            continue
        issues.append(_issue(
            DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "analysis_time": int(r["analysis_time"] or 0),
                "plan_execution_state": state,
                "plan_origin": str(raw.get("plan_origin") or ""),
                "has_trade_plan": True,
                "candidate_trade_plan_present": True,
            },
            "规则候选计划已落库为可执行计划（has_trade_plan=True）但 "
            "plan_execution_state={state}≠confirmed："
            "小时报告必须渲染为 '规则候选计划已生成，LLM 未确认，禁止执行' 而非 '候选计划已生成（LLM 已确认）'。"
            f" (state={state})",
        ))
    return issues


# ---------------------------------------------------------------------------
# 08-02 P1-3: execution-funnel report contract (per-decision + aggregate
# checks). All six checks self-skip when the execution_funnel_report_contract_v1
# marker is absent (an undeployed contract is never evaluated as current); the
# marker-missing check surfaces the absence as a fail-closed error instead.
# The four per-decision checks are SQL-bound by the marker's applied_at (or a
# 24h fallback when the marker is absent, defense-in-depth); the watch-table
# scan is NOT SQL-bound and relies on the marker cutoff demotion path.
# ---------------------------------------------------------------------------

def _is_nonempty_dict(value: Any) -> bool:
    """True only for a dict that is not empty. ``None``, ``[]``, ``""``, ``{}``
    and scalar values all return False — mirrors the report renderer's
    ``trade_plan`` truthiness (a persisted plan must be a non-empty dict).
    """
    return isinstance(value, dict) and bool(value)


def _is_final_executable(raw: dict[str, Any]) -> bool:
    """08-02 P1-3: the single locked predicate for "row is a final executable
    plan". Mirrors ``hourly_report._decision_row.final_executable`` exactly:
    ``plan_execution_state == "confirmed"`` AND ``plan_status == "executable"``
    AND ``has_trade_plan`` truthy AND ``trade_plan`` a non-empty dict. Any
    other combination (state confirmed but no plan, state not confirmed but
    plan present, etc.) is a report-contract contradiction and NOT executable.
    """
    return (
        raw.get("plan_execution_state") == "confirmed"
        and raw.get("plan_status") == "executable"
        and bool(raw.get("has_trade_plan"))
        and _is_nonempty_dict(raw.get("trade_plan"))
    )


def _condition_is_untriggerable(cond: Any) -> bool:
    """08-02 P1-3: fail-closed "is this watch condition untriggerable?" —
    mirrors the watcher's ``_condition_hit`` semantics: a bare string, a
    non-dict, or an unknown kind can never trigger a structured watch and is
    therefore a defect (the watch can never fire).

    08-02 R2 P2-3 (fresh reviewer): a SUPPORTED kind with no usable
    level/price is untriggerable too — every ``_condition_hit`` branch is
    gated on a numeric level, so the watcher falls through to a permanent
    silent wait. Mirror ``is_structured_condition`` (level first, then price)
    and require a positive non-bool number.
    """
    from plugins.crypto_guard.reasoning.watch_conditions import SUPPORTED_WATCH_CONDITION_KINDS
    if isinstance(cond, str) or not isinstance(cond, dict):
        return True
    # 08-02 R2 review P2-2 + Finding 1 (brand-new reviewer): the by-design
    # account_feedback_recheck routing is handled at the CALLER as a row-level
    # root-dict skip (mirroring opportunity_watcher.py:82). Here the kind
    # simply falls through to the SUPPORTED-set comparison: account_feedback_
    # recheck is NOT a SUPPORTED kind, so ANY non-routed variant (root-list
    # item, kind-only, uppercase) is untriggerable — exactly what the watcher's
    # ``_condition_hit`` returns for those shapes. The kind is lowercased first
    # so a SUPPORTED kind spelled "Price_Above" is not a false positive.
    raw_kind = cond.get("type") or cond.get("kind")
    kind = str(raw_kind or "").lower()
    if kind not in SUPPORTED_WATCH_CONDITION_KINDS:
        return True
    level = cond.get("level")
    if level is None:
        level = cond.get("price")
    if not isinstance(level, (int, float)) or isinstance(level, bool) or float(level) <= 0:
        return True
    return False


def _check_confirmed_without_executable_plan(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: decisions rendered "LLM plan confirmed" (``llm_plan_verdict``
    confirmed / ``plan_execution_state`` confirmed) yet NOT final executable.
    The report row split shows ``llm_plan_confirmed`` and ``final_executable``
    as separate columns; a confirmed row with no executable trade plan is the
    "confirmed without plan" contradiction — the report must NOT imply an
    executable plan exists.
    """
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (lower_bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if raw.get("plan_execution_state") != "confirmed":
            continue
        if _is_final_executable(raw):
            continue
        issues.append(_issue(
            CONFIRMED_WITHOUT_EXECUTABLE_PLAN, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "plan_status": str(raw.get("plan_status") or ""),
                "has_trade_plan": bool(raw.get("has_trade_plan")),
                "trade_plan_present": _is_nonempty_dict(raw.get("trade_plan")),
                "llm_plan_verdict": str(raw.get("llm_plan_verdict") or ""),
                "risk_ok": bool((raw.get("risk_check") or {}).get("ok")),
                "effective_signal_grade": str(raw.get("effective_signal_grade") or r["signal_grade"] or ""),
            },
            "计划已确认（plan_execution_state=confirmed）但没有可执行 trade_plan："
            "小时报告必须把 llm_plan_confirmed 与 final_executable 拆开渲染，"
            "已确认≠可执行（可能后续风险门清空了计划）。",
        ))
    return issues


def _check_no_candidate_with_candidate_plan(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: decisions whose lifecycle is ``no_candidate`` yet still carry
    a candidate/trade plan (``candidate_trade_plan`` non-empty, ``trade_plan``
    non-empty, or ``has_trade_plan`` truthy). ``no_candidate`` means NO plan was
    produced — a persisted plan is the inverse contradiction of
    confirmed_without_executable_plan.
    """
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (lower_bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if raw.get("plan_execution_state") != "no_candidate":
            continue
        if (
            _is_nonempty_dict(raw.get("candidate_trade_plan"))
            or _is_nonempty_dict(raw.get("trade_plan"))
            or bool(raw.get("has_trade_plan"))
        ):
            issues.append(_issue(
                NO_CANDIDATE_WITH_CANDIDATE_PLAN, "error",
                {
                    "decision_id": int(r["id"]),
                    "symbol": r["symbol"],
                    "plan_status": str(raw.get("plan_status") or ""),
                    "has_trade_plan": bool(raw.get("has_trade_plan")),
                    "candidate_trade_plan_present": _is_nonempty_dict(raw.get("candidate_trade_plan")),
                    "trade_plan_present": _is_nonempty_dict(raw.get("trade_plan")),
                    "llm_plan_verdict": str(raw.get("llm_plan_verdict") or ""),
                },
                "生命周期 no_candidate 却仍带 candidate/trade plan："
                "no_candidate 表示无任何计划，残留计划是逆反矛盾。",
            ))
    return issues


def _check_executable_status_without_plan(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: decisions with ``plan_status == "executable"`` that are NOT
    final executable (missing confirmed state, missing/empty trade_plan, or
    ``has_trade_plan`` falsy). ``plan_status=executable`` without the full
    confirmed+plan predicate means the row claims executability the report
    split cannot render — same contradiction family, different entry field.
    """
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (lower_bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if raw.get("plan_status") != "executable":
            continue
        if raw.get("plan_execution_state") == "confirmed":
            # Single-emit precedence: a row that is both confirmed AND
            # executable-status but not final-executable is already claimed by
            # confirmed_without_executable_plan (the more descriptive
            # contradiction); do not double-report it here (08-02 review P2-D).
            continue
        if _is_final_executable(raw):
            continue
        issues.append(_issue(
            EXECUTABLE_STATUS_WITHOUT_PLAN, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "plan_execution_state": str(raw.get("plan_execution_state") or ""),
                "has_trade_plan": bool(raw.get("has_trade_plan")),
                "trade_plan_present": _is_nonempty_dict(raw.get("trade_plan")),
                "llm_plan_verdict": str(raw.get("llm_plan_verdict") or ""),
            },
            "plan_status=executable 但未通过 final-executable 判定："
            "缺少 confirmed 状态或 trade_plan 为空/缺失，报告不得渲染为可执行。",
        ))
    return issues


def _persisted_actions(raw: dict[str, Any]) -> list[str]:
    """Read the persisted suggested-action list from a §8 envelope.

    Controller-produced rows persist actions at TOP level under
    ``feishu_actions`` (decision_schema.py §8 envelope line 144);
    ``suggested_actions`` exists only in the nested ``raw_legacy_decision``
    and in the compat shape (legacy_decision_from_ga_decision line 258).
    Reading only ``suggested_actions`` silently skipped every controller row
    (08-02 Finding 5 evidence gap — the P1-3 watch check never fired on
    production data). Each candidate key is checked in priority order; a
    non-list value falls through to the next key; returns [] when none is a
    list. Post-fix rows always carry ``feishu_actions`` (list) at top level.
    """
    for key in ("feishu_actions", "suggested_actions"):
        value = raw.get(key)
        if isinstance(value, list):
            return value
    nested = raw.get("raw_legacy_decision")
    if isinstance(nested, dict):
        value = nested.get("suggested_actions")
        if isinstance(value, list):
            return value
    return []


def _check_opportunity_watch_not_materialized(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: decisions that gated the auto opportunity-watch materialization
    (the P0-2 wire-in: ``create_opportunity_watch`` suggested action, structured
    watch, effective grade in S/A/B, no open paper order for the symbol) yet no
    active opportunity_watch row exists. A missing active watch is a broken
    funnel: the decision promised a watch that was never materialized. Mirrors
    the wire-in gate conditions exactly so a skipped-by-design decision (open
    order present, unstructured watch, D/C grade) is NOT flagged.
    """
    from plugins.crypto_guard.reasoning.watch_conditions import is_structured_watch
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (lower_bound,),
    ).fetchall()
    symbols = list({r["symbol"] for r in rows if r["symbol"]})
    open_symbols: set[str] = set()
    if symbols:
        open_rows = repo.conn.execute(
            """
            SELECT DISTINCT symbol FROM paper_orders
            WHERE status IN ('pending', 'open', 'needs_recheck')
              AND symbol = ANY(%s)
            """,
            (symbols,),
        ).fetchall()
        open_symbols = {r["symbol"] for r in open_rows}
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        actions = _persisted_actions(raw)
        if "create_opportunity_watch" not in actions:
            continue
        watch = raw.get("opportunity_watch")
        if not is_structured_watch(watch):
            continue
        grade = str(raw.get("effective_signal_grade") or r["signal_grade"] or "").upper()
        if grade not in {"S", "A", "B"}:
            continue
        symbol = r["symbol"]
        if symbol and symbol in open_symbols:
            continue
        direction = str((watch or {}).get("direction") or "").upper()
        # Match the producer's exact key format (repository.upsert_auto_
        # opportunity_watch writes f"auto:{symbol}:{direction}" — lowercase
        # ``auto:`` prefix, canonical symbol, uppercase direction). Uppercasing
        # the whole key would miss the stored row and false-positive an
        # already-materialized watch (08-02 review P1-A).
        dedupe_key = f"auto:{symbol}:{direction}"
        found = None
        if dedupe_key and symbol:
            found = repo.conn.execute(
                """
                SELECT id FROM opportunity_watches
                WHERE status = 'active'
                  AND (dedupe_key = %s OR ga_decision_id = %s)
                LIMIT 1
                """,
                (dedupe_key, int(r["id"])),
            ).fetchone()
        if found is None:
            issues.append(_issue(
                OPPORTUNITY_WATCH_NOT_MATERIALIZED, "error",
                {
                    "decision_id": int(r["id"]),
                    "symbol": symbol,
                    "direction": direction,
                    "effective_signal_grade": grade,
                    "expected_dedupe_key": dedupe_key,
                },
                "决策门控了自动机会 watch 物化（create_opportunity_watch + 结构化 watch "
                "+ S/A/B + 无未平仓纸面单）但 opportunity_watches 无 active 行："
                "漏斗断裂，机会 watch 未落地。",
            ))
    return issues


def _check_opportunity_watch_advertised_without_watch(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 Finding 5 (P2): decisions that ADVERTISE ``create_opportunity_watch``
    in their persisted actions yet carry NO structured opportunity_watch (None,
    missing, or an unstructured dict such as ``{"needed": True, "direction":
    "bidirectional"}``).

    This is the decision-level broken-promise check — the COMPANION to
    ``_check_opportunity_watch_not_materialized`` (which requires a structured
    watch and proves materialization happened; unstructured/None watch is its
    skipped-by-design path). The P0-2 wire-in can only honor the action when a
    structured watch exists, so an advertised action without one is a permanent
    funnel dead-end the decision itself promises and can never deliver.

    The Finding-2 controller fix (controller.py) strips ``create_opportunity_watch``
    from feishu_actions whenever the watch is unstructured, so a row firing this
    code means either a pre-fix persisted row (historical audit — excluded here
    by the SQL lower bound) or a non-controller producer path that still writes
    the broken promise. Firing on EITHER is correct evidence: the invariant
    must hold across all persisted rows, not just the current code path.
    Mirrors the sibling per-decision checks: marker-gated, SQL-bound to
    post-marker rows, one issue per row.
    """
    from plugins.crypto_guard.reasoning.watch_conditions import is_structured_watch
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, raw_decision_json
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (lower_bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        actions = _persisted_actions(raw)
        if "create_opportunity_watch" not in actions:
            continue
        watch = raw.get("opportunity_watch")
        if is_structured_watch(watch):
            continue
        issues.append(_issue(
            OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": r["symbol"],
                "opportunity_watch_present": watch is not None,
            },
            "决策持久化了 create_opportunity_watch 动作但没有结构化 opportunity_watch"
            "（None/缺失/非结构化）：P0-2 接线按 fail-closed 语义永远无法物化该动作，"
            "漏斗在决策层即告断裂（承诺的动作无法兑现）。",
        ))
    return issues


def _check_opportunity_watch_untriggerable_condition(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3: active opportunity_watches whose conditions can NEVER
    trigger — a bare string, non-dict, or unknown-kind condition (fail-closed
    watcher semantics). Such a watch is permanently untriggerable (a silent
    dead funnel) and MUST be surfaced instead of silently waiting forever.
    NOT SQL-bound (the marker cutoff demotes pre-contract watches via the
    ``watch_created_at`` fallback path).
    """
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, symbol, direction, watch_condition_json,
               invalid_condition_json, status, created_at
        FROM opportunity_watches
        WHERE status = 'active'
        ORDER BY id DESC LIMIT 200
        """,
    ).fetchall()
    for r in rows:
        conditions = r["watch_condition_json"]
        # 08-02 R2 review Finding 1 (brand-new reviewer): mirror the watcher's
        # exact routing (opportunity_watcher.py:82) — ONLY a ROOT-DICT
        # watch_condition_json whose type is exactly "account_feedback_recheck"
        # routes to _check_account_feedback_recheck (which CAN trigger, so it
        # is never untriggerable; the manual account-feedback gate at
        # paper_broker.py:1214 persists exactly this root-dict shape). Every
        # other shape (root-list item, kind-only, uppercase) falls through to
        # _condition_hit, where the kind is not SUPPORTED and IS untriggerable.
        # The row-level skip keeps the by-design gate watch un-flagged while the
        # per-item check below flags any non-routed account_feedback_recheck
        # variant.
        if isinstance(conditions, dict) and conditions.get("type") == "account_feedback_recheck":
            continue
        if isinstance(conditions, dict):
            conditions = [conditions]
        elif not isinstance(conditions, list):
            conditions = []
        untriggerable_conditions: list[dict[str, Any]] = []
        for idx, cond in enumerate(conditions):
            if _condition_is_untriggerable(cond):
                if isinstance(cond, str):
                    type_name = cond
                elif not isinstance(cond, dict):
                    type_name = type(cond).__name__
                else:
                    type_name = str(cond.get("type") or cond.get("kind") or "unknown")
                untriggerable_conditions.append({"index": idx, "type": type_name})
        invalid_untriggerable = False
        if r["invalid_condition_json"] is not None:
            invalid_untriggerable = _condition_is_untriggerable(r["invalid_condition_json"])
        if not untriggerable_conditions and not invalid_untriggerable:
            continue
        # 08-02 R2 review P2-3 (fresh reviewer): the watcher's whole-watch dead
        # marker is ``all(...)`` — only when EVERY condition is untriggerable is
        # the watch truly unable to ever fire. For a MIXED watch (one live
        # structured condition + one dead text/unknown condition) the watch is
        # NOT dead; the dead sub-condition is a data-quality issue the watcher
        # keeps waiting past. Split the message accordingly instead of
        # overclaiming "永远无法触发".
        all_conditions_untriggerable = (
            len(conditions) > 0 and len(untriggerable_conditions) == len(conditions)
        )
        message = (
            "机会 watch 存在不可触发条件（裸字符串/非 dict/未知 kind）："
            "按 fail-closed 语义该 watch 永远无法触发，漏斗静默失效。"
            if all_conditions_untriggerable else
            "机会 watch 存在不可触发子条件（裸字符串/非 dict/未知 kind）："
            "该子条件永远无法触发（数据质量问题），其余结构化条件仍可继续触发。"
        )
        watch_created_at = None
        if r["created_at"] is not None:
            try:
                watch_created_at = int(r["created_at"].timestamp() * 1000)
            except (TypeError, ValueError, OSError):
                watch_created_at = None
        issues.append(_issue(
            OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, "error",
            {
                "watch_id": int(r["id"]),
                "symbol": r["symbol"],
                "direction": str(r["direction"] or ""),
                "watch_status": str(r["status"] or ""),
                "untriggerable_conditions": untriggerable_conditions,
                "invalid_condition_untriggerable": invalid_untriggerable,
                "watch_created_at": watch_created_at,
            },
            message,
        ))
    return issues


def _execution_funnel_starvation_lower_bound_ts(repo: CryptoGuardRepository) -> str:
    """08-02 P1-4 (Codex terminal review): starvation stats lower bound =
    ``max(now - 24h, marker.applied_at)``.

    Pre-marker raw S/A rows (produced by a codebase that never wrote
    ``llm_plan_verdict`` / ``risk_check`` / plan evidence) must NOT trigger
    starvation — the error cohort only exists for rows the contract can
    actually assess. A corrupt/unparseable marker FAILS CLOSED: the bound
    becomes ``now`` so nothing is provably post-marker (the marker-missing /
    corrupt state is surfaced separately, never fail-open to current).
    """
    now_dt = datetime.now(timezone.utc)
    cutoff_dt = now_dt - timedelta(seconds=_LLM_DIAGNOSTIC_WINDOW_MS // 1000)
    marker_ts = _get_execution_funnel_report_contract_marker_ts(repo)
    if marker_ts is None:
        return cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        marker_dt = datetime.fromisoformat(str(marker_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        # Corrupt marker -> fail-closed: nothing is provably post-marker.
        return now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    lower_dt = max(cutoff_dt, marker_dt)
    return lower_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Shared SQL for the produced S/A cohort (P1-4): post-marker rows whose LLM
# synthesis CONFIRMED a plan with real plan evidence, whose risk gate PASSED,
# and whose EFFECTIVE grade is S/A. ``raw_signal_grade`` is deliberately NOT
# used: an HTF-grade degradation (raw S/A -> effective B/C) or a
# risk-rejected / LLM-unconfirmed outcome is a legitimate non-executable, not
# a starvation contradiction.
_EXECUTION_FUNNEL_PRODUCED_COHORT_SQL = """
          AND COALESCE(NULLIF(raw_decision_json->>'effective_signal_grade', ''),
                       signal_grade::text) IN ('S', 'A')
          AND raw_decision_json->>'llm_plan_verdict' = 'confirmed'
          AND (
                (jsonb_typeof(raw_decision_json->'llm_synthesis_trade_plan') = 'object'
                 AND raw_decision_json->'llm_synthesis_trade_plan' != '{}'::jsonb)
               OR
                (raw_decision_json->>'has_trade_plan' = 'true'
                 AND jsonb_typeof(raw_decision_json->'trade_plan') = 'object'
                 AND raw_decision_json->'trade_plan' != '{}'::jsonb)
              )
          AND raw_decision_json->'risk_check'->>'ok' = 'true'
"""


def _check_execution_funnel_starvation(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """08-02 P1-3 / P1-4: aggregate live check — PRODUCED S/A decisions in the
    post-marker 24h window with ZERO final-executable plans in the same window.

    The produced cohort (``strict_signal_count_24h``) is the REAL execution-
    funnel contradiction base (P1-4): rows with ``llm_plan_verdict=confirmed``,
    plan evidence (immutable ``llm_synthesis_trade_plan`` OR top-level
    ``has_trade_plan`` + non-empty ``trade_plan``), ``risk_check.ok=true`` and
    an EFFECTIVE grade S/A. A funnel that produces S/A decisions but never an
    executable plan is starved (gates collapse every candidate). Fires at most
    ONE error. Marker-gated (skips when the contract marker is absent) and
    SQL-bound by ``created_at >= max(now-24h, marker.applied_at)`` — pre-marker
    rows and legitimate non-executables (LLM unconfirmed / risk rejected / HTF
    grade degradation) never trigger.
    """
    issues: list[dict[str, Any]] = []
    if _get_execution_funnel_report_contract_marker_ts(repo) is None:
        return issues
    lower_bound = _execution_funnel_starvation_lower_bound_ts(repo)
    strict_row = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM ga_decisions"
        " WHERE raw_decision_json IS NOT NULL"
        + _EXECUTION_FUNNEL_PRODUCED_COHORT_SQL +
        "  AND created_at >= %s::timestamptz",
        (lower_bound,),
    ).fetchone()
    strict_count = int(strict_row["n"] or 0) if strict_row else 0
    executable_row = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM ga_decisions"
        " WHERE raw_decision_json IS NOT NULL"
        + _EXECUTION_FUNNEL_PRODUCED_COHORT_SQL +
        """
          AND raw_decision_json->>'plan_execution_state' = 'confirmed'
          AND raw_decision_json->>'plan_status' = 'executable'
          AND raw_decision_json->>'has_trade_plan' = 'true'
          AND jsonb_typeof(raw_decision_json->'trade_plan') = 'object'
          AND raw_decision_json->'trade_plan' != '{}'::jsonb
          AND created_at >= %s::timestamptz
        """,
        (lower_bound,),
    ).fetchone()
    executable_count = int(executable_row["n"] or 0) if executable_row else 0
    if strict_count == 0 or executable_count > 0:
        return issues
    evidence_rows = repo.conn.execute(
        "SELECT id, symbol FROM ga_decisions"
        " WHERE raw_decision_json IS NOT NULL"
        + _EXECUTION_FUNNEL_PRODUCED_COHORT_SQL +
        "  AND created_at >= %s::timestamptz"
        " ORDER BY id DESC LIMIT 5",
        (lower_bound,),
    ).fetchall()
    evidence = [{"decision_id": int(x["id"]), "symbol": x["symbol"]} for x in evidence_rows]
    issues.append(_issue(
        EXECUTION_FUNNEL_STARVATION, "error",
        {
            "strict_signal_count_24h": strict_count,
            "final_executable_count_24h": executable_count,
            "window_ms": _LLM_DIAGNOSTIC_WINDOW_MS,
            "window_hours": 24,
            "evidence": evidence,
        },
        "执行漏斗饥饿：24h 内 strict S/A 决策 " + str(strict_count) +
        " 条但 final-executable S/A 计划 0 条：每个 S/A 候选都在门控中被清空，"
        "没有可达执行路径。",
    ))
    return issues


def _check_effective_grade_exceeds_htf_cap(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag ga_decisions whose *effective* grade exceeds the HTF-alignment cap.

    07-22 Phase-2 contract correction (production evidence: Phase-2 verifier
    failed on 5× ``raw_grade_exceeds_htf_cap`` while analysis batches were
    10/10 complete):

    - ``raw_signal_grade`` is the pre-gate audit value. It MAY exceed the HTF
      cap; that is intentional and is NOT an error.
    - The cap must constrain the effective / canonical grade only:
      ``effective_signal_grade`` (preferred) or the column ``signal_grade``.
    - Issue code: ``effective_grade_exceeds_htf_cap``.

    Caps (recomputed from ``raw_decision_json.timeframe_context``):
    - Cap 1: 1D and 4H both opposite to candidate → max B.
    - Cap 2: 4H range/transition/mixed/unknown → max B.
    - Cap 3: 1H and 15M both not aligned with candidate → max B.
    - Cap 4: only 5M supports, 4H and 1H don't → max C.

    GRADE_ORDER is ``S > A > B > C > D``.
    """
    issues: list[dict[str, Any]] = []
    grade_rank = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, raw_decision_json, analysis_time
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND analysis_time >= %s
        ORDER BY id DESC LIMIT 200
        """,
        (cutoff_ms,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        # Pre-gate audit value — recorded for detail only, never the fail trigger.
        raw_grade = str(raw.get("raw_signal_grade") or "").upper()
        # Effective / canonical grade is what the cap must constrain.
        effective_grade = str(
            raw.get("effective_signal_grade")
            or r["signal_grade"]
            or "D"
        ).upper()
        if effective_grade not in grade_rank:
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
        # NOT against candidate_side.lower() ("long"/"short").
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

        if grade_rank[effective_grade] > grade_rank[max_allowed]:
            issues.append(_issue(
                EFFECTIVE_GRADE_EXCEEDS_HTF_CAP, "error",
                {
                    "decision_id": int(r["id"]),
                    "symbol": r["symbol"],
                    "analysis_time": int(r["analysis_time"] or 0),
                    "raw_signal_grade": raw_grade or None,
                    "effective_signal_grade": effective_grade,
                    "canonical_signal_grade": str(r["signal_grade"] or "").upper() or None,
                    "max_allowed_grade": max_allowed,
                    "applied_cap_reasons": applied_reasons or ["none"],
                    "timeframe_context_bias": {
                        "1d": bias_1d, "4h": bias_4h, "1h": bias_1h,
                        "15m": bias_15m, "5m": bias_5m,
                    },
                    "market_bias": market_bias,
                },
                f"effective_signal_grade={effective_grade} 超过 HTF 对齐上限 {max_allowed}"
                f"（{', '.join(applied_reasons) or 'none'}）；"
                f"raw_signal_grade={raw_grade or '-'} 为门禁前审计值，允许 raw>cap。"
                "检查 normalize_market_semantics / controller 的 cap 是否被绕过。",
            ))
    return issues


# Backward-compatible alias used by older tests / fault-injection imports.
def _check_raw_grade_exceeds_htf_cap(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Deprecated alias for ``_check_effective_grade_exceeds_htf_cap``."""
    return _check_effective_grade_exceeds_htf_cap(repo)


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
        WHERE status = 'success' AND analysis_time >= %s
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
                    "SELECT COUNT(*) AS c FROM batch_symbol_status WHERE batch_id=%s AND status='completed'",
                    (bid,),
                ).fetchone()["c"]
            except Exception:
                raise
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
              AND created_at >= NOW() - INTERVAL '1 hour'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    except Exception:
        raise
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
            # time columns come back as datetime objects under PG; str() normalizes
            # both datetime and ISO-string values before fromisoformat.
            t_running = _dt.fromisoformat(str(running_started_at).replace("Z", "+00:00"))
            t_alert = _dt.fromisoformat(str(alert_created_at).replace("Z", "+00:00"))
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


def _check_batch_stuck_running_all_terminal(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 R6-E (P1-4): flag ``analysis_batches.status='running'`` batches
    where every enabled symbol has a terminal row (``completed`` or
    ``failed``) in ``batch_symbol_status``. Such a batch is a terminalization
    leak: all work is done but ``finish_analysis_batch`` never flipped the
    batch to a terminal status (``success`` / ``failed``).

    Per plan §4 P1-4 / AC5: "A batch cannot stay running after all enabled
    symbols are terminal." At finish, completed/failed arrays must be
    materialized in the same transaction as the terminal status. A batch that
    slips through that write-link is a current error, not a warning — the
    report's progress header would freeze at <100% and the partial-running
    render guard would fire next cycle.

    Detection mirrors ``_check_hourly_report_incomplete_batch``: read the
    enabled set from ``enabled_symbols_json``, then join
    ``batch_symbol_status`` for live completed/failed/pending counts. The
    leak holds iff the enabled set is non-empty AND
    (completed ∪ failed) ⊇ enabled AND no pending rows exist for the batch.

    Window: only running batches with ``started_at`` within the latest 24h are
    evaluated, matching the Phase-I LLM diagnostic window. Older stuck-running
    rows are historical audit (recovered/restarted between cycles) and would
    be noise; the marker cutoff does not apply (this is a runtime check with
    no persisted-marker contract of its own, consistent with the other Phase-I
    runtime diagnostics). Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - _LLM_DIAGNOSTIC_WINDOW_MS
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status,
               enabled_symbols_json, started_at
        FROM analysis_batches
        WHERE status = 'running'
          AND analysis_time >= %s
        ORDER BY started_at DESC LIMIT 20
        """,
        (cutoff_ms,),
    ).fetchall()
    for row in rows:
        bid = row["batch_id"] if row["batch_id"] else None
        if not bid:
            continue
        enabled = _json_list(row["enabled_symbols_json"])
        if not enabled:
            continue
        completed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='completed'",
                (bid,),
            ).fetchall()
        ]
        failed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='failed'",
                (bid,),
            ).fetchall()
        ]
        pending_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='pending'",
                (bid,),
            ).fetchall()
        ]
        terminal = set(completed_syms) | set(failed_syms)
        # All enabled symbols must be terminal AND no pending work remains.
        # A symbol enabled but absent from batch_symbol_status entirely is
        # NOT terminal (it is missing/pending per P1-4 #1) -> not a leak.
        if pending_syms:
            continue
        if not set(enabled).issubset(terminal):
            continue  # Genuine leak check: every enabled symbol must be terminal.
        # Genuine leak: every enabled symbol terminal but batch still running.
        issues.append(_issue(
            BATCH_STUCK_RUNNING_ALL_TERMINAL, "error",
            {
                "batch_id": bid,
                "primary_interval": row["primary_interval"],
                "analysis_time": int(row["analysis_time"] or 0),
                "status": "running",
                "enabled_count": len(enabled),
                "completed_count": len(completed_syms),
                "failed_count": len(failed_syms),
                "enabled_symbols": enabled,
                "completed_symbols": completed_syms,
                "failed_symbols": failed_syms,
            },
            "批次全部 enabled 品种已终态（completed+failed==enabled）但 "
            "status 仍为 running：finish_analysis_batch 未在同一事务终态化，"
            "属 terminalization leak；必须物化 completed/failed 列并置 status "
            "为 success/failed，批次不得在全部品种终态后继续 running。",
        ))
    return issues


# ── 07-10 S7 (P1 #7): fair-scheduling + context-continuity contract checks ──
# Three independent checks verifying the S1-S6 production-chain postconditions
# survive in persisted decisions + batch_symbol_status + agent_jobs. Each uses
# the LLM_FAIR_SCHEDULING_CONTRACT_MARKER_KEY cutoff (applied by
# _apply_llm_fair_scheduling_marker_cutoff) so pre-marker rows are demoted to
# legacy_info and do not count against the current contract. The marker-missing
# check runs unconditionally (always error when absent) so a missing contract
# is never silently healthy.


def _check_llm_fair_scheduling_contract_markers_missing(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """07-10 S7 (P1 #7): flag a missing fair-scheduling + context-continuity
    contract marker.

    Mirrors ``_check_plan_lifecycle_contract_markers_missing`` /
    ``_check_semantic_contract_markers_missing``. The marker
    ``llm_fair_scheduling_context_contract_v1`` must exist in
    ``_migration_state``. If absent, emit an ``error`` issue so callers can
    detect the missing contract rather than receiving a silently-healthy report.
    When the marker is absent, the two data-dependent checks
    (``_check_fair_path_continuity_real_injection`` and
    ``_check_per_job_failure_consistency``) still run, but their findings are
    demoted to ``legacy_info`` by ``_apply_llm_fair_scheduling_marker_cutoff``
    (a no-op when the marker is absent, so the findings keep their original
    severity only if the marker exists — absent marker => no demotion needed
    because the data checks emit error only when a real S1-S6 regression is
    observed; the marker-missing error here is the primary signal).
    """
    issues: list[dict[str, Any]] = []
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
            (LLM_FAIR_SCHEDULING_CONTRACT_MARKER_KEY,),
        ).fetchone()
    except Exception:
        raise
    if not row or not row["applied_at"]:
        issues.append(_issue(
            LLM_FAIR_SCHEDULING_CONTRACT_MARKER_MISSING, "error",
            {
                "marker_key": LLM_FAIR_SCHEDULING_CONTRACT_MARKER_KEY,
                "contract": "llm-fair-scheduling-context-continuity",
                "issue": "marker_absent",
            },
            "fair-scheduling + context-continuity contract marker 未部署。"
            "运行 initialize_database() 部署 marker；marker 缺失时 S1-S6 "
            "契约诊断被降级为 legacy_info，可能导致假绿（continuity 真实注入 / "
            "per-job 失败一致性 / 归属完整性无法作为当前契约评估）。",
        ))
    return issues


def _check_llm_provider_timeout_envelope_contract_markers_missing(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """07-22 Codex P1-1 / P2: flag a missing provider-timeout envelope marker.

    The marker ``llm_provider_timeout_envelope_contract_v2`` must exist in
    ``_migration_state``. If absent, emit an ``error`` (fail-closed) so callers
    never receive a silently-healthy report while the post-prompt admission
    contract is undeployed. Pre-marker historical rows (e.g. production d49)
    stay in ``ga_decisions`` for audit; once the marker is present the SQL
    lower bound excludes them from current issues entirely (exclude-only —
    no legacy_info issue is generated). Rows are NOT deleted.
    """
    issues: list[dict[str, Any]] = []
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
            (LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_KEY,),
        ).fetchone()
    except Exception:
        raise
    if not row or not row["applied_at"]:
        issues.append(_issue(
            LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_MISSING, "error",
            {
                "marker_key": LLM_PROVIDER_TIMEOUT_ENVELOPE_CONTRACT_MARKER_KEY,
                "contract": "llm-provider-timeout-envelope",
                "issue": "marker_absent",
            },
            "provider-timeout envelope contract marker 未部署。"
            "运行 initialize_database() 部署 llm_provider_timeout_envelope_contract_v2；"
            "marker 缺失时 llm_timeout_config_out_of_range 无法区分历史 d49 与当前"
            "契约违约（fail-closed）。",
        ))
    return issues


def _check_fair_path_continuity_real_injection(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """07-10 S7 (P1 #7, validates S1 / P0 #1): flag ga_decisions where the
    CANONICAL continuity classification expects ``ok`` but the persisted
    ``analysis_continuity.continuity_status`` is NOT ``ok``.

    The S1 (P0 #1) fix pre-injects the real strict cross-batch previous
    analysis into each fair-path snapshot BEFORE the LLM prompt is built, so
    ``_compact_snapshot`` reads ``continuity_status="ok"`` (with a real
    ``previous.analysis_time`` and non-empty ``delta.trigger_progress``)
    rather than the lazy ``previous_row=None`` -> ``continuity_status="missing"``
    that the production bug produced. The controller's post-decision
    ``build_analysis_continuity`` re-derives the same status from the strict
    row, so the persisted block is the audit-grade witness.

    Phase B P1-2 (07-22): do NOT flag merely because a prior row exists. A
    prior row can legitimately yield ``stale`` when
    ``age_ms > CONTINUITY_MAX_AGE_MS`` (24h) — production batch6 after a
    30.75h gap recorded correct ``stale`` for all 10 symbols and was
    over-flagged. Reuse the canonical ``_continuity_status`` classifier; only
    flag when expected=``ok`` AND persisted ≠ ``ok``. Severity: ``error``
    post-marker, ``legacy_info`` pre-marker (applied by
    ``_apply_llm_fair_scheduling_marker_cutoff``).
    """
    from plugins.crypto_guard.reasoning.decision_context import _continuity_status

    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, batch_id, analysis_time, signal_grade, decision,
               raw_decision_json, created_at
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        continuity = raw.get("analysis_continuity")
        if not isinstance(continuity, dict):
            # A missing continuity block is already flagged by
            # _check_missing_analysis_continuity (Phase H). Do not double-flag.
            continue
        status = str(continuity.get("continuity_status") or "").lower()
        if status == "ok":
            continue
        # Re-derive the CANONICAL expected status the same way the controller
        # does. Only flag when expected is ``ok`` (injection should have
        # produced a valid prior) but the persisted status is not.
        symbol = str(r["symbol"] or "")
        batch_id = str(r["batch_id"] or "") or None
        try:
            at_utc = int(r["analysis_time"] or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or at_utc <= 0:
            continue
        try:
            prior = repo.latest_analysis_state_for_continuity(
                symbol,
                analysis_time_utc=at_utc,
                exclude_batch_id=batch_id,
            )
        except Exception:
            raise
        expected = _continuity_status(
            previous_row=prior if isinstance(prior, dict) else None,
            current_symbol=symbol,
            current_analysis_time_utc=at_utc,
            current_batch_id=batch_id,
        )
        if expected != "ok":
            # Legitimate non-ok (missing / stale / future / same_batch /
            # cross_symbol). A prior may exist but still be too old for
            # continuity — not an S1 injection regression.
            continue
        issues.append(_issue(
            FAIR_PATH_CONTINUITY_REAL_INJECTION, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": symbol,
                "batch_id": batch_id or "",
                "grade": r["signal_grade"],
                "decision": r["decision"],
                "analysis_time": at_utc,
                "observed_continuity_status": status,
                "expected_continuity_status": expected,
                "prior_analysis_time": int(
                    (prior or {}).get("analysis_time") or 0
                ) if isinstance(prior, dict) else 0,
            },
            "fair 路径 continuity 真实注入回归：canonical 期望 continuity_status=ok "
            "但持久化 status != ok。S1 (P0 #1) 预注入被绕过 -> "
            "LLM prompt 在无真实前序上下文下构建（生产饥饿/误判根因）。检查 "
            "process_fair_batch 的预注入循环是否在 run_fair_batch 之前执行。"
            "合法 stale（age > CONTINUITY_MAX_AGE_MS）不得报此码。",
        ))
    return issues


def _check_per_job_failure_consistency(
    repo: CryptoGuardRepository,
) -> list[dict[str, Any]]:
    """07-10 S7 (P1 #7, validates S6 / P1 #6): flag agent_jobs rows whose
    ``status`` disagrees with the per-symbol outcome recorded in
    ``batch_symbol_status`` for the same (batch_id, symbol).

    The S6 (P1 #6) fix calls ``finish_job(job_id, result=,
    error_message=str(exc) if failed else None)`` per-symbol inside
    ``process_fair_batch``, so a symbol whose ``analyze_symbol`` raised gets
    ``agent_jobs.status='failed'`` + a non-empty ``error_message``, while the
    matching ``batch_symbol_status`` row is marked ``failed``. The pre-S6
    defect ran a uniform ``finish_job(result=)`` loop in ``run_once`` that
    marked EVERY job in the batch ``success`` regardless of per-symbol failure
    -> failed symbols were hidden from the ops dashboard.

    Detection: join ``batch_symbol_status`` (status IN ('failed','completed'))
    to ``agent_jobs`` by (batch_id, symbol). The agent_jobs payload stores the
    snapshot under ``$.snapshot.symbol`` (cron_scheduler builds the payload);
    the batch_id is stored under ``$.batch_id``. A row is flagged when:
      - ``batch_symbol_status.status='failed'`` but the matching
        ``agent_jobs.status`` is NOT ``failed`` (the S6 mislabel defect), OR
      - ``batch_symbol_status.status='completed'`` but the matching
        ``agent_jobs.status='failed'`` (a job marked failed that the batch
        considers completed — also inconsistent).
    Severity: ``error`` post-marker, ``legacy_info`` pre-marker (applied by
    ``_apply_llm_fair_scheduling_marker_cutoff`` via the
    ``details.analysis_time`` fallback).
    """
    issues: list[dict[str, Any]] = []
    # Pull the latest batch_symbol_status rows (limit to recent batches to
    # bound work). analysis_time is not on batch_symbol_status, so the cutoff
    # helper falls back to details.analysis_time derived from the agent_jobs
    # payload's snapshot.analysis_time_utc when available.
    bss_rows = repo.conn.execute(
        """
        SELECT batch_id, symbol, status, updated_at
        FROM batch_symbol_status
        WHERE status IN ('failed', 'completed')
        ORDER BY updated_at DESC LIMIT 200
        """
    ).fetchall()
    for bss in bss_rows:
        batch_id = str(bss["batch_id"] or "")
        symbol = str(bss["symbol"] or "")
        bss_status = str(bss["status"] or "").lower()
        if not batch_id or not symbol:
            continue
        # Find the matching agent_jobs row by batch_id (payload) + symbol
        # (payload snapshot.symbol). There is exactly one per (batch, symbol)
        # because cron_scheduler dedupes by session_id and enqueue_job_once
        # guards re-enqueue.
        try:
            job_row = repo.conn.execute(
                """
                SELECT id, status, error_message, payload_json, finished_at
                FROM agent_jobs
                WHERE job_type='scheduled_market_analysis'
                  AND payload_json ->> 'batch_id' = %s
                  AND (
                    payload_json ->> 'symbol' = %s
                    OR payload_json #>> '{snapshot,symbol}' = %s
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (batch_id, symbol, symbol),
            ).fetchone()
        except Exception:
            raise
        if not job_row:
            # No matching agent_jobs row — a separate concern (orphaned
            # batch_symbol_status), not the S6 mislabel defect. Skip; the
            # state_consistency batch_claim_ownership check covers ownership.
            continue
        job_status = str(job_row["status"] or "").lower()
        # Derive analysis_time for the cutoff helper from the payload snapshot.
        at_ms = 0
        try:
            payload = _safe_json(job_row["payload_json"]) or {}
            snap = payload.get("snapshot") or {}
            at_ms = int(snap.get("analysis_time_utc") or 0)
        except Exception:
            at_ms = 0
        mismatch = None
        if bss_status == "failed" and job_status != "failed":
            mismatch = "batch_failed_job_not_failed"
        elif bss_status == "completed" and job_status == "failed":
            mismatch = "batch_completed_job_failed"
        if mismatch is None:
            continue
        issues.append(_issue(
            PER_JOB_FAILURE_CONSISTENCY, "error",
            {
                "batch_id": batch_id,
                "symbol": symbol,
                "batch_symbol_status": bss_status,
                "agent_job_id": int(job_row["id"]),
                "agent_job_status": job_status,
                "agent_job_error_message": str(job_row["error_message"] or "") or None,
                "mismatch": mismatch,
                "analysis_time": at_ms,
            },
            "per-job 失败一致性回归：batch_symbol_status 与 agent_jobs.status 不一致。"
            "S6 (P1 #6) 的 per-symbol finish_job(error_message=...) 被绕过 -> "
            "失败品种被误标 success（或反之）。检查 process_fair_batch 是否对每个 "
            "symbol 调用 finish_job(result=, error_message=) 而非 run_once 统一收尾。",
        ))
    return issues


# ---------------------------------------------------------------------------
# 07-10 P1-1 (design §10): eight formal Phase F diagnostics. Each verifies a
# post-marker fair-scheduling + context-continuity contract. Marker-cutoff-
# scoped (``_apply_llm_fair_scheduling_marker_cutoff`` demotes pre-marker
# findings to ``legacy_info``). These checks read the persisted ga_decisions
# §8 envelope + analysis_batches.summary_json.llm_health directly so they are
# independent of the production aggregation path.
# ---------------------------------------------------------------------------

# Config-derived bounds (mirror PerSymbolDeadline validation in llm_breaker.py
# and config/loader.py). The per-symbol deadline caps the whole attempt chain;
# a persisted provider_timeout_ms must be 0 (exhausted skip) or in (0, cap].
_LLM_PER_SYMBOL_TIMEOUT_MAX_MS = 1200 * 1000  # per_symbol_timeout_seconds <= 1200


def _recent_success_batches(repo: CryptoGuardRepository, limit: int = 10) -> list[Any]:
    """Latest ``success`` batches (the batches the hourly report renders
    against), most-recent first. Bounded by ``limit``."""
    with repo.conn.transaction():
        return repo.conn.execute(
            """
            SELECT batch_id, primary_interval, analysis_time, status,
                   started_at, enabled_symbols_json, completed_symbols_json,
                   failed_symbols_json, summary_json
            FROM analysis_batches
            WHERE status = 'success'
            ORDER BY started_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()


def _check_llm_first_attempt_coverage_low(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a ``success`` batch where eligible enabled
    symbols were NOT attempted (no physical provider call) WITHOUT being
    accounted for by ANY allowed bucket.

    The starvation root cause (P0 #1/#2) meant later alphabetically-sorted
    symbols never received a provider call under the shared 90s budget. The
    fair scheduler (Phase C) + per-symbol deadline (Phase B) guarantee every
    enabled symbol receives Attempt-1 unless an explicit skip accounts for it.
    Production's ``_aggregate_batch_llm_outcomes`` buckets every no-call row
    into one of: ``policy_skip`` (no-call, non-failed, no/gate terminal
    reason), ``breaker_skip`` (``breaker_skipped``), ``budget_skip``
    (``symbol_timeout`` / ``batch_deadline_skipped``), ``worker_failed``
    (enabled symbol with NO decision row). So a correct batch satisfies
    ``attempted + policy_skip + breaker_skip + budget_skip + worker_failed ==
    expected``. The coverage defect is the residual: an enabled symbol that is
    NONE of these - silently dropped with a row that has a real provider
    ``failure`` (``llm_status="failed"``) on a ``success`` batch, OR an
    enabled symbol whose row is missing entirely but was NOT counted as
    worker_failed.

    Detection: re-aggregate the latest ``success`` batch's decisions and
    compute the residual ``expected - (attempted + policy_skip + breaker_skip
    + budget_skip + worker_failed)``. These five buckets are DISJOINT (a row
    with ``pcc>=1`` is in ``attempted`` regardless of status; the no-call
    buckets are mutually exclusive by terminal reason). A row with
    ``pcc==0`` AND ``llm_status="failed"`` falls into NONE of them - it is a
    bare failure with no call and no budget/breaker reason, the silent-drop
    signature. A strictly POSITIVE residual means a symbol is unaccounted-for
    (the coverage gap). Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    for r in _recent_success_batches(repo, limit=5):
        batch_id = str(r["batch_id"] or "")
        if not batch_id:
            continue
        agg = _reaggregate_batch_llm_outcomes(repo, batch_id)
        if not agg:
            continue
        expected = int(agg.get("expected_symbols") or 0)
        attempted = int(agg.get("llm_symbols_attempted") or 0)
        policy_skip = int(agg.get("llm_policy_skip_count") or 0)
        breaker_skip = int(agg.get("llm_breaker_skip_count") or 0)
        budget_skip = int(agg.get("llm_budget_skip_count") or 0)
        worker_failed = int(agg.get("llm_symbols_worker_failed") or 0)
        if expected <= 0 or attempted >= expected:
            continue
        # Disjoint bucket accounting (mirrors _aggregate_batch_llm_outcomes):
        #   attempted   = rows with pcc>=1 (a call happened; any status)
        #   policy_skip = rows with pcc==0, status!=failed, no/gate terminal
        #   breaker_skip= rows with pcc==0, terminal=breaker_skipped
        #   budget_skip = rows with pcc==0, terminal in {symbol_timeout,
        #                 batch_deadline_skipped}
        #   worker_failed = enabled symbols with NO decision row
        # A row with pcc==0 AND status="failed" (a failure recorded with no
        # call and no budget/breaker reason) falls into NONE of these buckets
        # - it is the unexplained residual (a symbol silently dropped with a
        # bare failure, the coverage-gap defect). ``failed`` is NOT added
        # separately because a pcc>=1 failure is already in ``attempted``.
        accounted = (
            attempted + policy_skip + breaker_skip + budget_skip
            + worker_failed
        )
        unexplained = expected - accounted
        if unexplained <= 0:
            # Every non-attempted symbol falls into an allowed / recorded
            # bucket. Coverage is low but fully explained (e.g. a legitimately
            # degraded batch with all skips recorded) - NOT the silent-drop
            # defect.
            continue
        issues.append(_issue(
            LLM_FIRST_ATTEMPT_COVERAGE_LOW, "error",
            {
                "batch_id": batch_id,
                "analysis_time": int(r["analysis_time"] or 0),
                "expected_symbols": expected,
                "attempted_symbols": attempted,
                "policy_skip": policy_skip,
                "breaker_skip": breaker_skip,
                "budget_skip": budget_skip,
                "worker_failed": worker_failed,
                "unexplained_unattempted": unexplained,
            },
            "首轮覆盖不足：success 批次存在 enabled 品种未被尝试且无法被任何允许桶"
            "(policy/breaker/budget skip / worker_failed) 解释（典型：pcc=0 且 "
            "status=failed 的裸失败行）。这是公平调度饥饿的回归信号（调度器静默"
            "丢弃品种）。检查 run_fair_batch 的 attemptable_symbols 是否覆盖全部 "
            "enabled symbol，以及每个被跳过品种是否记录了显式 terminal_reason。",
        ))
    return issues


def _check_llm_symbol_starvation(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag three CONSECUTIVE eligible batches with
    ZERO provider calls (``llm_physical_provider_calls == 0``) for the batch
    overall.

    The original production defect: the shared 90s budget was consumed by the
    first few symbols' retry loops, leaving zero physical calls for the rest;
    across consecutive batches the same symbols were starved. Three
    consecutive eligible batches (each with ``expected_symbols > 0``) all
    making zero provider calls means the LLM layer is structurally unreachable
    for the whole batch - a total starvation, not a one-off breaker trip.
    Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    rows = _recent_success_batches(repo, limit=10)
    # Walk most-recent-first; collect runs of >=3 consecutive eligible batches
    # with zero physical provider calls.
    run: list[Any] = []
    for r in rows:
        batch_id = str(r["batch_id"] or "")
        if not batch_id:
            continue
        agg = _reaggregate_batch_llm_outcomes(repo, batch_id)
        if not agg or int(agg.get("expected_symbols") or 0) <= 0:
            run = []
            continue
        physical = int(agg.get("llm_physical_provider_calls") or 0)
        if physical == 0:
            run.append((r, agg))
            if len(run) >= 3:
                # Report once for the run, anchored at the most-recent batch.
                newest_r, newest_agg = run[-1]
                issues.append(_issue(
                    LLM_SYMBOL_STARVATION, "error",
                    {
                        "batch_id": str(newest_r["batch_id"] or ""),
                        "analysis_time": int(newest_r["analysis_time"] or 0),
                        "consecutive_starved_batches": len(run),
                        "batch_ids": [str(rr["batch_id"] or "") for rr, _ in run],
                        "expected_symbols": int(newest_agg.get("expected_symbols") or 0),
                        "physical_provider_calls": 0,
                    },
                    "品种饥饿：连续 >=3 个合格批次物理 provider 调用数为 0。LLM 层对整个"
                    "批次结构性不可达（共享预算耗尽 / 调度器未派发 / breaker 常开）。"
                    "检查公平调度器是否真正派发 Attempt-1，以及 ESTIMATED_CALL_MS 门控是否"
                    "仍残留。",
                ))
                break  # report the single most-recent run once
        else:
            run = []
    return issues


def _check_llm_report_count_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a ``success`` batch whose persisted
    ``summary_json.llm_health`` counters DISAGREE with a fresh re-aggregation
    from the underlying ``ga_decisions`` rows.

    The original report defect: the hourly report rendered "完成 10/10" while
    the persisted decisions only covered 3 symbols - the rendered counters
    were computed from a stale/optimistic summary, not from the real decisions.
    This check independently re-aggregates the batch's decisions (the source of
    truth, mirroring ``_aggregate_batch_llm_outcomes``) and compares key
    counters against what ``summary_json.llm_health`` claims. A mismatch means
    the persisted summary lied (or went stale after a late decision write) ->
    the report rendered false-healthy numbers. Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    for r in _recent_success_batches(repo, limit=5):
        batch_id = str(r["batch_id"] or "")
        if not batch_id:
            continue
        agg = _reaggregate_batch_llm_outcomes(repo, batch_id)
        if not agg:
            continue
        summary = _safe_json(r["summary_json"]) or {}
        if not isinstance(summary, dict):
            continue
        health = summary.get("llm_health") or {}
        if not isinstance(health, dict) or not health:
            # No persisted llm_health -> covered by other checks; not a
            # mismatch (nothing to disagree with).
            continue
        # Compare the counters the report renderer reads (design §9). Persisted
        # keys are the Phase E aggregate names; the re-aggregation uses the
        # same names. Any divergence on a structural counter is the defect.
        compared = [
            ("expected_symbols", "expected_symbols"),
            ("llm_symbols_attempted", "llm_symbols_attempted"),
            ("llm_physical_provider_calls", "llm_physical_provider_calls"),
            ("llm_symbols_success", "llm_symbols_success"),
            ("llm_symbols_failed", "llm_symbols_failed"),
        ]
        mismatches: list[dict[str, Any]] = []
        for persisted_key, agg_key in compared:
            persisted_val = health.get(persisted_key)
            if persisted_val is None:
                continue
            agg_val = agg.get(agg_key)
            if int(persisted_val) != int(agg_val or 0):
                mismatches.append({
                    "field": persisted_key,
                    "persisted": int(persisted_val),
                    "reaggregated": int(agg_val or 0),
                })
        if not mismatches:
            continue
        issues.append(_issue(
            LLM_REPORT_COUNT_MISMATCH, "error",
            {
                "batch_id": batch_id,
                "analysis_time": int(r["analysis_time"] or 0),
                "mismatches": mismatches,
            },
            "报告计数不一致：summary_json.llm_health 持久化计数与从 ga_decisions "
            "重新聚合的计数不符。报告渲染了虚假健康数字（如完成 10/10 实为 3/10）。"
            "检查 finish_analysis_batch 是否在所有 per-symbol 决策持久化后、用最新聚合"
            "写入 summary_json.llm_health。",
        ))
    return issues


def _check_llm_success_missing_attempt_metadata(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a decision whose ``llm_status == "ok"``
    but whose §8 attempt-metadata envelope is missing/incomplete.

    A successful LLM decision MUST carry the audit envelope (the controller
    surfaces it at the top level of raw_decision_json via
    ``controller_decision_from_legacy``). ``llm_status == "ok"`` with
    ``llm_provider_call_count`` absent/None (or ``llm_latency_ms`` absent on a
    real provider call) means a success was recorded WITHOUT the metadata that
    proves a real provider call happened - the silent-success path that masked
    the original starvation (the report counted symbols as "covered" with no
    evidence of a call). Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    # R6-E P1-3 #4 (second half): apply a marker/time bound BEFORE LIMIT so
    # historical rows are not fetched and counted in the summary (the §3.6
    # ``llm_success_missing_attempt_metadata=32`` cumulative-count noise).
    bound = _llm_attempt_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, batch_id, analysis_time, signal_grade, decision,
               raw_decision_json
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        if str(raw.get("llm_status") or "").lower() != "ok":
            continue
        pcc = raw.get("llm_provider_call_count")
        # A success must carry a non-null provider_call_count (>=0 integer).
        # None / missing on an "ok" status is the audit-gap defect.
        missing: list[str] = []
        if pcc is None:
            missing.append("llm_provider_call_count")
        # When a physical call was made (pcc >= 1), latency + prompt bytes +
        # continuity_included MUST be present (they are captured by
        # _call_ga_llm's thread-local). Their absence on a real call means the
        # success bypassed the capture path.
        if isinstance(pcc, int) and pcc >= 1:
            for fld in ("llm_latency_ms", "llm_prompt_bytes", "llm_continuity_included"):
                if raw.get(fld) is None:
                    missing.append(fld)
        if not missing:
            continue
        issues.append(_issue(
            LLM_SUCCESS_MISSING_ATTEMPT_METADATA, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": str(r["symbol"] or ""),
                "batch_id": str(r["batch_id"] or "") or "",
                "grade": r["signal_grade"],
                "decision": r["decision"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_status": "ok",
                "missing_fields": missing,
                "llm_provider_call_count": pcc,
            },
            "成功决策缺少尝试元数据：llm_status=ok 但 §8 信封字段缺失。原始饥饿"
            "缺陷正是用无证据的'成功'掩盖了未发生的 provider call。检查 controller "
            "是否在 ok 路径透出 attempt_meta，以及 _call_ga_llm 的 thread-local 捕获"
            "是否生效。",
        ))
    return issues


def _check_llm_continuity_not_included(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a decision whose prompt was built WITHOUT
    the analysis_continuity block, i.e. ``llm_continuity_included`` is
    False/absent on a decision that DID make a provider call.

    The S1 (P0 #1) fix pre-injects the real strict cross-batch previous
    analysis into each fair-path snapshot so the prompt carries continuity.
    ``llm_continuity_included`` is the §8 envelope flag the LLM call sets when
    the prompt contained the continuity block. A real provider call
    (``llm_provider_call_count >= 1``) with ``llm_continuity_included`` not
    True means the prompt was built without the prior-context block - the
    continuity regression that re-introduces the misjudgment root cause. This
    complements ``_check_fair_path_continuity_real_injection`` (which checks
    the persisted block's status); this check catches the prompt-level flag.
    Severity: ``error``.
    """
    issues: list[dict[str, Any]] = []
    # R6-E P1-3 #4 (second half): marker/time bound BEFORE LIMIT (see
    # _llm_attempt_check_created_at_lower_bound).
    bound = _llm_attempt_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, batch_id, analysis_time, signal_grade, decision,
               raw_decision_json
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        pcc = raw.get("llm_provider_call_count")
        if not (isinstance(pcc, int) and pcc >= 1):
            # No physical call -> continuity flag is irrelevant (no prompt
            # was sent). Not a defect.
            continue
        included = raw.get("llm_continuity_included")
        if included is True:
            continue
        issues.append(_issue(
            LLM_CONTINUITY_NOT_INCLUDED, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": str(r["symbol"] or ""),
                "batch_id": str(r["batch_id"] or "") or "",
                "grade": r["signal_grade"],
                "decision": r["decision"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_provider_call_count": pcc,
                "llm_continuity_included": included,
            },
            "continuity 未包含：决策进行了 provider call 但 prompt 未携带 "
            "analysis_continuity 块。S1 (P0 #1) 预注入被绕过 -> LLM 在无前序分析"
            "上下文下判定（误判/饥饿根因）。检查 process_fair_batch 预注入循环与 "
            "build_llm_decision_prompt 的 continuity 拼装。",
        ))
    return issues


def _check_llm_timeout_config_out_of_range(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10) / 07-22 Codex P2 exclude-only: flag a decision
    whose persisted ``llm_provider_timeout_ms`` is outside the valid config
    range on a symbol that made a real provider call.

    The per-symbol deadline (Phase B) derives ``provider_timeout_ms =
    min(per_attempt_timeout_ms, remaining_ms())``. Config validation
    (``PerSymbolDeadline``) requires ``per_symbol_timeout_seconds ∈ [180,
    1200]`` and ``per_attempt > 0``. So a persisted ``llm_provider_timeout_ms``
    on a real call (``llm_provider_call_count >= 1``) MUST be in ``(0,
    _LLM_PER_SYMBOL_TIMEOUT_MAX_MS]``. A value of 0 on a real call (deadline
    already exhausted yet a call still happened) or a value exceeding the
    configured cap, or a negative value, is a config/timeout regression that
    would either let a call run unbounded or mis-bounds the hard kill. ``0``
    on a no-call skip path is the legitimate exhausted-deadline value and is
    NOT flagged.

    Severity / window (unique contract, 07-22 Codex P2):
    - Post-envelope-marker violations → ``error``.
    - Pre-marker historical rows (e.g. d49) stay in ``ga_decisions`` for audit
      but are SQL-excluded from this check (``created_at >= marker applied_at``)
      so they never enter issues — not as error and not as ``legacy_info``.
    - Marker missing → independent fail-closed error from the marker-missing
      check; this SQL bound falls back to a 24h window only.
    """
    issues: list[dict[str, Any]] = []
    # 07-22 Codex P2 exclude-only: SQL bound to envelope marker applied_at is
    # the sole pre-marker filter. Removing this bound is the revert-fail trigger
    # (pre-marker d49-shaped seed would reappear as current error).
    bound = _llm_timeout_envelope_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, batch_id, analysis_time, signal_grade, decision,
               raw_decision_json
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        pcc = raw.get("llm_provider_call_count")
        if not (isinstance(pcc, int) and pcc >= 1):
            continue
        pt_ms = raw.get("llm_provider_timeout_ms")
        if pt_ms is None:
            # Missing timeout metadata on a real call is already surfaced by
            # _check_llm_success_missing_attempt_metadata / the failed-path
            # envelope. Skip here to avoid double-flagging.
            continue
        try:
            pt = int(pt_ms)
        except (TypeError, ValueError):
            pt = None
        defect = None
        if pt is None:
            defect = "unparseable"
        elif pt < 0:
            defect = "negative"
        elif pt == 0:
            defect = "zero_on_real_call"
        elif pt > _LLM_PER_SYMBOL_TIMEOUT_MAX_MS:
            defect = "exceeds_per_symbol_cap"
        if defect is None:
            continue
        issues.append(_issue(
            LLM_TIMEOUT_CONFIG_OUT_OF_RANGE, "error",
            {
                "decision_id": int(r["id"]),
                "symbol": str(r["symbol"] or ""),
                "batch_id": str(r["batch_id"] or "") or "",
                "grade": r["signal_grade"],
                "decision": r["decision"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_provider_call_count": pcc,
                "llm_provider_timeout_ms": pt_ms,
                "valid_range_ms": f"(0, {_LLM_PER_SYMBOL_TIMEOUT_MAX_MS}]",
                "defect": defect,
            },
            "超时配置越界：真实 provider call 的 llm_provider_timeout_ms 超出有效区间 "
            f"(0, {_LLM_PER_SYMBOL_TIMEOUT_MAX_MS}]。硬超时门控会失效（无界调用或误杀）。"
            "检查 PerSymbolDeadline.provider_timeout_ms() 的 min(per_attempt, remaining) "
            "推导与 config/loader.py 的 [180,1200] 校验。",
        ))
    return issues


def _check_llm_batch_degraded_reported_healthy(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a batch that was capacity-degraded (first-
    attempt coverage < 1.0 with unexplained gaps) but whose rendered report
    claimed a healthy state.

    Per design §11.2 "Batch capacity insufficient before start: mark
    capacity-degraded and report it explicitly". A batch with
    ``llm_coverage_degraded == True`` (coverage < 1.0) whose persisted
    ``summary_json.llm_health`` claims full coverage
    (``llm_first_attempt_coverage >= 1.0``) OR whose batch ``status`` is
    ``success`` without a degraded marker is the false-healthy report defect:
    an operator reading the report cannot see the batch was degraded. This
    catches the case where the rendered "完成 10/10" hid a 3/10 coverage.
    Severity: ``error`` when coverage is missing entirely; ``warning`` when
    the degraded flag is merely not surfaced.
    """
    issues: list[dict[str, Any]] = []
    for r in _recent_success_batches(repo, limit=5):
        batch_id = str(r["batch_id"] or "")
        if not batch_id:
            continue
        agg = _reaggregate_batch_llm_outcomes(repo, batch_id)
        if not agg:
            continue
        expected = int(agg.get("expected_symbols") or 0)
        attempted = int(agg.get("llm_symbols_attempted") or 0)
        if expected <= 0:
            continue
        reaggregated_coverage = round(attempted / expected, 3)
        if reaggregated_coverage >= 1.0:
            continue  # genuinely full coverage
        summary = _safe_json(r["summary_json"]) or {}
        health = (summary if isinstance(summary, dict) else {}).get("llm_health") or {}
        persisted_coverage = health.get("llm_first_attempt_coverage")
        persisted_degraded = health.get("llm_coverage_degraded")
        # Defect: the persisted summary claims full coverage (>=1.0) OR does
        # NOT mark degraded, while the real re-aggregation is < 1.0.
        false_healthy = False
        if persisted_coverage is not None and float(persisted_coverage) >= 1.0:
            false_healthy = True
        elif persisted_degraded is not True:
            false_healthy = True
        if not false_healthy:
            continue
        issues.append(_issue(
            LLM_BATCH_DEGRADED_REPORTED_HEALTHY, "error",
            {
                "batch_id": batch_id,
                "analysis_time": int(r["analysis_time"] or 0),
                "expected_symbols": expected,
                "attempted_symbols": attempted,
                "reaggregated_coverage": reaggregated_coverage,
                "persisted_llm_first_attempt_coverage": persisted_coverage,
                "persisted_llm_coverage_degraded": persisted_degraded,
            },
            "批次降级但报告健康：实际首轮覆盖 < 1.0 但 summary_json.llm_health 声称"
            "全覆盖/未标降级。操作员看到'完成 10/10'无法察觉 3/10 的覆盖缺口（原始"
            "报告缺陷）。检查 finish_analysis_batch 是否在 coverage_degraded=True 时"
            "写入降级标记，以及 hourly_report 是否渲染（首轮覆盖不足）。",
        ))
    return issues


def _check_llm_repair_counted_as_provider_call(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-10 P1-1 (design §10): flag a decision where a schema-alias / unwrap
    REPAIR was counted as an additional provider call.

    A repair is a LOCAL re-parse / re-validation of an ALREADY-received
    response (``_try_repair_entry_trigger_confirmation`` operates on the
    in-memory decision, no new network call). The repair signal persisted at
    the top level of ``raw_decision_json`` is ``llm_terminal_reason ==
    "schema_repaired"`` (``_LLM_REPAIR_TERMINAL_REASONS`` in controller.py;
    set by ``_run_single_llm_attempt`` alongside ``attempt_meta["llm_repair_event"]``,
    but only ``llm_terminal_reason`` is surfaced by the decision_schema §8
    envelope - so this check reads the terminal reason, NOT ``llm_repair_event``).
    The §8 envelope distinguishes ``llm_provider_call_count`` (physical network
    calls) from ``llm_attempt_count`` (logical attempts incl. no-call skips). A
    repaired success has ``llm_terminal_reason == "schema_repaired"``,
    ``llm_attempt_count == 1``, ``llm_provider_call_count == 1``. The defect:
    ``llm_provider_call_count > llm_attempt_count`` on a repaired decision (a
    repair inflated the physical call counter) - which would over-count provider
    calls in the breaker / report, masking real call volume. Severity:
    ``warning``.
    """
    issues: list[dict[str, Any]] = []
    # R6-E P1-3 #4 (second half): marker/time bound BEFORE LIMIT (see
    # _llm_attempt_check_created_at_lower_bound).
    bound = _llm_attempt_check_created_at_lower_bound(repo)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, batch_id, analysis_time, signal_grade, decision,
               raw_decision_json
        FROM ga_decisions
        WHERE raw_decision_json IS NOT NULL
          AND created_at >= %s::timestamptz
        ORDER BY id DESC LIMIT 200
        """,
        (bound,),
    ).fetchall()
    for r in rows:
        raw = _safe_json(r["raw_decision_json"]) or {}
        if not isinstance(raw, dict):
            continue
        # Repair signal persisted at the top level (decision_schema §8 surfaces
        # llm_terminal_reason, NOT llm_repair_event).
        if str(raw.get("llm_terminal_reason") or "") != "schema_repaired":
            continue
        try:
            pcc = int(raw.get("llm_provider_call_count") or 0)
            attempt_count = int(raw.get("llm_attempt_count") or 0)
        except (TypeError, ValueError):
            continue
        # A repair must not make provider_call_count exceed attempt_count.
        # attempt_count includes no-call skips, so pcc <= attempt_count always
        # holds when accounting is correct; pcc > attempt_count means a repair
        # was billed as a provider call.
        if pcc <= attempt_count:
            continue
        issues.append(_issue(
            LLM_REPAIR_COUNTED_AS_PROVIDER_CALL, "warning",
            {
                "decision_id": int(r["id"]),
                "symbol": str(r["symbol"] or ""),
                "batch_id": str(r["batch_id"] or "") or "",
                "grade": r["signal_grade"],
                "decision": r["decision"],
                "analysis_time": int(r["analysis_time"] or 0),
                "llm_terminal_reason": "schema_repaired",
                "llm_attempt_count": attempt_count,
                "llm_provider_call_count": pcc,
            },
            "修复被计为 provider call：schema-alias/unwrap 修复（本地重解析，无网络）"
            "被计入 llm_provider_call_count，使物理调用计数虚高。修复路径不应增加 "
            "provider_call_count。检查 _run_single_llm_attempt 修复分支是否误增 "
            "provider_call_count，以及 breaker 事件是否用 repairable 而非 physical 计数。",
        ))
    return issues


def _reaggregate_batch_llm_outcomes(
    repo: CryptoGuardRepository, batch_id: str
) -> dict[str, Any]:
    """07-10 P1-1: independently re-aggregate a batch's LLM outcomes from its
    persisted ``ga_decisions`` §8 envelopes, mirroring the production
    ``_aggregate_batch_llm_outcomes`` definition but living here so the
    LLM_REPORT_COUNT_MISMATCH / coverage / starvation / degraded diagnostics
    do NOT trust the persisted ``summary_json.llm_health`` they are auditing.

    Returns an empty dict when ``batch_id`` is falsy or the batch has no
    decisions / no enabled_symbols row (so callers skip cleanly on legacy
    batches). The keys match the Phase E aggregate names so they are directly
    comparable to the persisted ``llm_health`` counters.
    """
    if not batch_id:
        return {}
    with repo.conn.transaction():
        rows = repo.list_ga_decisions_for_batch(batch_id)
    if not rows:
        return {}
    # Expected denominator from the batch row (the authoritative enabled set).
    with repo.conn.transaction():
        batch_row = repo.get_analysis_batch(batch_id)
    enabled_symbols = (
        list(batch_row.get("enabled_symbols") or [])
        if batch_row is not None
        else []
    )

    covered: set[str] = set()
    expected = 0
    attempted = 0
    provider_calls = 0
    success = 0
    failed = 0
    budget_skip = 0
    breaker_skip = 0
    policy_skip = 0
    repair = 0
    retry_calls = 0
    fallback_reasons: dict[str, int] = {}

    _BUDGET_SKIP = {"symbol_timeout", "batch_deadline_skipped"}
    _BREAKER_SKIP = {"breaker_skipped"}
    _REPAIR = {"schema_repaired"}
    # 07-10 P1-2 (terminal review): keep this set in lock-step with the
    # production ``_LLM_POLICY_SKIP_TERMINAL_REASONS`` (controller.py). The
    # fair coordinator's ``single_flight_skipped`` / ``missing_snapshot``
    # terminal reasons are LEGITIMATE upstream policy skips (pcc=0,
    # llm_status="skipped"); without this membership they would land in the
    # coverage diagnostic's unexplained residual and false-fire as a
    # silent-drop defect. See ``_policy_skip_result`` in llm_fair_scheduler.
    _POLICY_SKIP = {"llm_disabled", "single_flight_skipped", "missing_snapshot"}

    for row in rows:
        row_symbol = row.get("symbol")
        raw = row.get("raw_decision_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if not row_symbol and isinstance(raw.get("symbol"), str):
            row_symbol = raw.get("symbol")
        if isinstance(row_symbol, str) and row_symbol:
            covered.add(row_symbol)
        if enabled_symbols:
            pass
        else:
            expected += 1
        status = str(raw.get("llm_status") or "").lower()
        pcc = int(raw.get("llm_provider_call_count") or 0)
        terminal = str(raw.get("llm_terminal_reason") or "")
        attempt_count = int(raw.get("llm_attempt_count") or 0)
        fallback = str(raw.get("llm_fallback_reason") or "")
        provider_calls += pcc
        if pcc >= 1:
            attempted += 1
        if status == "ok":
            success += 1
        elif status == "failed":
            failed += 1
        if terminal in _REPAIR:
            repair += 1
        if terminal in _BUDGET_SKIP:
            budget_skip += 1
        if terminal in _BREAKER_SKIP:
            breaker_skip += 1
        if pcc == 0 and status != "failed":
            if terminal in _POLICY_SKIP or not terminal:
                policy_skip += 1
        if attempt_count > 1:
            retry_calls += attempt_count - 1
        if fallback:
            fallback_reasons[fallback] = fallback_reasons.get(fallback, 0) + 1

    if enabled_symbols:
        expected = len(enabled_symbols)
        worker_failed = len(set(enabled_symbols) - covered)
    else:
        worker_failed = 0
    coverage = round(attempted / expected, 3) if expected else 0.0
    dominant_reason = ""
    if fallback_reasons:
        dominant_reason = max(fallback_reasons, key=fallback_reasons.get)
    return {
        "expected_symbols": expected,
        "llm_symbols_attempted": attempted,
        "llm_physical_provider_calls": provider_calls,
        "llm_symbols_success": success,
        "llm_symbols_failed": failed,
        "llm_symbols_worker_failed": worker_failed,
        "llm_budget_skip_count": budget_skip,
        "llm_breaker_skip_count": breaker_skip,
        "llm_policy_skip_count": policy_skip,
        "llm_repair_count": repair,
        "llm_retry_calls": retry_calls,
        "llm_first_attempt_coverage": coverage,
        "llm_coverage_degraded": bool(expected and coverage < 1.0),
        "dominant_llm_fallback_reason": dominant_reason,
    }


# 07-14 R8 P2-NEW-1 (contract #4): staleness threshold for a "stuck prepared"
# skill-execution log. Mirrors ``recover_stale_prepared_skill_logs``'s default
# (cron_scheduler.py): a prepared row surviving past this age is no longer a
# legitimate in-flight producer tick -- the producer died before reaching the
# Phase-2 terminalization. The restart hook terminalizes these to ``aborted``;
# this diagnostic surfaces any that remain (e.g. on a node that has not yet
# restarted, or a row younger than the hook threshold but older than this).
SKILL_LOG_PREPARED_STALE_SECONDS = 600


def _check_stuck_prepared_skill_logs(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """07-14 R8 P2-NEW-1 (contract #4): surface ``skill_execution_logs`` rows
    stuck at ``commit_state='prepared'`` past the staleness threshold.

    A producer that crashes (OOM kill, power loss, OS fault) between Phase 1
    (the prepared skill-log autocommit write) and Phase 2 (the BEGIN IMMEDIATE
    commit/abort) leaves rows at ``prepared`` indefinitely. The restart
    recovery hook ``recover_stale_prepared_skill_logs`` (wired in
    ``service_manager.start_all_services``) terminalizes long-prepared rows to
    ``aborted`` so they stay excluded from learning (contract #5) yet are no
    longer silent stuck-state. BUT: (a) a node that has not yet restarted
    never runs the hook, and (b) a row younger than the hook threshold but
    older than a real tick (stuck mid-Phase-1) is also a signal.

    This runtime diagnostic queries ANY ``prepared`` row older than
    ``SKILL_LOG_PREPARED_STALE_SECONDS`` and emits one aggregate ``warning``
    issue per distinct (symbol, analysis_time) so an operator sees the stuck
    batch without restarting. Severity is ``warning`` (not ``error``): a stuck
    prepared row does NOT corrupt learning (the consumer gating at
    ``latest_skill_result_refs`` excludes it), it just signals the producer
    failed to terminalize -- which the restart hook will clean up on next
    start.

    This is a LIVE runtime invariant, NOT marker-cutoff-scoped: every
    ``prepared`` row is in-flight by definition, so there is no historical
    pre-marker audit to demote. It is windowed to recent rows
    (``created_at >= now - 24h``) so the diagnostic does not scan the full
    audit history on every hourly-report render.
    """
    issues: list[dict[str, Any]] = []
    try:
        # 07-16 PG adaptation: aggregate per (symbol, analysis_time) per the
        # contract -- do NOT group by ``skill_name`` (two stuck logs with
        # different skills on the same (symbol, analysis_time) are ONE stuck
        # batch with stuck_count=2, not two count=1 issues). ``timeframe`` is
        # aggregated via MIN so the SELECT stays PG-valid (no bare non-grouped
        # column) without widening the GROUP BY away from the contract.
        rows = repo.conn.execute(
            """
            SELECT symbol, MIN(timeframe) AS timeframe, analysis_time,
                   COUNT(*) AS stuck_count,
                   MIN(created_at) AS oldest_created_at,
                   MAX(created_at) AS newest_created_at
            FROM skill_execution_logs
            WHERE commit_state='prepared'
              AND created_at < NOW() + %s::interval
              AND created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY symbol, analysis_time
            ORDER BY oldest_created_at DESC
            LIMIT 50
            """,
            (f"-{int(SKILL_LOG_PREPARED_STALE_SECONDS)} seconds",),
        ).fetchall()
    except Exception:
        # ``run_for_report`` turns this into an explicit
        # diagnostic_query_failed issue and restores the connection.
        raise
    for r in rows:
        issues.append(_issue(
            STUCK_PREPARED_SKILL_LOGS, "warning",
            {
                "symbol": str(r["symbol"] or ""),
                "timeframe": str(r["timeframe"] or ""),
                "analysis_time": int(r["analysis_time"] or 0),
                "stuck_log_count": int(r["stuck_count"] or 0),
                "oldest_created_at": str(r["oldest_created_at"] or ""),
                "newest_created_at": str(r["newest_created_at"] or ""),
                "stale_threshold_seconds": int(SKILL_LOG_PREPARED_STALE_SECONDS),
            },
            "skill_execution_logs 存在长期 prepared 行（生产者 Phase 1 与 Phase 2 之间"
            "崩溃残留）。已排除出学习上下文（contract #5），但需重启恢复钩子"
            "（recover_stale_prepared_skill_logs）终态化为 aborted。若持续新增，"
            "排查 enqueue_market_analysis 是否在 Phase 1 后崩溃。",
        ))
    return issues
