from __future__ import annotations

from typing import Any

from plugins.crypto_guard.ga_master.context_builder import ContextBuilder
from plugins.crypto_guard.ga_master.decision_persistence import DecisionPersistence
from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest, controller_decision_from_legacy, legacy_decision_from_ga_decision
from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions
from plugins.crypto_guard.ga_master.performance_gate import PerformanceGate
from plugins.crypto_guard.ga_master.risk_gate import RiskGate
from plugins.crypto_guard.ga_master.skill_orchestrator import SkillOrchestrator
from plugins.crypto_guard.reasoning.analysis_state import build_market_analysis_state
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.strategy.shadow_testing import record_shadow_evaluation
import json


def _find_shadow_candidate(repo: CryptoGuardRepository, strategy_name: str) -> str | None:
    """Find the latest candidate version in shadow_testing status."""
    for version in repo.list_strategy_versions(strategy_name):
        if version.get("status") in {"candidate", "shadow_testing"}:
            return str(version["version"])
    return None


def _load_candidate_patch(repo: CryptoGuardRepository, strategy_name: str, candidate_version: str) -> dict[str, Any]:
    """Load candidate patch from strategy_patches."""
    row = repo.conn.execute(
        "SELECT patch_json FROM strategy_patches WHERE strategy_name=? AND candidate_version=? ORDER BY id DESC LIMIT 1",
        (strategy_name, candidate_version),
    ).fetchone()
    if row and row["patch_json"]:
        try:
            return json.loads(row["patch_json"])
        except Exception:
            pass
    return {}


