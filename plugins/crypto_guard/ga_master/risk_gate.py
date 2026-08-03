from __future__ import annotations

from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class RiskGate:
    def __init__(self, repo: CryptoGuardRepository | None = None):
        self.repo = repo

    def check(self, decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        snapshot = context.get("snapshot") or {}

        # Codex terminal-review P1-1: NEVER re-validate the plan here.
        # ``apply_risk_to_decision`` (risk_engine) already ran
        # ``validate_trade_plan`` EXACTLY ONCE on the ORIGINAL proposed plan
        # and stored the immutable result under ``decision["risk_check"]``.
        # Re-validating in this gate would run it a SECOND time — on the
        # ALREADY-CLEARED state (risk rejection / LLM failure / continuity
        # invalidation each clear has_trade_plan/trade_plan), which re-derives
        # "缺少完整 trade_plan" and collapses the real blocker reason. Instead
        # copy the immutable upstream risk_check and independently attach the
        # account-risk result below. Defensive fail-closed default (no
        # upstream risk_check) never re-validates either.
        upstream = decision.get("risk_check")
        if isinstance(upstream, dict):
            risk = dict(upstream)
        else:
            risk = {
                "ok": False,
                "reasons": ["缺少上游 risk_check（apply_risk_to_decision 未先运行），禁止开仓"],
                "metrics": {},
            }
        risk["manual_bypass_allowed"] = False
        risk["checked_by"] = "ga_master_risk_gate"

        # Phase E (07-05): plan lifecycle separation. When the upstream
        # apply_risk_to_decision path already set structured plan_blockers
        # (LLM parse failed / LLM disabled / continuity invalidated / risk
        # rejected), preserve those reasons on the copied risk_check so
        # downstream consumers (report, diagnostics) see the actual blocking
        # stage. The copied upstream reasons already carry the ORIGINAL
        # validation output; the augmentation appends the structured blocker
        # audit on top without ever re-deriving from the cleared state.
        plan_blockers = list(decision.get("plan_blockers") or [])
        if plan_blockers:
            existing = list(risk.get("reasons") or [])
            # 08-02 P1-2: when the upstream apply_risk_to_decision path already
            # validated the ORIGINAL proposed plan ONCE and cleared it (setting
            # a structured dict blocker with a code), any residual
            # "缺少完整 trade_plan" from a collapsed state is a by-product, NOT
            # a real reason — it would overwrite the actual blocking stage.
            # Drop it so the preserved blocker reason leads.
            # String-only blockers (e.g. continuity_unavailable, where no plan
            # gate ever ran) leave it intact.
            _has_structured_blocker = any(
                isinstance(b, dict) and bool(b.get("code"))
                for b in plan_blockers
            )
            if _has_structured_blocker:
                existing = [r for r in existing if r != "缺少完整 trade_plan"]
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
            # Codex P1-1: the account-risk side must come from the ORIGINAL /
            # llm_synthesis plan, NOT the cleared ``trade_plan``. On the
            # risk-rejected / LLM-failed / continuity-invalidated paths
            # ``has_trade_plan`` is already False and ``trade_plan`` is None;
            # fall back to the preserved candidate plan so the account gate
            # still sees the intended direction.
            plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
            if plan is None:
                plan = decision.get("candidate_trade_plan")
            if plan is None:
                plan = decision.get("llm_synthesis_trade_plan")
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
