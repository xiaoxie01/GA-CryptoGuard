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
from plugins.crypto_guard.paper.shadow_virtual_trade_updater import DEFAULT_MAX_PENDING_MINUTES
import json


def _find_shadow_candidates(repo: CryptoGuardRepository, strategy_name: str) -> list[dict[str, Any]]:
    """Return ALL shadow_testing versions for a strategy, newest first.

    Each entry is {version, status, candidate_patch}.
    Only returns 'shadow_testing' status — candidates still in 'candidate' status
    have not passed backtest gate and should not receive shadow evaluations.
    """
    candidates = []
    for version_info in repo.list_strategy_versions(strategy_name):
        if version_info.get("status") == "shadow_testing":
            version = str(version_info["version"])
            patch = _load_candidate_patch(repo, strategy_name, version)
            candidates.append({
                "version": version,
                "status": version_info.get("status"),
                "candidate_patch": patch,
            })
    return candidates


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


def _create_virtual_trade_for_candidate(
    repo: CryptoGuardRepository,
    ga_decision_id: int,
    candidate_version: str,
    candidate_patch: dict[str, Any],
    active_decision: dict[str, Any],
    shadow_decision: dict[str, Any],
    symbol: str,
) -> int | None:
    """Create a shadow_virtual_trade for a candidate's independent lifecycle.

    Uses the dedicated shadow_virtual_trades table — never touches paper_orders
    or paper_trades (which affect account statistics).

    Always creates when the candidate has a trade_plan or opportunity_watch,
    regardless of whether it matches the active decision. The candidate's
    virtual trade tracks its own entry/SL/TP/risk and is independently
    updated and closed.

    Returns virtual_trade_id or None.
    """
    try:
        candidate_decision = shadow_decision.get("decision", "")

        if candidate_decision != "trade_plan_available":
            return None

        trade_plan = shadow_decision.get("trade_plan") or active_decision.get("trade_plan", {})
        if isinstance(trade_plan, str):
            trade_plan = json.loads(trade_plan)
        if not trade_plan:
            return None

        # Apply candidate_patch trade_plan overrides if present
        patch_data = candidate_patch.get("patch", candidate_patch)
        entry_adjustment = patch_data.get("entry_price_adjustment")
        sl_adjustment = patch_data.get("stop_loss_adjustment")
        tp_adjustment = patch_data.get("take_profit_adjustment")

        if entry_adjustment is not None:
            base_entry = trade_plan.get("entry_price") or trade_plan.get("trigger_price") or 0
            trade_plan = dict(trade_plan)
            trade_plan["entry_price"] = float(base_entry) + float(entry_adjustment)
        if sl_adjustment is not None:
            base_sl = trade_plan.get("stop_loss", 0)
            trade_plan = dict(trade_plan)
            trade_plan["stop_loss"] = float(base_sl) + float(sl_adjustment)
        if tp_adjustment is not None:
            tps = trade_plan.get("take_profits", [])
            if tps:
                trade_plan = dict(trade_plan)
                adjusted_tps = []
                for tp in tps:
                    tp = dict(tp)
                    tp["price"] = float(tp.get("price", 0)) + float(tp_adjustment)
                    adjusted_tps.append(tp)
                trade_plan["take_profits"] = adjusted_tps

        side = trade_plan.get("side", "LONG")
        entry_price = trade_plan.get("entry_price") or trade_plan.get("trigger_price") or 0
        stop_loss = trade_plan.get("stop_loss", 0)
        if not entry_price or not stop_loss:
            return None

        entry = float(entry_price)
        stop = float(stop_loss)

        # Apply slippage FIRST, then size against the fill price (consistent R-basis)
        from plugins.crypto_guard.paper.paper_broker import compute_position_size, compute_fill_price

        fill_price = compute_fill_price(entry, side, order_type=trade_plan.get("entry_type", "market"))

        sizing = compute_position_size(
            entry_price=fill_price,
            stop_loss=stop,
            risk_percent=float(trade_plan.get("risk_percent") or 0.5),
        )
        if sizing is None:
            return None
        qty, initial_risk = sizing

        # Extract strategy_name from active_decision or shadow_decision
        strategy_name = str(active_decision.get("strategy_name") or shadow_decision.get("strategy_name") or "smc_pullback_long")

        vt_id = repo.create_shadow_virtual_trade(
            strategy_name=strategy_name,
            candidate_version=candidate_version,
            ga_decision_id=ga_decision_id,
            symbol=symbol,
            side=side,
            entry_price=fill_price,
            stop_loss=stop,
            initial_stop_loss=stop,
            take_profit_json=json.dumps(trade_plan.get("take_profits", []), ensure_ascii=False),
            quantity=qty,
            initial_risk_usdt=initial_risk,
            entry_type=trade_plan.get("entry_type", "market"),
            max_pending_minutes=DEFAULT_MAX_PENDING_MINUTES,
        )
        return vt_id
    except Exception:
        LOGGER = __import__("plugins.crypto_guard.logging_utils", fromlist=["get_logger"]).get_logger("crypto_guard.ga_master")
        LOGGER.exception("shadow_virtual_trade_create_failed: candidate=%s ga_decision=%s", candidate_version, ga_decision_id)
        return None


