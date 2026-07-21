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
from plugins.crypto_guard.paper.sizing import compute_position_size
import json

from plugins.crypto_guard.logging_utils import get_logger

LOGGER = get_logger("crypto_guard.ga_master")


# 07-10 Phase E (design §9 / Fact 2): map the structured ``llm_terminal_reason``
# persisted by the retry wrapper / run_agent_sop_decision to a precise
# grade_adjustment ``code``. This replaces the pre-Phase-E generic
# ``llm_parse_failed`` blanket that mislabeled budget skips (where the LLM was
# never called) as "LLM 解析失败". Each terminal reason gets its own code so
# the report can render an exact Chinese label (hourly_report label branch).
_LLM_TERMINAL_REASON_TO_ADJ_CODE: dict[str, str] = {
    "llm_json_parse_failed": "llm_json_parse_failed",
    "llm_schema_validation_failed": "llm_schema_validation_failed",
    "symbol_timeout": "llm_symbol_timeout",
    "batch_deadline_skipped": "llm_batch_deadline_skipped",
    "breaker_skipped": "llm_breaker_skipped",
    "retry_budget_exhausted": "llm_retry_budget_exhausted",
    "prompt_budget_contract_violation": "llm_prompt_budget_violation",
    "schema_repaired": "llm_schema_repaired",
}
# Dedup set: every code that represents an LLM outcome so a single decision
# never gets duplicate grade_adjustment rows. Covers the new terminal-reason
# codes plus the legacy ``llm_disabled``.
_LLM_ADJ_DEDUP_CODES = frozenset(
    set(_LLM_TERMINAL_REASON_TO_ADJ_CODE.values())
    | {"llm_disabled", "llm_failure", "llm_parse_failed"}
)


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
        "SELECT patch_json FROM strategy_patches WHERE strategy_name=%s AND candidate_version=%s ORDER BY id DESC LIMIT 1",
        (strategy_name, candidate_version),
    ).fetchone()
    if row and row["patch_json"]:
        try:
            patch = row["patch_json"]
            if isinstance(patch, str):
                patch = json.loads(patch)
            return patch
        except Exception:
            pass
    return {}


