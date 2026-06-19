from __future__ import annotations

from typing import Any


def classify_trade(trade: dict[str, Any]) -> str:
    """Classify a closed trade into a loss/win pattern.

    Order of checks matters: more specific patterns checked first.
    """
    pnl_r = float(trade.get("pnl_r") or 0)
    close_reason = trade.get("close_reason")
    mfe = float(trade.get("max_favorable_excursion") or 0)
    mae = float(trade.get("max_adverse_excursion") or 0)
    side = str(trade.get("side") or "").upper()

    if pnl_r > 0.05:
        return "good_execution"

    # Check regime context if available (from snapshot or stored field)
    regime = _get_regime_context(trade)

    # Market regime mismatch patterns (most specific first)
    if close_reason == "stop_loss" and regime:
        market_phase = regime.get("market_phase", "")
        alignment = regime.get("regime_alignment", "")

        if alignment == "counter_regime":
            if side == "SHORT" and market_phase in {"rebound", "risk_on"}:
                return "macro_rebound_short_squeeze_loss"
            if side == "LONG" and market_phase in {"selloff", "risk_off"}:
                return "macro_selloff_long_trap_loss"
            return "counter_regime_entry_loss"

        if side == "SHORT" and market_phase in {"rebound", "risk_on"}:
            return "market_regime_mismatch_short_loss"
        if side == "LONG" and market_phase in {"selloff", "risk_off"}:
            return "market_regime_mismatch_long_loss"

    # Standard patterns
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


def _get_regime_context(trade: dict[str, Any]) -> dict[str, Any] | None:
    """Extract market regime context from trade's snapshot or stored fields."""
    # Check stored market_regime_json on trade
    regime_json = trade.get("market_regime_json")
    if regime_json:
        import json
        if isinstance(regime_json, str):
            try:
                return json.loads(regime_json)
            except Exception:
                pass
        elif isinstance(regime_json, dict):
            return regime_json

    # Check snapshot context
    snapshot = trade.get("market_regime_at_loss")
    if snapshot and isinstance(snapshot, dict):
        return snapshot

    return None