def _adjustment_matches_context(
    when: dict[str, Any],
    snapshot: dict[str, Any],
    active_decision: dict[str, Any],
) -> bool:
    """Check if a conditional adjustment's 'when' clause matches current context.

    Returns True if all specified conditions match (AND logic).
    An empty 'when' dict always matches.
    """
    if not when:
        return True

    # Check side match
    required_side = when.get("side")
    if required_side:
        actual_side = str(active_decision.get("trade_plan", {}).get("side") or "").upper()
        if actual_side != required_side.upper():
            return False

    # Check market_phase match — read from snapshot.modules.market_regime (NOT market_profile)
    required_phase = when.get("market_phase")
    if required_phase:
        modules = snapshot.get("modules") or {}
        market_regime = modules.get("market_regime") or {}
        actual_phase = market_regime.get("market_phase", "")
        if actual_phase != required_phase:
            return False

    # Check trend_stage match
    required_stage = when.get("trend_stage")
    if required_stage:
        actual_stage = active_decision.get("trend_stage", "")
        if actual_stage != required_stage:
            return False

    # Check entry_type match — read from trade_plan.entry_type (NOT top-level entry_type)
    required_entry = when.get("entry_type")
    if required_entry:
        trade_plan = active_decision.get("trade_plan", {})
        if isinstance(trade_plan, str):
            try:
                trade_plan = json.loads(trade_plan)
            except (json.JSONDecodeError, TypeError):
                trade_plan = {}
        actual_entry = (trade_plan or {}).get("entry_type", "")
        if actual_entry != required_entry:
            return False

    # Check pattern_type — always matches (used for documentation)
    # pattern_type is informational, not a runtime filter

    return True


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

    # 1. Apply score_adjustment(s) — support conditional {value, when} format
    score_adj = patch_data.get("score_adjustment") or patch_data.get("score_adjustments")
    if score_adj is not None:
        if isinstance(score_adj, (int, float)):
            shadow_score = max(0.0, min(1.0, shadow_score + float(score_adj)))
            evidence["score_adjustment"] = float(score_adj)
        elif isinstance(score_adj, dict):
            total_adj = 0.0
            for adj_name, adj_value in score_adj.items():
                if isinstance(adj_value, (int, float)):
                    # Legacy flat format: unconditional sum
                    total_adj += float(adj_value)
                elif isinstance(adj_value, dict) and "value" in adj_value:
                    # New conditional format: {value, when}
                    when = adj_value.get("when", {})
                    if _adjustment_matches_context(when, snapshot, active_decision):
                        total_adj += float(adj_value["value"])
                        evidence[f"adjustment_{adj_name}"] = {
                            "applied": True,
                            "value": adj_value["value"],
                            "when": when,
                        }
                    else:
                        evidence[f"adjustment_{adj_name}"] = {
                            "applied": False,
                            "reason": "context_mismatch",
                            "when": when,
                        }
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

    # 5. Merge candidate_patch trade_plan overrides into shadow_decision
    entry_adjustment = patch_data.get("entry_price_adjustment")
    sl_adjustment = patch_data.get("stop_loss_adjustment")
    tp_adjustment = patch_data.get("take_profit_adjustment")

    if any(x is not None for x in (entry_adjustment, sl_adjustment, tp_adjustment)):
        active_tp = active_decision.get("trade_plan", {})
        if isinstance(active_tp, str):
            try:
                active_tp = json.loads(active_tp)
            except (json.JSONDecodeError, TypeError):
                active_tp = {}
        if active_tp:
            candidate_tp = dict(active_tp)
            if entry_adjustment is not None:
                base_entry = candidate_tp.get("entry_price") or candidate_tp.get("trigger_price") or 0
                candidate_tp["entry_price"] = float(base_entry) + float(entry_adjustment)
            if sl_adjustment is not None:
                base_sl = candidate_tp.get("stop_loss", 0)
                candidate_tp["stop_loss"] = float(base_sl) + float(sl_adjustment)
            if tp_adjustment is not None:
                tps = candidate_tp.get("take_profits", [])
                if tps:
                    adjusted_tps = []
                    for tp in tps:
                        tp = dict(tp)
                        tp["price"] = float(tp.get("price", 0)) + float(tp_adjustment)
                        adjusted_tps.append(tp)
                    candidate_tp["take_profits"] = adjusted_tps
            shadow_decision_with_tp = dict(shadow_decision)
            shadow_decision_with_tp["trade_plan"] = candidate_tp
            return {
                "decision": shadow_decision,
                "score": shadow_score,
                "evidence": evidence,
                "trade_plan": candidate_tp,
            }

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

        # P0: 写入 shadow 评估 — 为所有 shadow_testing 候选积累样本（多候选不饥饿）
        strategy_name = legacy.get("strategy_name", "smc_pullback_long")
        ga_decision_id = saved.get("ga_decision_id")
        shadow_candidates = _find_shadow_candidates(self.repo, strategy_name)
        for sc in shadow_candidates:
            try:
                shadow_decision = _evaluate_shadow_candidate(
                    active_decision=legacy,
                    candidate_patch=sc["candidate_patch"],
                    snapshot=snapshot,
                )
                # Determine outcome_source and create virtual trade for each candidate
                virtual_trade_id = None
                candidate_dec = shadow_decision.get("decision", "")
                active_dec = legacy.get("decision", "")

                if candidate_dec in ("trade_plan_available", "opportunity_watch"):
                    # Candidate would enter — create virtual trade for independent tracking
                    virtual_trade_id = _create_virtual_trade_for_candidate(
                        self.repo,
                        ga_decision_id=ga_decision_id,
                        candidate_version=sc["version"],
                        candidate_patch=sc["candidate_patch"],
                        active_decision=legacy,
                        shadow_decision=shadow_decision,
                        symbol=symbol,
                    )
                    outcome_source = "executed_virtual_trade"
                elif candidate_dec == "monitor_only":
                    if active_dec in ("trade_plan_available", "opportunity_watch"):
                        outcome_source = "avoided_trade"
                    else:
                        outcome_source = "no_entry"
                else:
                    outcome_source = "invalidated"

                record_shadow_evaluation(
                    self.repo,
                    symbol=symbol,
                    timeframe=snapshot.get("timeframe", "1h"),
                    analysis_time_utc=int(context["analysis_time_utc"]),
                    strategy_name=strategy_name,
                    strategy_version=sc["version"],
                    score=shadow_decision.get("score", 0.0),
                    decision=candidate_dec,
                    evidence=shadow_decision.get("evidence", {}),
                    snapshot_id=context.get("snapshot_id"),
                    ga_decision_id=ga_decision_id,
                    outcome_source=outcome_source,
                    shadow_virtual_trade_id=virtual_trade_id,
                )
            except Exception:
                LOGGER.warning(
                    "shadow_evaluation_failed: ga_decision=%s candidate=%s",
                    ga_decision_id, sc.get("version"),
                    exc_info=True,
                )

        # Return a compatibility shape to existing callers, with GADecision attached.
        compat = legacy_decision_from_ga_decision(saved)
        compat["ga_decision"] = saved
        compat["ga_decision_id"] = saved["ga_decision_id"]
        compat["signal_id"] = saved.get("signal_id")
        compat["analysis_state_id"] = analysis_state_id
        compat["market_analysis_state"] = analysis_state
        compat["suggested_actions"] = feishu_actions
        return compat
