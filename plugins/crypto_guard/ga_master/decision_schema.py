from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class GAAnalysisRequest:
    symbol: str
    decision_type: str
    analysis_time_utc: int | None = None
    mode: str = "scheduled"
    timeframes: list[str] | None = None
    snapshot: dict[str, Any] | None = None
    snapshot_id: int | None = None
    requested_by: str | None = None
    request_text: str = ""
    allow_realtime_signal_alert: bool = False
    batch_id: str | None = None


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def controller_decision_from_legacy(
    *,
    legacy: dict[str, Any],
    decision_type: str,
    analysis_time: int,
    skill_result_refs: dict[str, int],
    feishu_actions: list[str],
    snapshot_id: int | None = None,
    analysis_state_id: int | None = None,
    batch_id: str | None = None,
    previous_grade: str | None = None,
    grade_delta_value: str | None = None,
) -> dict[str, Any]:
    risk_check = legacy.get("risk_check") or {"ok": False, "reasons": ["缺少风控记录"]}
    old_decision = str(legacy.get("decision") or "no_edge")
    final_decision = _final_decision(old_decision, legacy, risk_check)
    summary = str(legacy.get("summary") or legacy.get("final_summary") or "")
    # Phase E (07-05): plan lifecycle separation. ``candidate_trade_plan``
    # is the deterministic plan preserved for audit even when execution is
    # blocked (LLM failure, risk rejection, continuity invalidated).
    # ``plan_status`` / ``plan_source`` / ``plan_blockers`` carry the
    # structured reason so reports/diagnostics can distinguish
    # "withheld due to LLM failure" from "no plan ever generated".
    candidate_plan = legacy.get("candidate_trade_plan")
    if candidate_plan is None and legacy.get("trade_plan") and legacy.get("has_trade_plan"):
        # Executable plan — surface as candidate for audit parity.
        candidate_plan = legacy.get("trade_plan")
    plan_status = legacy.get("plan_status")
    if not plan_status:
        if legacy.get("has_trade_plan") and legacy.get("trade_plan"):
            plan_status = "executable"
        elif candidate_plan is not None:
            plan_status = "withheld"
        else:
            plan_status = "no_plan"
    # Phase F (07-05): raw vs effective grade/score separation.
    # raw_signal_grade / raw_score are the deterministic SOP's pre-gate
    # conclusions (captured before risk/hysteresis/clamp/perf gates).
    # effective_signal_grade / effective_execution_confidence are the
    # post-gate canonical values. canonical signal_grade remains the
    # effective grade for backward compatibility. grade_adjustments
    # records every post-gate downgrade with reason code for audit.
    raw_signal_grade = str(legacy.get("raw_signal_grade") or legacy.get("signal_grade") or "D").upper()
    raw_score_value = legacy.get("raw_score")
    if raw_score_value is None:
        raw_score_value = legacy.get("confidence")
    try:
        raw_score = round(float(raw_score_value or 0.0), 4)
    except (TypeError, ValueError):
        raw_score = 0.0
    effective_signal_grade = str(legacy.get("effective_signal_grade") or legacy.get("signal_grade") or "D").upper()
    effective_exec_conf_value = legacy.get("effective_execution_confidence")
    if effective_exec_conf_value is None:
        effective_exec_conf_value = legacy.get("confidence")
    try:
        effective_execution_confidence = round(float(effective_exec_conf_value or 0.0), 4)
    except (TypeError, ValueError):
        effective_execution_confidence = 0.0
    grade_adjustments = list(legacy.get("grade_adjustments") or [])
    # Phase G (07-05): ``analysis_time_utc`` must satisfy
    # ``ga_decision.schema.json`` (``analysis_time_utc: integer, minimum=1``)
    # when validated on the in-memory decision dict. The DB column
    # ``analysis_time_utc TEXT NOT NULL`` (schema.sql:149) stores whatever
    # value is passed, and 13+ SQL consumers in
    # ``diagnostics/state_consistency.py`` filter via
    # ``datetime(replace(replace(analysis_time_utc, 'T', ' '), 'Z', ''))``
    # which returns NULL for integer input (verified by SQLite
    # reproduction). The same applies to ``paper/position_conflict_revalidator.py:355``
    # (``datetime(analysis_time_utc) >= datetime(?)``) and
    # ``paper/pending_order_manager.py:122`` (``ORDER BY analysis_time_utc DESC``
    # misorders mixed integer/ISO-text populations — integer-text sorts
    # AFTER ISO-text because ``'1' < '2'``).
    #
    # R13 P0 fix: keep ``analysis_time_utc`` as the ISO string (matching
    # the pre-task behavior and all SQL consumers). The schema-required
    # integer is already on the ``analysis_time`` column (INTEGER NOT NULL,
    # schema.sql:148) and on the in-memory decision dict's
    # ``decision["analysis_time"]`` field (line 99 below, ``at_int``).
    # ``ga_judge.py`` builds the in-memory dict with integer
    # ``analysis_time_utc`` (line 471, 633) for schema validation, then
    # ``controller_decision_from_legacy`` is called afterwards to
    # produce the DB-persistence shape — at this point the schema has
    # already been validated, so we can safely convert to ISO for the DB.
    # ``analysis_time_iso`` (added by this task) remains as a separate
    # human-readable display field.
    at_int = int(analysis_time)
    return {
        "symbol": legacy["symbol"],
        "analysis_time": at_int,
        "analysis_time_utc": iso_from_ms(at_int),
        "analysis_time_iso": iso_from_ms(at_int),
        "decision_type": decision_type,
        "signal_grade": str(legacy.get("signal_grade") or "D"),
        "raw_signal_grade": raw_signal_grade,
        "raw_score": raw_score,
        "effective_signal_grade": effective_signal_grade,
        "effective_execution_confidence": effective_execution_confidence,
        "grade_adjustments": grade_adjustments,
        "confidence": float(legacy.get("confidence") or 0),
        "market_bias": legacy.get("market_bias") or "neutral",
        "trend_stage": legacy.get("trend_stage") or "unknown",
        "decision": final_decision,
        "legacy_decision": old_decision,
        "timeframe_context": legacy.get("timeframe_context") or {},
        "alignment": legacy.get("alignment"),
        "htf_conflict": legacy.get("htf_conflict"),
        "market_reason_codes": list(legacy.get("market_reason_codes") or []),
        "skill_result_refs": skill_result_refs,
        "evidence": list(legacy.get("evidence") or []),
        "counter_evidence": list(legacy.get("counter_evidence") or legacy.get("risk_notes") or ["缺少反向证据记录"]),
        "risk_check": risk_check,
        "trade_plan": legacy.get("trade_plan") if legacy.get("has_trade_plan") else None,
        "candidate_trade_plan": candidate_plan,
        "plan_status": plan_status,
        "plan_source": legacy.get("plan_source") or "deterministic_sop",
        "plan_blockers": list(legacy.get("plan_blockers") or []),
        "opportunity_watch": legacy.get("opportunity_watch"),
        "feishu_actions": feishu_actions,
        "final_summary": summary,
        "summary": summary,
        "raw_legacy_decision": legacy,
        "analysis_state_id": analysis_state_id,
        "snapshot_id": snapshot_id,
        "created_by": "ga_master_controller",
        "analysis_source": legacy.get("analysis_source") or "ga_master_controller",
        "llm_status": legacy.get("llm_status") or "ok",
        "llm_error": legacy.get("llm_error"),
        # Phase B (07-07): new LLM error taxonomy fields (design §3.4)
        "llm_error_category": legacy.get("llm_error_category"),
        "llm_error_stage": legacy.get("llm_error_stage"),
        "llm_attempt_count": legacy.get("llm_attempt_count"),
        "llm_retry_round": legacy.get("llm_retry_round"),
        "llm_config_name": legacy.get("llm_config_name"),
        "llm_model": legacy.get("llm_model"),
        "llm_fallback_reason": legacy.get("llm_fallback_reason"),
        # Phase B (07-07): plan state model (design §6.1)
        "plan_origin": legacy.get("plan_origin"),
        "plan_execution_state": legacy.get("plan_execution_state"),
        # Phase B (07-07): 5M bias surfaced by _apply_htf_alignment_caps
        # (market_semantics.py). 5M is data-only in TIMEFRAME_CONTEXT_TFS,
        # so it cannot live under timeframe_context (schema
        # additionalProperties:false). Exposed at top level so diagnostics
        # can read it from raw_decision_json without re-fetching snapshot.
        "m5_bias": legacy.get("m5_bias"),
        "batch_id": batch_id,
        "previous_grade": previous_grade,
        "grade_delta": grade_delta_value,
    }