def _build_virtual_trade_kwargs(
    candidate_patch: dict[str, Any],
    active_decision: dict[str, Any],
    shadow_decision: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the vt_kwargs dict for create_shadow_virtual_trade or create_shadow_evaluation_with_vt.

    Extracts and patches trade_plan from shadow/active decisions.
    Returns None if trade_plan is missing or prices are invalid.
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

        sizing = compute_position_size(
            entry_price=entry,
            stop_loss=stop,
            risk_percent=float(trade_plan.get("risk_percent") or 0.5),
        )
        if sizing is None:
            return None
        qty, initial_risk = sizing

        return {
            "symbol": "PLACEHOLDER_SYMBOL",
            "side": side,
            "entry_price": entry,
            "stop_loss": stop,
            "initial_stop_loss": stop,
            "take_profit_json": json.dumps(trade_plan.get("take_profits", []), ensure_ascii=False),
            "quantity": qty,
            "initial_risk_usdt": initial_risk,
            "entry_type": trade_plan.get("entry_type", "market"),
            "max_pending_minutes": DEFAULT_MAX_PENDING_MINUTES,
        }
    except Exception:
        return None


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
        vt_kwargs = _build_virtual_trade_kwargs(candidate_patch, active_decision, shadow_decision)
        if vt_kwargs is None:
            return None

        vt_kwargs["symbol"] = symbol

        # Extract strategy_name from active_decision or shadow_decision
        strategy_name = str(active_decision.get("strategy_name") or shadow_decision.get("strategy_name") or "smc_pullback_long")

        vt_id = repo.create_shadow_virtual_trade(
            strategy_name=strategy_name,
            candidate_version=candidate_version,
            ga_decision_id=ga_decision_id,
            **vt_kwargs,
        )
        return vt_id
    except Exception:
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
        # Phase B (07-07): per-batch circuit breaker cache. Keyed by batch_id,
        # each breaker lives for one batch. The controller creates the breaker
        # on first symbol of a batch and reuses it for subsequent symbols.
        self._breakers: dict[str, Any] = {}

    def analyze_symbol(
        self,
        request: GAAnalysisRequest,
        *,
        preset_llm_candidate: dict[str, Any] | None = None,
        preset_llm_attempt_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self.context_builder.build(request)
        snapshot = context["snapshot"]
        # 07-10 R5: when the fair coordinator has already produced the LLM
        # candidate, inject it into the context so ``run_agent_sop_decision``
        # consumes it instead of calling the provider a second time. The
        # sentinel ``_SENTINEL``-free contract: pass the candidate only when
        # the fair path actually supplied one (None is a real terminal
        # failure, so it must be carried through, not dropped).
        if preset_llm_candidate is not None or preset_llm_attempt_meta is not None:
            context["llm_preset_candidate"] = preset_llm_candidate
            context["llm_preset_attempt_meta"] = preset_llm_attempt_meta or {}

        # Phase D (07-05): strict previous-state lookup for analysis continuity.
        # The loose ``latest_analysis_state`` returns the latest row for the
        # symbol without filtering by analysis_time or batch_id, so it can
        # return a same-batch or future row in pathological cases. The
        # continuity builder requires a strictly-prior, cross-batch, same-
        # symbol row; everything else is audit-only.
        previous_row_strict = None
        try:
            previous_row_strict = self.repo.latest_analysis_state_for_continuity(
                str(snapshot.get("symbol") or request.symbol),
                analysis_time_utc=int(snapshot.get("analysis_time_utc") or 0),
                exclude_batch_id=request.batch_id,
            )
        except Exception:
            LOGGER.warning(
                "latest_analysis_state_for_continuity failed for %s; falling back to None",
                snapshot.get("symbol"),
                exc_info=True,
            )
        # Persist the strict row on the context for build_market_analysis_state
        # (which still reads the loose row from context["previous_analysis_state"]
        # for backward compat — the strict row is the audit-grade source).
        context["previous_analysis_state_strict"] = previous_row_strict

        # Attach a pre-decision continuity block (delta without current_decision
        # — trigger_progress uses snapshot-only heuristics). The LLM prompt
        # path reads this from _compact_snapshot. After the decision is
        # finalized we rebuild the block with the actual current_decision so
        # delta.trigger_progress and thesis_status reflect what the decision
        # actually concluded.
        try:
            from plugins.crypto_guard.reasoning.decision_context import (
                attach_analysis_continuity_to_snapshot,
            )
            attach_analysis_continuity_to_snapshot(
                snapshot,
                previous_row=previous_row_strict,
                current_batch_id=request.batch_id,
                current_decision=None,
            )
        except Exception:
            LOGGER.warning(
                "attach_analysis_continuity_to_snapshot (pre-decision) failed for %s",
                snapshot.get("symbol"),
                exc_info=True,
            )

        # Hourly Report Accuracy: previous grade + hysteresis + clampgate
        from plugins.crypto_guard.strategy.grade_config import (
            grade_with_hysteresis, clamp_grade, grade_delta, SA_MAX_COUNTER_EVIDENCE,
        )
        previous_grade = self.repo.previous_ga_decision_grade(
            snapshot.get("symbol", ""),
            exclude_batch_id=request.batch_id,
        ) if hasattr(self.repo, "previous_ga_decision_grade") else None
        context["previous_grade"] = previous_grade

        # Phase B (07-07): create or retrieve per-batch circuit breaker and
        # budgets. The breaker lives for one batch; the wall-clock budget
        # timer starts when the breaker is first created (batch start).
        # Both breaker and budgets are cached on self._breakers (keyed by
        # batch_id) so they persist across symbols within the same batch,
        # even when the controller is recreated per-job.
        from plugins.crypto_guard.reasoning.llm_breaker import (
            CircuitBreaker,
            BatchRetryBudget,
            BatchWallClockBudget,
        )
        from plugins.crypto_guard.config.loader import load_config
        llm_cfg = load_config().trading_mode.get("llm", {})
        breaker_cfg = llm_cfg.get("circuit_breaker", {})
        retry_cfg = llm_cfg.get("retry", {})
        batch_id = request.batch_id or ""
        batch_state = self._breakers.get(batch_id)
        if batch_state is None:
            breaker = CircuitBreaker(
                enabled=breaker_cfg.get("enabled", True),
                consecutive_threshold=breaker_cfg.get("consecutive_failures", 3),
                rate_threshold=breaker_cfg.get("rate_threshold", 0.5),
                rate_window=breaker_cfg.get("rate_window", 10),
                min_rate_samples=breaker_cfg.get("min_rate_samples", 5),
            )
            retry_budget = BatchRetryBudget(
                max_batch_retry_calls=retry_cfg.get("max_batch_retry_calls", 9),
            )
            wall_clock_budget = BatchWallClockBudget(
                budget_seconds=retry_cfg.get("batch_wall_clock_budget_seconds", 90),
            )
            breaker._wall_clock_budget = wall_clock_budget
            batch_state = {
                "breaker": breaker,
                "retry_budget": retry_budget,
                "wall_clock_budget": wall_clock_budget,
            }
            self._breakers[batch_id] = batch_state
        breaker = batch_state["breaker"]
        context["llm_breaker"] = breaker
        context["llm_retry_budget"] = batch_state["retry_budget"]
        context["llm_wall_clock_budget"] = batch_state["wall_clock_budget"]

        legacy = run_agent_sop_decision(snapshot, context=context)
        legacy["analysis_source"] = "ga_master_controller"
        legacy["batch_id"] = request.batch_id
        legacy["previous_grade"] = previous_grade if previous_grade else legacy.get("previous_grade")

        # Phase F (07-05): capture raw_signal_grade / raw_score BEFORE
        # risk_gate / hysteresis / clamp run. These are the pre-gate
        # deterministic conclusions and must persist for audit/report so
        # the hourly report can render "原始评分 X% · 执行等级 Y" with
        # the original signal strength even when hysteresis or clamp
        # later downgrade the grade. The LLM judge already set these on
        # the legacy dict via _normalize_llm_decision; capture them here
        # as a stable snapshot before any further mutation.
        raw_signal_grade_capture = str(legacy.get("raw_signal_grade") or legacy.get("signal_grade") or "D").upper()
        raw_score_capture = float(legacy.get("raw_score") if legacy.get("raw_score") is not None else legacy.get("confidence") or 0.0)
        legacy["raw_signal_grade"] = raw_signal_grade_capture
        legacy["raw_score"] = round(raw_score_capture, 4)
        # grade_adjustments records every post-gate downgrade with its
        # reason code. Populated as hysteresis/clamp/perf_gate run.
        grade_adjustments: list[dict[str, Any]] = []

        # P1-6 (07-05 final review): the post-decision continuity rebuild
        # previously ran here (before risk_gate / hysteresis / clamp /
        # performance_gate), so the persisted delta reflected a pre-gate
        # decision that could still be downgraded or watch-only'd by the
        # gates below. That contradicted the "finalized decision" claim
        # in the persisted block. The rebuild now happens AFTER all gates
        # complete (around line 746, just before persistence), reading
        # the final effective_signal_grade / has_trade_plan / decision
        # so the delta and trigger_progress reflect the actual persisted
        # state. The LLM prompt still receives a pre-decision continuity
        # block via the snapshot built earlier in build_market_state_snapshot.

        # P0-3 (Round 3): run risk_gate FIRST so emergency_down can use
        # the account_risk result (hard_risk_off / daily_loss_pause).
        risk = self.risk_gate.check(legacy, context)
        legacy["risk_check"] = risk

        # Grade hysteresis (research 10): apply against previous grade
        # P0-7: pass the actual signal_grade, not confidence score
        prev_for_hys = legacy.get("previous_grade") or previous_grade
        if prev_for_hys:
            # P0-3 (Round 3): compute emergency_down from risk gate result,
            # not from legacy (which never has hard_risk_off before risk gate runs).
            account_risk = risk.get("account_risk") or {}
            emergency_down = bool(account_risk.get("hard_risk_off") or account_risk.get("daily_loss_pause"))
            effective_grade, hys_reason = grade_with_hysteresis(
                legacy.get("signal_grade") or "D", prev_for_hys,
                emergency_down=emergency_down,
            )
            if effective_grade != legacy.get("signal_grade"):
                legacy["signal_grade"] = effective_grade
                notes = list(legacy.get("risk_notes") or [])
                if hys_reason:
                    notes.append(hys_reason)
                    # Phase F (07-05): record the downgrade for audit.
                    grade_adjustments.append({
                        "code": "hysteresis",
                        "stage": "grade_gate",
                        "from": str(prev_for_hys),
                        "to": str(effective_grade),
                        "detail": hys_reason,
                    })
                legacy["risk_notes"] = notes
        if legacy.get("has_trade_plan") and legacy.get("trade_plan") and not risk.get("ok"):
            legacy["has_trade_plan"] = False
            legacy["decision"] = "monitor_only"
            notes = list(legacy.get("risk_notes") or [])
            notes.append("GA Master 风控未通过：" + "；".join(risk.get("reasons") or []))
            legacy["risk_notes"] = notes

        # S/A clamp (research 11): cap grade when execution evidence is missing.
        counter_count = len(legacy.get("counter_evidence") or [])
        if hasattr(risk, "get"):
            pass  # risk is a dict
        # Determine HTF conflict for clamp (low-cost heuristic from snapshot)
        # P1-8: 4H range/transition/unknown also creates conflict unless independent_trend
        htf_conflict = False
        independent_trend = bool(((legacy.get("market_regime_gate") or {}).get("adjustments") or {}).get("regime_alignment") == "independent_trend")
        if legacy.get("trade_plan") and risk.get("ok", False):
            side = str((legacy.get("trade_plan") or {}).get("side") or "").upper()
            htf_structure = str(((snapshot.get("profiles") or {}).get("4h") or {}).get("market_structure") or "unknown").lower()
            # P1-8: Only bullish/bearish are non-conflicting. Everything else (range,
            # transition, unknown, empty) means the 4H has not confirmed direction.
            allowed_long = {"bullish"} if not independent_trend else {"bullish", "transition", "range", "unknown", ""}
            allowed_short = {"bearish"} if not independent_trend else {"bearish", "transition", "range", "unknown", ""}
            if side == "LONG" and htf_structure not in allowed_long:
                htf_conflict = True
            elif side == "SHORT" and htf_structure not in allowed_short:
                htf_conflict = True
        clamped_grade, clamp_reason = clamp_grade(
            legacy.get("signal_grade") or "D",
            has_trade_plan=bool(legacy.get("has_trade_plan") and legacy.get("trade_plan")),
            risk_ok=bool(risk.get("ok")),
            confidence=float(legacy.get("confidence") or 0),
            htf_conflict=htf_conflict,
            independent_trend=independent_trend,
            counter_evidence_count=counter_count,
        )
        if clamped_grade != legacy.get("signal_grade"):
            pre_clamp_grade = str(legacy.get("signal_grade") or "D")
            legacy["signal_grade"] = clamped_grade
            notes = list(legacy.get("risk_notes") or [])
            if clamp_reason:
                notes.append(clamp_reason)
                # Phase F (07-05): record the clamp downgrade for audit.
                grade_adjustments.append({
                    "code": "clamp_sa_evidence",
                    "stage": "grade_gate",
                    "from": pre_clamp_grade,
                    "to": str(clamped_grade),
                    "detail": clamp_reason,
                })
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
            # Phase F (07-05): record performance gate downgrade.
            grade_adjustments.append({
                "code": "performance_gate_watch_only",
                "stage": "performance_gate",
                "from": str(legacy.get("signal_grade") or "D"),
                "to": str(legacy.get("signal_grade") or "D"),
                "detail": "；".join(perf_gate.get("reasons") or []) or "performance gate watch-only",
            })
        elif perf_gate.get("performance_degraded"):
            # Update grade if degraded
            pre_perf_grade = str(legacy.get("signal_grade") or "D")
            legacy["signal_grade"] = perf_gate.get("effective_grade", signal_grade)
            notes = list(legacy.get("risk_notes") or [])
            notes.append(f"信号降级：{signal_grade}→{perf_gate.get('effective_grade')}")
            legacy["risk_notes"] = notes
            # Phase F (07-05): record performance gate grade downgrade.
            grade_adjustments.append({
                "code": "performance_gate_degraded",
                "stage": "performance_gate",
                "from": pre_perf_grade,
                "to": str(perf_gate.get("effective_grade") or pre_perf_grade),
                "detail": f"信号降级：{pre_perf_grade}→{perf_gate.get('effective_grade')}",
            })

        # Apply confidence adjustment if any
        if perf_gate.get("confidence_adjustment", 0) < 0:
            legacy["confidence"] = perf_gate.get("effective_confidence", confidence)

        # Phase D (07-07): executable grade caps per design §7.1 Step 4d/4e.
        # These run AFTER clamp_grade and performance_gate, BEFORE
        # effective_signal_grade is set. They cap the executable signal_grade
        # (not raw_signal_grade) when execution-gate evidence is missing.
        # Step 4d: no structured entry confirmation → max B
        _trade_plan = legacy.get("trade_plan")
        _has_structured_entry = (
            isinstance(_trade_plan, dict)
            and isinstance(_trade_plan.get("entry_trigger_confirmation"), dict)
        )
        if (
            not _has_structured_entry
            and str(legacy.get("signal_grade") or "D").upper() in {"S", "A"}
        ):
            _pre_cap = str(legacy.get("signal_grade") or "D").upper()
            legacy["signal_grade"] = "B"
            _notes = list(legacy.get("risk_notes") or [])
            _notes.append("无结构化入场确认，executable grade 降至 B。")
            legacy["risk_notes"] = _notes
            grade_adjustments.append({
                "code": "no_entry_confirmation_cap",
                "stage": "grade_gate",
                "from": _pre_cap,
                "to": "B",
                "detail": "trade_plan.entry_trigger_confirmation 缺失或非结构化对象",
            })
        # Step 4e: LLM failed → executable grade max B + plan_execution_state=unconfirmed
        _llm_status_cap = str(legacy.get("llm_status") or "").lower()
        if (
            _llm_status_cap == "failed"
            and str(legacy.get("signal_grade") or "D").upper() in {"S", "A"}
        ):
            _pre_cap = str(legacy.get("signal_grade") or "D").upper()
            legacy["signal_grade"] = "B"
            _notes = list(legacy.get("risk_notes") or [])
            _notes.append("LLM 失败，executable grade 降至 B，plan_execution_state=unconfirmed。")
            legacy["risk_notes"] = _notes
            grade_adjustments.append({
                "code": "llm_failed_executable_cap",
                "stage": "synthesis",
                "from": _pre_cap,
                "to": "B",
                "detail": "llm_status=failed 触发 fail-closed grade cap",
            })
            # Ensure plan_execution_state is unconfirmed (already set in
            # run_agent_sop_decision for the LLM-failed path; this is a
            # safety net in case an upstream override cleared it).
            if legacy.get("plan_execution_state") not in {"unconfirmed", "risk_rejected", "invalidated", "no_candidate"}:
                legacy["plan_execution_state"] = "unconfirmed"

        # Phase F (07-05): set effective_signal_grade / effective_execution_confidence
        # / grade_adjustments AFTER all gates (risk, hysteresis, clamp, perf) have
        # run. effective_signal_grade equals the canonical signal_grade (post-gate);
        # effective_execution_confidence equals the canonical confidence (post-gate).
        # raw_signal_grade / raw_score are the pre-gate deterministic conclusions
        # captured at the top of analyze_symbol and never mutate after that.
        legacy["effective_signal_grade"] = str(legacy.get("signal_grade") or "D").upper()
        legacy["effective_execution_confidence"] = round(float(legacy.get("confidence") or 0.0), 4)
        legacy["grade_adjustments"] = list(grade_adjustments)
        # If LLM failed/disabled, record a grade_adjustment entry so the report
        # can surface "LLM 失败" as a downgrade reason alongside hysteresis/clamp.
        # 07-10 Phase E (design §9 / Fact 2): replace the generic
        # ``llm_parse_failed`` code with the EXACT structured terminal reason
        # persisted by the retry wrapper / run_agent_sop_decision. A budget
        # skip (terminal_reason=symbol_timeout / batch_deadline_skipped) must
        # NOT render as "LLM 解析失败" - the LLM was never called, so claiming
        # a parse failure misleads the operator. The dedup set covers all
        # terminal-reason codes so a decision does not get duplicate rows.
        llm_status = str(legacy.get("llm_status") or "").lower()
        terminal_reason = str(legacy.get("llm_terminal_reason") or "")
        if llm_status == "failed":
            # Map the structured terminal reason to the grade_adjustment code.
            # When no terminal reason was recorded (legacy / unexpected path),
            # fall back to a precise category rather than the generic
            # ``llm_parse_failed`` blanket.
            adj_code = _LLM_TERMINAL_REASON_TO_ADJ_CODE.get(
                terminal_reason, "llm_failure"
            )
        elif llm_status == "disabled":
            adj_code = "llm_disabled"
        else:
            adj_code = ""
        if adj_code and not any(
            str(adj.get("code") or "") in _LLM_ADJ_DEDUP_CODES
            for adj in grade_adjustments
        ):
            grade_adjustments.append({
                "code": adj_code,
                "stage": "synthesis",
                "from": str(raw_signal_grade_capture),
                "to": str(legacy.get("signal_grade") or "D"),
                "detail": f"llm_status={llm_status}"
                          + (f"，terminal_reason={terminal_reason}" if terminal_reason else "")
                          + " 触发 fail-closed，候选计划保留为 candidate_trade_plan",
            })
            legacy["grade_adjustments"] = list(grade_adjustments)

        # Phase C (07-07): override plan_execution_state based on risk_gate /
        # continuity results per design §6.3. Only override the state label
        # (not the decision outcome) so risk gates are not weakened. The
        # fields are set initially in run_agent_sop_decision; here we refine
        # them after all gates have finalized has_trade_plan / plan_blockers /
        # candidate_trade_plan.
        _candidate = legacy.get("candidate_trade_plan")
        _has_candidate = isinstance(_candidate, dict) and bool(_candidate)
        _has_plan = bool(legacy.get("has_trade_plan") and legacy.get("trade_plan"))
        _blockers = legacy.get("plan_blockers") or []
        _continuity_invalidated = any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in _blockers
        )
        if _continuity_invalidated:
            legacy["plan_execution_state"] = "invalidated"
        elif (_has_candidate and not _has_plan and not risk.get("ok")
              and legacy.get("plan_origin") not in {"deterministic_fallback", "deterministic_sop"}):
            # Risk gate rejected a plan that was executable before risk but got
            # downgraded. Only applies when plan_origin is NOT deterministic_*
            # (LLM-failed candidates are already unconfirmed and must stay so;
            # overwriting them to risk_rejected would hide the LLM failure).
            legacy["plan_execution_state"] = "risk_rejected"
        elif not _has_plan and not _has_candidate:
            legacy["plan_execution_state"] = "no_candidate"
        # Otherwise: keep the state set by run_agent_sop_decision (confirmed
        # for LLM-success path, unconfirmed for LLM-failed path with a
        # candidate, etc.).

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
            batch_id=request.batch_id,
            previous_grade=legacy.get("previous_grade") or previous_grade,
            grade_delta_value=grade_delta(legacy.get("previous_grade") or previous_grade, legacy.get("signal_grade") or "D"),
        )
        # Deterministic consistency override of final_summary text (rendered_summary)
        # before persistence (P0 text/field consistency check).
        from plugins.crypto_guard.notify.report_consistency import (
            rewrite_inconsistent_summary,
            contains_forbidden_phrase,
            execution_eligible,
        )
        # Phase C (07-03): preserve the original LLM/template summary text in
        # raw_decision_json["raw_llm_summary"] BEFORE any canonical override
        # so the audit trail retains the LLM's wording without letting it
        # influence business consumers.
        _original_llm_summary = (
            ga_decision.get("final_summary")
            or ga_decision.get("summary")
            or ""
        )
        # R2-7 (07-03 final review P1): set raw_llm_summary at the TOP LEVEL
        # of ga_decision so create_ga_decision's ``json.dumps(decision)``
        # serializes it to ``raw_decision_json.raw_llm_summary``. The GA
        # decision adapter reads from this top-level path. The previous code
        # only wrote to ``raw_legacy_decision.raw_llm_summary`` (nested),
        # which adapter reads returned None for in production.
        ga_decision["raw_llm_summary"] = _original_llm_summary
        raw_json = ga_decision.get("raw_legacy_decision")
        if isinstance(raw_json, dict):
            # Also keep a copy in the nested raw_legacy_decision for
            # legacy_decision_from_ga_decision consumers that read from there.
            raw_json.setdefault("raw_llm_summary", _original_llm_summary)
        # Phase C (07-05): attach the MultiTimeframeFeaturePack to the
        # persisted decision so raw_decision_json preserves per-TF compact
        # modules (sample_count, data_as_of, bias, structure, momentum,
        # key_levels) for ALL 5 timeframes. Previously only the primary-TF
        # modules survived into raw_decision_json, hiding 1d/4h/1h/15m
        # structure from audit. The pack is bounded to 24 KiB by the
        # builder; raw candle arrays are never included.
        try:
            from plugins.crypto_guard.reasoning.decision_context import (
                build_multi_timeframe_feature_pack,
            )
            feature_pack = build_multi_timeframe_feature_pack(snapshot)
            ga_decision["multi_timeframe_feature_pack"] = feature_pack
            if isinstance(raw_json, dict):
                raw_json["multi_timeframe_feature_pack"] = feature_pack
        except Exception:
            LOGGER.warning(
                "multi_timeframe_feature_pack build failed for %s; omitting",
                snapshot.get("symbol"),
                exc_info=True,
            )
        # Phase D (07-05) + P1-6 (final review): attach the
        # analysis_continuity block (previous compact + delta with
        # trigger_progress) to the persisted decision. This rebuild runs
        # AFTER all gates (risk / hysteresis / clamp / performance_gate)
        # have finalized ``legacy`` — so delta.current_grade,
        # delta.has_trade_plan, delta.decision and trigger_progress
        # statuses reflect the actual persisted state, not a pre-gate
        # snapshot. The pre-decision block attached at line ~420 (with
        # current_decision=None) served only the LLM prompt; we always
        # rebuild here from the finalized legacy dict rather than reusing
        # that pre-decision block.
        try:
            from plugins.crypto_guard.reasoning.decision_context import (
                build_analysis_continuity,
            )
            continuity_block = build_analysis_continuity(
                snapshot,
                previous_row=context.get("previous_analysis_state_strict"),
                current_batch_id=request.batch_id,
                current_decision=legacy,
            )
            ga_decision["analysis_continuity"] = continuity_block
            if isinstance(raw_json, dict):
                raw_json["analysis_continuity"] = continuity_block
        except Exception:
            LOGGER.warning(
                "analysis_continuity persist attach failed for %s; omitting",
                snapshot.get("symbol"),
                exc_info=True,
            )
        # rewrite_inconsistent_summary still runs first as a secondary defense
        # (blacklist stripping + deterministic rendered summary when the gate
        # is not passed). Its output is then unified with the canonical
        # builder below.
        rendered = rewrite_inconsistent_summary(
            ga_decision.get("final_summary") or "", ga_decision,
        )
        ga_decision["rendered_summary"] = rendered
        # If LLM or template text still carried forbidden phrases, replace
        # final_summary itself so downstream (signals table, agent brief) does
        # not leak inconsistent wording.
        if contains_forbidden_phrase(ga_decision.get("final_summary") or "") and rendered != ga_decision.get("final_summary"):
            ga_decision["final_summary"] = rendered
            ga_decision["summary"] = rendered

        # Phase C (07-03): generate the canonical deterministic summary from
        # the final structured fields. ``final_summary`` and
        # ``rendered_summary`` are unified to this canonical text so every
        # downstream consumer (hourly report, signal policy, alert delivery,
        # report adapter, feishu action builder) reads the same semantic
        # summary. The original LLM text is preserved only in
        # ``raw_decision_json["raw_llm_summary"]`` for audit; it never enters
        # business decisions.
        from plugins.crypto_guard.reasoning.summary_builder import (
            build_canonical_market_summary,
        )
        canonical = build_canonical_market_summary(ga_decision)
        # R1-5 (07-03 final review): always set final_summary == summary ==
        # rendered_summary == canonical. The original LLM text is preserved
        # separately in raw_decision_json["raw_llm_summary"] (below). This
        # closes the gap where executable decisions kept the LLM's wording
        # in final_summary while rendered_summary used canonical, causing
        # drift between the two fields. Diagnostics now compare both
        # against the canonical recompute.
        ga_decision["final_summary"] = canonical
        ga_decision["summary"] = canonical
        ga_decision["rendered_summary"] = canonical
        # Ensure raw_llm_summary is preserved on the persisted dict itself so
        # create_ga_decision's json.dumps(decision) captures it at the TOP
        # LEVEL of raw_decision_json. R2-7: the adapter reads from
        # raw_decision_json["raw_llm_summary"] (top-level). Also mirror into
        # raw_legacy_decision for legacy consumers.
        if isinstance(raw_json, dict):
            raw_json["raw_llm_summary"] = _original_llm_summary
        ga_decision["raw_llm_summary"] = _original_llm_summary

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

                if candidate_dec == "trade_plan_available":
                    # Build VT kwargs from candidate decisions
                    vt_kwargs = _build_virtual_trade_kwargs(
                        sc["candidate_patch"], legacy, shadow_decision,
                    )
                    if vt_kwargs is not None:
                        vt_kwargs["symbol"] = symbol
                        # Use atomic eval+VT creation to avoid split-brain
                        try:
                            result = self.repo.create_shadow_evaluation_with_vt(
                                strategy_name=strategy_name,
                                strategy_version=sc["version"],
                                ga_decision_id=ga_decision_id,
                                symbol=symbol,
                                analysis_time=int(context["analysis_time_utc"]),
                                outcome_source="executed_virtual_trade",
                                vt_kwargs=vt_kwargs,
                                timeframe=snapshot.get("timeframe", "1h"),
                                score=shadow_decision.get("score", 0.0),
                                decision=candidate_dec,
                                evidence=shadow_decision.get("evidence", {}),
                                snapshot_id=context.get("snapshot_id"),
                            )
                            virtual_trade_id = result.get("vt_id")
                            # eval_id is already created atomically; skip record_shadow_evaluation below
                            continue
                        except Exception:
                            LOGGER.warning(
                                "shadow_evaluation_atomic_failed: ga_decision=%s candidate=%s",
                                ga_decision_id, sc.get("version"),
                                exc_info=True,
                            )
                            outcome_source = "invalidated"
                            virtual_trade_id = None
                    else:
                        outcome_source = "invalidated"  # VT kwargs build failed (bad price/risk)
                elif candidate_dec == "opportunity_watch":
                    virtual_trade_id = None
                    outcome_source = "opportunity_watch_recorded"
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

    def get_batch_llm_health(self, batch_id: str) -> dict[str, Any]:
        """Return the LLM health snapshot for a batch (for batch summary persistence).

        Called by run_ga_workers.py when finishing a batch to merge the breaker
        snapshot into analysis_batches.summary_json.

        07-10 Phase E (design §9 / PRD R6): the breaker snapshot only records
        physical-call outcomes. Budget skips (terminal_reason=symbol_timeout /
        batch_deadline_skipped), breaker skips (terminal_reason=breaker_skipped),
        policy skips, the expected-symbol count, and the first-attempt coverage
        ratio are NOT in the breaker. This method merges the breaker snapshot
        with per-decision aggregates read from the persisted ga_decisions rows
        (``raw_decision_json`` carries the §8 envelope at the top level). The
        per-decision counts are the source of truth for the report's coverage
        line; the breaker counts remain for the failure-rate / open-state
        diagnostics. Both are additive JSON fields in summary_json (design §8).
        """
        batch_state = self._breakers.get(batch_id)
        snapshot: dict[str, Any] = {}
        if batch_state is not None:
            breaker = batch_state.get("breaker")
            if breaker is not None:
                snapshot = dict(breaker.snapshot())
            # Add wall-clock budget remaining for observability
            wcb = batch_state.get("wall_clock_budget")
            if wcb is not None:
                snapshot["wall_clock_budget_ms_remaining"] = wcb.remaining_ms()

        # Phase E: augment with per-decision outcomes from persisted rows.
        per_decision = _aggregate_batch_llm_outcomes(self.repo, batch_id)
        snapshot.update(per_decision)
        return snapshot