def _evaluate_shadow_candidate(
    active_decision: dict[str, Any],
    candidate_patch: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate shadow candidate based on deterministic patch effects.

    Applies real semantic changes to decision/score based on candidate patch.
    Returns evaluation that reflects how candidate would have decided.
    """
    # Start with active decision as baseline
    shadow_decision = active_decision.get("decision", "unknown")
    shadow_score = float(active_decision.get("confidence", 0.0))
    evidence = {
        "source": "deterministic_candidate",
        "active_decision": active_decision.get("decision"),
        "active_score": shadow_score,
        "candidate_patch_applied": bool(candidate_patch),
    }

    if not candidate_patch:
        # No patch to apply - mark as not promotable
        evidence["not_promotable"] = True
        evidence["reason"] = "empty_patch"
        return {"decision": shadow_decision, "score": shadow_score, "evidence": evidence}

    patch_data = candidate_patch.get("patch", candidate_patch)
    is_promotable = True

    # 1. Apply score_adjustment(s) - directly modify score
    score_adj = patch_data.get("score_adjustment") or patch_data.get("score_adjustments")
    if score_adj is not None:
        if isinstance(score_adj, (int, float)):
            shadow_score = max(0.0, min(1.0, shadow_score + float(score_adj)))
            evidence["score_adjustment"] = float(score_adj)
        elif isinstance(score_adj, dict):
            # Per-skill adjustments
            total_adj = sum(float(v) for v in score_adj.values() if isinstance(v, (int, float)))
            shadow_score = max(0.0, min(1.0, shadow_score + total_adj))
            evidence["score_adjustments"] = score_adj

    # 2. Apply risk_controls - may change decision
    risk_controls = patch_data.get("risk_controls", [])
    if risk_controls:
        evidence["risk_controls"] = risk_controls
        # Specific risk controls that change decision
        if "pause_after_trigger" in risk_controls:
            # Candidate would pause after trigger - downgrade to monitor
            if shadow_decision in {"trade_plan_available", "opportunity_watch"}:
                shadow_decision = "monitor_only"
                evidence["decision_changed_by"] = "pause_after_trigger"
        if "require_structure_momentum_alignment" in risk_controls:
            # Stricter alignment requirement - might block some trades
            evidence["stricter_alignment_required"] = True

    # 3. Apply paper_order_permission - affects decision semantics
    paper_order_perm = patch_data.get("paper_order_permission")
    if paper_order_perm:
        evidence["paper_order_permission"] = paper_order_perm
        if paper_order_perm == "shadow_testing_only":
            # Candidate would not create paper orders - downgrade decision
            if shadow_decision == "trade_plan_available":
                shadow_decision = "opportunity_watch"
                evidence["decision_changed_by"] = "shadow_testing_only_permission"

    # 4. Check patch status for promotability
    patch_status = patch_data.get("status")
    if patch_status:
        evidence["candidate_status"] = patch_status

    # Mark if candidate would have decided differently
    if shadow_decision != active_decision.get("decision"):
        evidence["decision_changed"] = True

    return {
        "decision": shadow_decision,
        "score": shadow_score,
        "evidence": evidence,
    }


class GAMasterController:
    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo
        self.context_builder = ContextBuilder(repo)
        self.skill_orchestrator = SkillOrchestrator(repo)
        self.risk_gate = RiskGate(repo)
        self.performance_gate = PerformanceGate(repo)
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

        # Account risk_off state — visible in ga_decisions for monitoring
        account_risk = risk.get("account_risk") or {}
        legacy["account_risk_off"] = bool(account_risk.get("risk_off"))
        legacy["hard_risk_off"] = bool(account_risk.get("hard_risk_off"))
        legacy["daily_loss_pause"] = bool(account_risk.get("daily_loss_pause"))
        legacy["pause_active"] = bool(account_risk.get("pause_active"))
        legacy["account_risk_off_reason"] = account_risk.get("pause_reason") or account_risk.get("reason")
        if account_risk.get("pause_active"):
            # hard_risk_off 或 daily_loss_pause — 强制 monitor_only
            legacy["has_trade_plan"] = False
            legacy["decision"] = "monitor_only"
            notes = list(legacy.get("risk_notes") or [])
            notes.append(f"账户暂停开仓：{account_risk.get('pause_reason')}")
            legacy["risk_notes"] = notes
        elif account_risk.get("risk_off") and account_risk.get("effective_risk_percent"):
            # Inject reduced risk percent into trade_plan if present
            plan = legacy.get("trade_plan")
            if plan:
                plan["risk_percent"] = account_risk["effective_risk_percent"]

        # Performance gate check (context-based degradation and cooldown)
        symbol = snapshot.get("symbol", "")
        side = legacy.get("trade_plan", {}).get("side", "").upper() if legacy.get("trade_plan") else ""
        signal_grade = legacy.get("signal_grade", "C")
        trend_stage = legacy.get("trend_stage", "transition")
        confidence = legacy.get("confidence", 0.0)

        perf_gate = self.performance_gate.check(
            symbol=symbol,
            side=side,
            signal_grade=signal_grade,
            trend_stage=trend_stage,
            confidence=confidence,
        )
        legacy["performance_gate"] = perf_gate

        # Apply performance gate results
        if perf_gate.get("should_watch_only"):
            legacy["has_trade_plan"] = False
            legacy["decision"] = "opportunity_watch"
            notes = list(legacy.get("risk_notes") or [])
            notes.append("Performance gate 降级：" + "；".join(perf_gate.get("reasons") or []))
            legacy["risk_notes"] = notes
        elif perf_gate.get("performance_degraded"):
            # Update grade if degraded
            legacy["signal_grade"] = perf_gate.get("effective_grade", signal_grade)
            notes = list(legacy.get("risk_notes") or [])
            notes.append(f"信号降级：{signal_grade}→{perf_gate.get('effective_grade')}")
            legacy["risk_notes"] = notes

        # Apply confidence adjustment if any
        if perf_gate.get("confidence_adjustment", 0) < 0:
            legacy["confidence"] = perf_gate.get("effective_confidence", confidence)

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

        # P0: 写入 shadow 评估 — 为 shadow_testing 候选积累样本
        strategy_name = legacy.get("strategy_name", "smc_pullback_long")
        candidate_version = _find_shadow_candidate(self.repo, strategy_name)
        if candidate_version:
            try:
                # 加载 candidate patch 用于真实评估
                candidate_patch = _load_candidate_patch(self.repo, strategy_name, candidate_version)
                shadow_decision = _evaluate_shadow_candidate(
                    active_decision=legacy,
                    candidate_patch=candidate_patch,
                    snapshot=snapshot,
                )
                record_shadow_evaluation(
                    self.repo,
                    symbol=symbol,
                    timeframe=snapshot.get("timeframe", "1h"),
                    analysis_time_utc=int(context["analysis_time_utc"]),
                    strategy_name=strategy_name,
                    strategy_version=candidate_version,
                    score=shadow_decision.get("score", 0.0),
                    decision=shadow_decision.get("decision", "unknown"),
                    evidence=shadow_decision.get("evidence", {}),
                    snapshot_id=context.get("snapshot_id"),
                )
            except Exception:
                pass  # Shadow evaluation failure should not block main flow

        # Return a compatibility shape to existing callers, with GADecision attached.
        compat = legacy_decision_from_ga_decision(saved)
        compat["ga_decision"] = saved
        compat["ga_decision_id"] = saved["ga_decision_id"]
        compat["signal_id"] = saved.get("signal_id")
        compat["analysis_state_id"] = analysis_state_id
        compat["market_analysis_state"] = analysis_state
        compat["suggested_actions"] = feishu_actions
        return compat
