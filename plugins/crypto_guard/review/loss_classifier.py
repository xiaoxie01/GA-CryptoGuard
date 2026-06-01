from __future__ import annotations

from typing import Any


def classify_trade(trade: dict[str, Any]) -> str:
    pnl_r = float(trade.get("pnl_r") or 0)
    close_reason = trade.get("close_reason")
    mfe = float(trade.get("max_favorable_excursion") or 0)
    mae = float(trade.get("max_adverse_excursion") or 0)
    if pnl_r > 0.05:
        return "good_execution"
    if float(trade.get("signal_decay_score") or 0) >= 0.75:
        return "late_trend_chasing"
    if close_reason == "stop_loss":
        if float(trade.get("entry_efficiency") or 1) < 0.25:
            return "entry_chasing"
        if mfe > abs(mae) and mfe > 0:
            return "entry_too_late"
        return "wrong_direction"
    if pnl_r < -0.5:
        if float(trade.get("max_adverse_excursion") or 0) < -abs(float(trade.get("max_favorable_excursion") or 0)) * 1.5:
            return "stop_loss_too_tight"
        return "entry_too_early"
    if close_reason == "timeout":
        return "take_profit_too_far"
    return "unknown"
