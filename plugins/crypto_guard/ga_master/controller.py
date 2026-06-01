from __future__ import annotations

from typing import Any

from plugins.crypto_guard.ga_master.context_builder import ContextBuilder
from plugins.crypto_guard.ga_master.decision_persistence import DecisionPersistence
from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest, controller_decision_from_legacy, legacy_decision_from_ga_decision
from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions
from plugins.crypto_guard.ga_master.risk_gate import RiskGate
from plugins.crypto_guard.ga_master.skill_orchestrator import SkillOrchestrator
from plugins.crypto_guard.reasoning.analysis_state import build_market_analysis_state
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class GAMasterController:
    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo
        self.context_builder = ContextBuilder(repo)
        self.skill_orchestrator = SkillOrchestrator(repo)
        self.risk_gate = RiskGate()
        self.persistence = DecisionPersistence(repo)

    def analyze_symbol(self, request: GAAnalysisRequest) -> dict[str, Any]:
        context = self.context_builder.build(request)
        snapshot = context["snapshot"]
        legacy = run_agent_sop_decision(snapshot, context=context)
        legacy["analysis_source"] = "ga_master_controller"

        risk = self.risk_gate.check(legacy, context)
        legacy["risk_check"] = risk
        if legacy.get("has_trade_plan") and legacy.get("trade_plan") and not risk.get("ok"):
            legacy["has_trade_plan"] = False
            legacy["decision"] = "monitor_only"
            notes = list(legacy.get("risk_notes") or [])
            notes.append("GA Master 风控未通过：" + "；".join(risk.get("reasons") or []))
            legacy["risk_notes"] = notes

        feishu_actions = build_feishu_actions(legacy, risk)
        legacy["suggested_actions"] = feishu_actions

        previous_state = context.get("previous_analysis_state")
        analysis_state = build_market_analysis_state(snapshot=snapshot, decision=legacy, previous_state=previous_state)
        analysis_state_id = self.repo.save_analysis_state(analysis_state)
        legacy["analysis_state_id"] = analysis_state_id
        legacy["market_analysis_state"] = analysis_state

        skill_refs = self.skill_orchestrator.result_refs(context)
        ga_decision = controller_decision_from_legacy(
            legacy=legacy,
            decision_type=request.decision_type,
            analysis_time=int(context["analysis_time_utc"]),
            skill_result_refs=skill_refs,
            feishu_actions=feishu_actions,
            snapshot_id=context.get("snapshot_id"),
            analysis_state_id=analysis_state_id,
        )
        saved = self.persistence.save(ga_decision)

        # Return a compatibility shape to existing callers, with GADecision attached.
        compat = legacy_decision_from_ga_decision(saved)
        compat["ga_decision"] = saved
        compat["ga_decision_id"] = saved["ga_decision_id"]
        compat["signal_id"] = saved.get("signal_id")
        compat["analysis_state_id"] = analysis_state_id
        compat["market_analysis_state"] = analysis_state
        compat["suggested_actions"] = feishu_actions
        return compat