# Terminal reasons that represent a budget skip (the symbol never completed a
# successful first attempt because its per-symbol / batch wall-clock budget ran
# out). These MUST render as budget skips, NOT as parse failures (Fact 2).
_LLM_BUDGET_SKIP_TERMINAL_REASONS = frozenset({
    "symbol_timeout",
    "batch_deadline_skipped",
})
# Terminal reasons that represent the breaker refusing to call the LLM.
_LLM_BREAKER_SKIP_TERMINAL_REASONS = frozenset({"breaker_skipped"})
# Terminal reasons that represent a successful schema-alias / unwrap repair.
_LLM_REPAIR_TERMINAL_REASONS = frozenset({"schema_repaired"})
# Terminal reasons that represent an upstream policy skip (LLM disabled /
# gated out before any provider call). These carry llm_provider_call_count=0
# but a truthy terminal reason, so the generic policy_skip condition
# (``pcc == 0 and not terminal``) would miss them. 07-10 Phase E reviewer
# P2-3: without this set, a fully-LLM-disabled batch renders 0/10 coverage
# with all skip buckets at 0 and only a bare degraded marker - the operator
# cannot tell why coverage is zero.
# 07-10 P1-2 (terminal review): ``single_flight_skipped`` and
# ``missing_snapshot`` are the fair coordinator's structured policy-skip
# reasons (a symbol already in flight cross-batch, or a snapshot absent).
# They carry ``llm_status="skipped"`` (NOT "failed") + ``pcc=0`` + a truthy
# terminal reason, so without this set they would fall into the unexplained
# residual (``expected - accounted``) - the exact silent-drop signature the
# coverage-low diagnostic exists to catch - even though they are LEGITIMATE
# upstream skips, not defects. Classify them as ``policy_skip`` so the
# coverage accounting stays exact (every non-attempted symbol in an allowed
# bucket) and the operator sees the skip count.
_LLM_POLICY_SKIP_TERMINAL_REASONS = frozenset({
    "llm_disabled",
    "single_flight_skipped",
    "missing_snapshot",
})


