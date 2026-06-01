from __future__ import annotations

from typing import Any


def build_feishu_actions(decision: dict[str, Any], risk_check: dict[str, Any] | None = None) -> list[str]:
    grade = str(decision.get("signal_grade") or "D").upper()
    risk = risk_check or decision.get("risk_check") or {}
    has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
    risk_ok = bool(risk.get("ok"))

    if grade in {"D", "C"}:
        return ["add_to_watchlist", "ignore"]
    if grade == "B":
        return ["create_opportunity_watch", "add_to_watchlist", "ignore"]
    if grade in {"A", "S"} and has_plan and risk_ok:
        return ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore"]
    if grade in {"A", "S"}:
        return ["create_opportunity_watch", "add_to_watchlist", "ignore"]
    return ["add_to_watchlist", "ignore"]
