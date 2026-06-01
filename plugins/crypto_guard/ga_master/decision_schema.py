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
) -> dict[str, Any]:
    risk_check = legacy.get("risk_check") or {"ok": False, "reasons": ["缺少风控记录"]}
    old_decision = str(legacy.get("decision") or "no_edge")
    final_decision = _final_decision(old_decision, legacy, risk_check)
    summary = str(legacy.get("summary") or legacy.get("final_summary") or "")
    return {
        "symbol": legacy["symbol"],
        "analysis_time": int(analysis_time),
        "analysis_time_utc": iso_from_ms(int(analysis_time)),
        "decision_type": decision_type,
        "signal_grade": str(legacy.get("signal_grade") or "D"),
        "confidence": float(legacy.get("confidence") or 0),
        "market_bias": legacy.get("market_bias") or "neutral",
        "trend_stage": legacy.get("trend_stage") or "unknown",
        "decision": final_decision,
        "legacy_decision": old_decision,
        "skill_result_refs": skill_result_refs,
        "evidence": list(legacy.get("evidence") or []),
        "counter_evidence": list(legacy.get("counter_evidence") or legacy.get("risk_notes") or ["缺少反向证据记录"]),
        "risk_check": risk_check,
        "trade_plan": legacy.get("trade_plan") if legacy.get("has_trade_plan") else None,
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
    }


def legacy_decision_from_ga_decision(ga_decision: dict[str, Any]) -> dict[str, Any]:
    raw = dict(ga_decision.get("raw_legacy_decision") or {})
    raw.update(
        {
            "symbol": ga_decision["symbol"],
            "decision": ga_decision.get("legacy_decision") or ga_decision.get("decision"),
            "signal_grade": ga_decision.get("signal_grade"),
            "confidence": ga_decision.get("confidence"),
            "market_bias": ga_decision.get("market_bias"),
            "trend_stage": ga_decision.get("trend_stage"),
            "summary": ga_decision.get("final_summary"),
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
