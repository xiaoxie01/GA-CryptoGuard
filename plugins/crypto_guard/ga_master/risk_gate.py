from __future__ import annotations

from typing import Any

from plugins.crypto_guard.risk.risk_engine import validate_trade_plan
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class RiskGate:
    def __init__(self, repo: CryptoGuardRepository | None = None):
        self.repo = repo

    def check(self, decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        snapshot = context.get("snapshot") or {}
        risk = validate_trade_plan(decision, snapshot)
        risk["manual_bypass_allowed"] = False
        risk["checked_by"] = "ga_master_risk_gate"

        # Account-level risk check (drawdown, risk_off, cooldown)
        account_risk_result = None
        if self.repo:
            from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard

            guard = AccountRiskGuard(self.repo)
            symbol = snapshot.get("symbol", "")
            plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
            side = str(plan.get("side", "")).upper() if plan else ""

            account_risk_result = guard.check(symbol=symbol, side=side)
            risk["account_risk"] = account_risk_result

            if account_risk_result.get("blocked") or account_risk_result.get("pause_active"):
                risk["ok"] = False
                risk["reasons"] = risk.get("reasons", [])
                risk["reasons"].append(f"账户风控拦截：{account_risk_result.get('blocked_reason') or account_risk_result.get('pause_reason')}")

            # hard_risk_off / daily_loss_pause — 全面暂停开仓
            if account_risk_result.get("pause_active"):
                risk["pause_active"] = True
                risk["pause_reason"] = account_risk_result.get("pause_reason")

            if account_risk_result.get("risk_off") and account_risk_result.get("effective_risk_percent"):
                risk["risk_off"] = True
                risk["effective_risk_percent"] = account_risk_result["effective_risk_percent"]

        return risk
