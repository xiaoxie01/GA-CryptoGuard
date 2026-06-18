from __future__ import annotations

from typing import Any

from plugins.crypto_guard.strategy.grade_config import PUSH_GRADES, WATCH_GRADES, STORE_ONLY_GRADES, is_paper_order_eligible


def build_feishu_actions(decision: dict[str, Any], risk_check: dict[str, Any] | None = None) -> list[str]:
    grade = str(decision.get("signal_grade") or "D").upper()
    risk = risk_check or decision.get("risk_check") or {}
    has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
    risk_ok = bool(risk.get("ok"))
    confidence = float(decision.get("confidence") or 0)

    if grade in STORE_ONLY_GRADES:
        return ["add_to_watchlist", "ignore"]
    if grade in WATCH_GRADES:
        return ["create_opportunity_watch", "add_to_watchlist", "ignore"]
    if grade in PUSH_GRADES and has_plan and risk_ok and is_paper_order_eligible(grade, confidence):
        return ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore"]
    if grade in PUSH_GRADES:
        return ["create_opportunity_watch", "add_to_watchlist", "ignore"]
    return ["add_to_watchlist", "ignore"]
