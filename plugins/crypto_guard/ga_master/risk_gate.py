from __future__ import annotations

from typing import Any

from plugins.crypto_guard.risk.risk_engine import validate_trade_plan


class RiskGate:
    def check(self, decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        snapshot = context.get("snapshot") or {}
        risk = validate_trade_plan(decision, snapshot)
        risk["manual_bypass_allowed"] = False
        risk["checked_by"] = "ga_master_risk_gate"
        return risk