def legacy_decision_from_ga_decision(ga_decision: dict[str, Any]) -> dict[str, Any]:
    raw = dict(ga_decision.get("raw_legacy_decision") or {})
    # Phase C (07-03): prefer rendered_summary (canonical) over final_summary
    # for the legacy summary field so downstream signal/brief consumers read
    # the deterministic canonical text. The original LLM text is preserved
    # in raw["raw_llm_summary"] by the controller.
    summary = ga_decision.get("rendered_summary") or ga_decision.get("final_summary")
    raw.update(
        {
            "symbol": ga_decision["symbol"],
            "decision": ga_decision.get("legacy_decision") or ga_decision.get("decision"),
            "signal_grade": ga_decision.get("signal_grade"),
            "confidence": ga_decision.get("confidence"),
            # Phase F (07-05): propagate raw/effective grade/score so
            # downstream consumers (compat layer) can access them.
            "raw_signal_grade": ga_decision.get("raw_signal_grade"),
            "raw_score": ga_decision.get("raw_score"),
            "effective_signal_grade": ga_decision.get("effective_signal_grade"),
            "effective_execution_confidence": ga_decision.get("effective_execution_confidence"),
            "grade_adjustments": ga_decision.get("grade_adjustments") or [],
            "market_bias": ga_decision.get("market_bias"),
            "trend_stage": ga_decision.get("trend_stage"),
            "summary": summary,
            "evidence": ga_decision.get("evidence") or [],
            "counter_evidence": ga_decision.get("counter_evidence") or [],
            "risk_check": ga_decision.get("risk_check") or {},
            "trade_plan": ga_decision.get("trade_plan"),
            "has_trade_plan": bool(ga_decision.get("trade_plan")),
            "opportunity_watch": ga_decision.get("opportunity_watch"),
            "suggested_actions": list(ga_decision.get("feishu_actions") or []),
            "ga_decision_id": ga_decision.get("ga_decision_id") or ga_decision.get("id"),
            "analysis_time_utc": ga_decision.get("analysis_time"),
            "analysis_source": "ga_master_controller",
            "llm_status": raw.get("llm_status") or "controller",
        }
    )
    return raw


