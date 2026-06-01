from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def build_market_analysis_state(
    *,
    snapshot: dict[str, Any],
    decision: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(snapshot["symbol"])
    analysis_time = int(snapshot.get("analysis_time_utc") or decision.get("analysis_time_utc") or 0)
    timeframes = list(decision.get("timeframes") or snapshot.get("timeframes") or (snapshot.get("profiles") or {}).keys())
    profiles = snapshot.get("profiles") or {}
    modules = snapshot.get("modules") or {}
    pa = modules.get("price_action") or {}
    momentum = modules.get("momentum") or {}
    trend = modules.get("trend_stage") or {}
    risk = decision.get("risk_check") or {}
    trade_plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
    key_levels = _key_levels(pa, profiles)
    no_trade = _no_trade_reason(decision, risk)
    paper_allowed = bool(decision.get("has_trade_plan") and trade_plan and risk.get("ok"))
    next_minutes = 15 if "15m" in timeframes else 5
    next_time = _iso_from_ms(analysis_time + next_minutes * 60 * 1000)
    state = {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": _iso_from_ms(analysis_time),
        "analysis_mode": str(snapshot.get("mode") or "unknown"),
        "timeframes": timeframes,
        "previous_state_id": (previous_state or {}).get("id"),
        "market_structure": {
            "direction_1d": _profile_value(profiles, "1d", "market_structure"),
            "direction_4h": _profile_value(profiles, "4h", "market_structure"),
            "trend_1h": _profile_value(profiles, "1h", "trend_stage"),
            "structure_15m": _profile_value(profiles, "15m", "market_structure"),
            "trigger_5m": _profile_value(profiles, "5m", "market_structure"),
            "structure_status": _structure_status(decision, pa, trend),
        },
        "trend_clarity": {
            "score": round(float(decision.get("confidence") or trend.get("confidence") or 0), 4),
            "level": _clarity_level(float(decision.get("confidence") or 0)),
            "reason": _clarity_reasons(profiles, decision, momentum),
        },
        "no_trade_reason": no_trade,
        "key_levels": key_levels,
        "next_triggers": _next_triggers(decision, key_levels, momentum),
        "next_analysis": {
            "suggested_time_utc": next_time,
            "reason": f"等待下一根 {next_minutes}m 已收盘 K 线确认",
        },
        "breakout_watch": _breakout_watch(decision, key_levels),
        "trade_permission": {
            "paper_trade_allowed": paper_allowed,
            "reason": "风控通过，允许模拟盘候选" if paper_allowed else "；".join(risk.get("reasons") or [no_trade.get("detail") or "未形成完整入场触发"]),
            "manual_bypass_allowed": False,
        },
        "opportunity_watch_recommended": bool("create_opportunity_watch" in (decision.get("suggested_actions") or [])),
        "trade_plan": trade_plan or {"has_trade_plan": False},
        "trend_evolution": _trend_evolution(snapshot, decision, previous_state),
    }
    return state


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _profile_value(profiles: dict[str, Any], timeframe: str, key: str) -> Any:
    profile = profiles.get(timeframe) or {}
    return profile.get(key) or "unknown"


def _structure_status(decision: dict[str, Any], pa: dict[str, Any], trend: dict[str, Any]) -> str:
    if decision.get("has_trade_plan"):
        return "trade_plan_candidate"
    if str(decision.get("decision") or "").startswith("wait_for"):
        return "waiting_confirmation"
    if pa.get("range_status") in {"breakout", "breakout_retest"}:
        return "breakout_watch"
    if trend.get("trend_stage") == "range":
        return "range_observation"
    return "no_edge" if decision.get("decision") == "no_edge" else "monitoring"


def _clarity_level(score: float) -> str:
    if score >= 0.72:
        return "clear"
    if score >= 0.55:
        return "mixed"
    return "unclear"


def _clarity_reasons(profiles: dict[str, Any], decision: dict[str, Any], momentum: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    p4h = profiles.get("4h") or {}
    p1h = profiles.get("1h") or {}
    p15 = profiles.get("15m") or {}
    reasons.append(f"4H 方向={p4h.get('market_structure', 'unknown')}")
    reasons.append(f"1H 趋势={p1h.get('trend_stage', 'unknown')}")
    reasons.append(f"15M 结构={p15.get('market_structure', 'unknown')}")
    reasons.append(f"动能={momentum.get('direction', 'unknown')}，分数={momentum.get('momentum_score', '-')}")
    if decision.get("counter_evidence"):
        reasons.append("存在反向证据：" + str((decision.get("counter_evidence") or [])[0])[:120])
    return reasons


def _key_levels(pa: dict[str, Any], profiles: dict[str, Any]) -> dict[str, Any]:
    levels = pa.get("key_levels") or {}
    support = levels.get("support") or []
    resistance = levels.get("resistance") or []
    range_info = pa.get("range") or {}
    lower = range_info.get("low") or (support[-1] if support else None)
    upper = range_info.get("high") or (resistance[-1] if resistance else None)
    waiting_zone = [x for x in [lower, upper] if x is not None]
    return {
        "support": support,
        "resistance": resistance,
        "invalid_level": pa.get("invalid_level"),
        "breakout_boundary": {"upper": upper, "lower": lower},
        "waiting_zone": waiting_zone,
        "background_4h": profiles.get("4h") or {},
    }


def _no_trade_reason(decision: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any]:
    has_no_trade = not bool(decision.get("has_trade_plan") and (decision.get("trade_plan") or {}))
    if not has_no_trade:
        return {"has_no_trade": False, "reason_code": "trade_plan_available", "detail": "存在通过风控前检查的交易计划。"}
    reasons = risk.get("reasons") or decision.get("risk_notes") or decision.get("counter_evidence") or []
    detail = "；".join(str(x) for x in reasons[:3]) if reasons else str(decision.get("summary") or "优势不足，等待结构确认。")
    code = "risk_rejected" if risk.get("reasons") else "waiting_for_confirmation"
    return {"has_no_trade": True, "reason_code": code, "detail": detail}


def _next_triggers(decision: dict[str, Any], key_levels: dict[str, Any], momentum: dict[str, Any]) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    boundary = key_levels.get("breakout_boundary") or {}
    if boundary.get("upper") is not None:
        triggers.append({"type": "breakout_confirm", "timeframe": "15m", "condition": f"15M 收盘站上 {boundary['upper']}"})
    if boundary.get("lower") is not None:
        triggers.append({"type": "breakdown_confirm", "timeframe": "15m", "condition": f"15M 收盘跌破 {boundary['lower']}"})
    triggers.append({"type": "momentum_confirm", "timeframe": "5m", "condition": f"动能方向从 {momentum.get('direction', 'neutral')} 转为与 4H 一致"})
    watch = decision.get("opportunity_watch") or {}
    for condition in (watch.get("conditions") or [])[:2]:
        triggers.append({"type": "opportunity_watch", "timeframe": "5m", "condition": str(condition)})
    return triggers[:5]


def _breakout_watch(decision: dict[str, Any], key_levels: dict[str, Any]) -> dict[str, Any]:
    boundary = key_levels.get("breakout_boundary") or {}
    direction = ((decision.get("trade_plan") or {}).get("side") or decision.get("market_bias") or "neutral")
    return {
        "enabled": bool(boundary.get("upper") is not None or boundary.get("lower") is not None),
        "direction": direction,
        "boundary_high": boundary.get("upper"),
        "boundary_low": boundary.get("lower"),
        "confirmation_required": "15M 收盘突破/跌破边界后，5M 回踩或反转确认；5M 不能单独推翻 4H。",
    }


def _trend_evolution(snapshot: dict[str, Any], decision: dict[str, Any], previous_state: dict[str, Any] | None) -> dict[str, Any]:
    profiles = snapshot.get("profiles") or {}
    small = profiles.get("5m") or {}
    mid = profiles.get("15m") or {}
    p4h = profiles.get("4h") or {}
    small_dir = small.get("market_structure")
    mid_dir = mid.get("market_structure")
    htf_dir = p4h.get("market_structure")
    growing = bool(small_dir and mid_dir and small_dir == mid_dir and (htf_dir in {small_dir, "range"}))
    previous = (previous_state or {}).get("state") or {}
    return {
        "small_tf_growth": growing,
        "from_timeframe": "5m",
        "target_timeframe": "15m",
        "condition": "5M 反转需守住结构边界，并由 15M 收盘确认后才视为升级。",
        "position_management_impact": "若确认升级，模拟盘可考虑止损移至保本；若失败，维持观察或撤销机会监控。",
        "previous_structure_status": ((previous.get("market_structure") or {}).get("structure_status") if previous else None),
    }