def _aggregate_batch_llm_outcomes(
    repo: CryptoGuardRepository, batch_id: str | None
) -> dict[str, Any]:
    """Aggregate per-decision LLM outcomes from persisted ga_decisions rows.

    Returns the Phase E (design §9 / PRD R6) batch counters derived from the
    ``raw_decision_json`` §8 envelope of each decision in the batch:

    - ``expected_symbols``: number of symbols the batch was scheduled to
      analyze (``analysis_batches.enabled_symbols``). Sourced from the batch
      row — NOT from the count of ``ga_decisions`` rows — so a symbol whose
      worker raised before persisting a decision is still counted in the
      denominator (otherwise a 2-of-3 batch would falsely read 2/2 = 100%).
      Legacy batches without an ``enabled_symbols`` column fall back to the
      decision-row count so historical readers never break.
    - ``llm_symbols_worker_failed``: enabled symbols that have NO persisted
      decision row — i.e. the worker raised before recording any outcome.
      These are the gap between ``expected_symbols`` and the count of
      enabled symbols covered by a decision row. They drive the degraded
      coverage marker when a symbol crashed silently.
    - ``llm_symbols_attempted``: decisions with ``llm_provider_call_count >= 1``
      (a physical provider call was actually made - the basis of first-attempt
      coverage).
    - ``llm_physical_provider_calls``: sum of ``llm_provider_call_count``.
    - ``llm_symbols_success``: decisions with ``llm_status == "ok"``.
    - ``llm_symbols_failed``: decisions with ``llm_status == "failed"``.
    - ``llm_budget_skip_count``: decisions whose terminal reason is a budget
      skip (never mislabeled as a parse failure - Fact 2).
    - ``llm_breaker_skip_count``: decisions whose terminal reason is a breaker
      skip.
    - ``llm_policy_skip_count``: decisions skipped by an upstream policy
      (llm disabled / not enabled) before any provider call - terminal reason
      absent AND provider_call_count == 0 AND llm_status != "failed".
    - ``llm_repair_count``: decisions whose terminal reason is a repair.
    - ``llm_first_attempt_coverage``: ``llm_symbols_attempted /
      expected_symbols`` rounded to 3 dp (0.0 when no decisions).
    - ``llm_coverage_degraded``: True when coverage < 1.0 and at least one
      decision exists (drives the degraded-coverage report state).
    - ``dominant_llm_fallback_reason``: the most frequent
      ``llm_fallback_reason`` among non-empty values (for the budget-skip
      banner).

    Returns an empty dict when ``batch_id`` is falsy or no decisions are found
    so the breaker-only snapshot degrades gracefully for legacy callers.
    """
    if not batch_id:
        return {}
    try:
        rows = repo.list_ga_decisions_for_batch(batch_id)
    except Exception:
        LOGGER.warning("get_batch_llm_health: failed to read decisions for %s", batch_id, exc_info=True)
        return {}
    if not rows:
        return {}

    # 07-10 R1-2: the expected denominator is the set of symbols the batch
    # was scheduled to analyze (``analysis_batches.enabled_symbols``), NOT the
    # count of ``ga_decisions`` rows. A symbol whose worker raised before it
    # could persist a decision has no row, so a row-count denominator would
    # hide the failure (2-of-3 would read 2/2 = 100%). Legacy batches without
    # the column fall back to the row count so historical readers never break.
    enabled_symbols: list[str] = []
    try:
        batch_row = repo.get_analysis_batch(batch_id)
        if batch_row is not None:
            enabled_symbols = list(batch_row.get("enabled_symbols") or [])
    except Exception:
        LOGGER.warning("get_batch_llm_health: failed to read batch row for %s", batch_id, exc_info=True)
        enabled_symbols = []

    # Symbols covered by a persisted decision row (any outcome). The gap
    # between ``enabled_symbols`` and this set is ``worker_failed`` - symbols
    # the worker crashed on before recording anything.
    covered_symbols: set[str] = set()

    expected = 0
    attempted = 0
    provider_calls = 0
    success = 0
    failed = 0
    budget_skip = 0
    breaker_skip = 0
    policy_skip = 0
    repair = 0
    retry_calls = 0
    fallback_reasons: dict[str, int] = {}

    for row in rows:
        # Track which enabled symbols have a decision row. A decision row may
        # carry its symbol in raw_decision_json (§8 envelope) or in the row's
        # ``symbol`` column; prefer the envelope, fall back to the column.
        row_symbol = row.get("symbol")
        raw = row.get("raw_decision_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if not row_symbol and isinstance(raw.get("symbol"), str):
            row_symbol = raw.get("symbol")
        if isinstance(row_symbol, str) and row_symbol:
            covered_symbols.add(row_symbol)

        if enabled_symbols:
            # Denominator comes from the batch row, counted once below.
            pass
        else:
            # Legacy batch (no enabled_symbols column): fall back to the row
            # count so the denominator is never zero for historical batches.
            expected += 1
        status = str(raw.get("llm_status") or "").lower()
        pcc = int(raw.get("llm_provider_call_count") or 0)
        terminal = str(raw.get("llm_terminal_reason") or "")
        attempt_count = int(raw.get("llm_attempt_count") or 0)
        fallback = str(raw.get("llm_fallback_reason") or "")

        provider_calls += pcc
        # 07-10 Phase E reviewer P2-1: ``llm_symbols_attempted`` is defined as
        # "decisions with llm_provider_call_count >= 1 (a physical provider call
        # was actually made)" - the basis of first-attempt coverage. The
        # ``attempt_count > 0`` branch used here would count the
        # ``prompt_budget_contract_violation`` path (llm_attempt_count=1 but
        # pcc=0, provider call suppressed) as attempted, inflating coverage
        # (10/10 instead of 9/10) and masking the coverage gap. Only a physical
        # call counts as an attempt.
        if pcc >= 1:
            attempted += 1
        if status == "ok":
            success += 1
        elif status == "failed":
            failed += 1

        if terminal in _LLM_REPAIR_TERMINAL_REASONS:
            repair += 1
        if terminal in _LLM_BUDGET_SKIP_TERMINAL_REASONS:
            budget_skip += 1
        if terminal in _LLM_BREAKER_SKIP_TERMINAL_REASONS:
            breaker_skip += 1
        # Policy skip: no provider call, not a recorded failure, AND the LLM
        # layer was disabled / gated out before any call. 07-10 Phase E
        # reviewer P2-3: the disabled path sets llm_terminal_reason="llm_disabled"
        # (truthy), so the generic ``not terminal`` check alone would miss it.
        # Explicitly count policy-skip terminal reasons so a fully-disabled
        # batch surfaces llm_policy_skip_count instead of an unexplained 0/10.
        if pcc == 0 and status != "failed":
            if terminal in _LLM_POLICY_SKIP_TERMINAL_REASONS or not terminal:
                policy_skip += 1

        # llm_attempt_count includes no-call skips; a decision that attempted
        # the LLM more than once consumed retries (attempt 2/3).
        if attempt_count > 1:
            retry_calls += attempt_count - 1

        if fallback:
            fallback_reasons[fallback] = fallback_reasons.get(fallback, 0) + 1

    # Resolve the expected denominator now that the loop is done. When the
    # batch row carries enabled_symbols, that list is authoritative; otherwise
    # the legacy row-count fallback (incremented in the loop) is used.
    if enabled_symbols:
        expected = len(enabled_symbols)
        worker_failed = len(set(enabled_symbols) - covered_symbols)
    else:
        worker_failed = 0

    coverage = round(attempted / expected, 3) if expected else 0.0
    dominant_reason = ""
    if fallback_reasons:
        dominant_reason = max(fallback_reasons, key=fallback_reasons.get)

    return {
        "expected_symbols": expected,
        "llm_symbols_attempted": attempted,
        "llm_physical_provider_calls": provider_calls,
        "llm_symbols_success": success,
        "llm_symbols_failed": failed,
        "llm_symbols_worker_failed": worker_failed,
        "llm_budget_skip_count": budget_skip,
        "llm_breaker_skip_count": breaker_skip,
        "llm_policy_skip_count": policy_skip,
        "llm_repair_count": repair,
        "llm_retry_calls": retry_calls,
        "llm_first_attempt_coverage": coverage,
        "llm_coverage_degraded": bool(expected and coverage < 1.0),
        "dominant_llm_fallback_reason": dominant_reason,
    }