def _final_decision(old_decision: str, legacy: dict[str, Any], risk_check: dict[str, Any]) -> str:
    grade = str(legacy.get("signal_grade") or "D").upper()
    if legacy.get("has_trade_plan") and legacy.get("trade_plan") and risk_check.get("ok") and grade in {"S", "A"}:
        return "create_paper_order"
    if old_decision.startswith("wait_for") or (legacy.get("opportunity_watch") and grade in {"S", "A", "B"}):
        return "opportunity_watch"
    if old_decision in {"monitor_only", "trade_plan_available"}:
        return "monitor_only"
    return "no_edge"

# Phase B (07-09): re-export the alias-normalization helper from
# ``reasoning.decision_schema`` to avoid a circular import
# (``ga_master.__init__`` -> ``controller`` -> ``llm_agent_judge``
# -> ``ga_master.decision_schema``). The implementation lives in
# ``reasoning.decision_schema``; this re-export keeps tests and
# callers that import from ``ga_master.decision_schema`` working
# without triggering the package __init__.
from plugins.crypto_guard.reasoning.decision_schema import (
    normalize_entry_trigger_confirmation,
)

__all__ = [
    "GAAnalysisRequest",
    "iso_from_ms",
    "controller_decision_from_legacy",
    "legacy_decision_from_ga_decision",
    "normalize_entry_trigger_confirmation",
]
