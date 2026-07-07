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

        # Phase E (07-05): plan lifecycle separation. When the upstream
        # apply_risk_to_decision path already set structured plan_blockers
        # (LLM parse failed / LLM disabled / continuity invalidated / risk
        # rejected), preserve those reasons on the re-computed risk_check
        # so downstream consumers (report, diagnostics) see the actual
        # blocking stage. Otherwise validate_trade_plan re-derives
        # reasons from the now-cleared trade_plan=None state and collapses
        # to "缺少完整 trade_plan", hiding the real blocker.
        plan_blockers = list(decision.get("plan_blockers") or [])
        if plan_blockers:
            existing = list(risk.get("reasons") or [])
            for blocker in plan_blockers:
                if not isinstance(blocker, dict):
                    continue
                code = str(blocker.get("code") or "")
                stage = str(blocker.get("stage") or "")
                detail = str(blocker.get("detail") or "")
                if code == "llm_parse_failed":
                    existing.append(
                        f"LLM 解析失败（{stage}），候选计划已保留为 candidate_trade_plan"
                    )
                elif code == "llm_disabled":
                    existing.append(
                        f"LLM 已禁用（{stage}），候选计划已保留为 candidate_trade_plan"
                    )
                elif code == "risk_rejected":
                    existing.append(
                        f"风控未通过（{stage}）：{detail}"
                    )
                elif code == "continuity_trigger_invalidated":
                    existing.append(
                        f"前次触发已被反转（{stage}），候选计划已保留为 candidate_trade_plan"
                    )
                elif code:
                    existing.append(f"{code}（{stage}）：{detail}")
            risk["reasons"] = existing
            # Mark that this risk_check carries structured blocker audit
            # so diagnostics can distinguish from a collapsed
            # "缺少完整 trade_plan" only state.
            risk["has_structured_blockers"] = True

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
