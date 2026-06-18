from __future__ import annotations

from typing import Any

from plugins.crypto_guard.strategy.grade_config import grade_from_score
from plugins.crypto_guard.strategy.strategy_loader import load_active_strategies


def _match_strategy(strategies: list[dict[str, Any]], pa: dict[str, Any], momentum: dict[str, Any], trend: dict[str, Any], smc: dict[str, Any]) -> dict[str, Any]:
    """Select the best strategy based on current market conditions."""
    if not strategies:
        return {"strategy_name": "deterministic_sop", "version": "1.0"}

    structure = pa.get("market_structure", "range")
    trend_stage = trend.get("trend_stage", "unknown")
    smc_setup = smc.get("setup", "none")
    range_status = pa.get("range_status", "")

    for s in strategies:
        name = s.get("strategy_name", "")
        # Range market with breakout signal -> breakout retest strategy
        if structure in ("range", "transition") and ("breakout" in name or "retest" in name):
            if range_status in ("breakout", "breakout_retest", "structure_shift", "near_breakout"):
                return s
        # Trending market with SMC setup -> pullback strategy
        if structure in ("bullish", "bearish") and smc_setup == "liquidity_sweep" and "pullback" in name:
            return s
        # Strong momentum -> continuation strategy
        if momentum.get("quality") == "healthy" and "continuation" in name:
            return s

    # Default: if range with SMC setup, prefer breakout strategy
    if structure in ("range", "transition"):
        for s in strategies:
            if "breakout" in s.get("strategy_name", ""):
                return s
    return strategies[0]


def score_snapshot(snapshot: dict[str, Any], *, score_adjustment: float = 0.0) -> dict[str, Any]:
    """Score a market snapshot.

    Args:
        snapshot: Market state snapshot
        score_adjustment: Optional adjustment to final score (used for candidate evaluation)
    """
    modules = snapshot.get("modules", {})
    pa = modules.get("price_action") or {}
    momentum = modules.get("momentum") or {}
    trend = modules.get("trend_stage") or {}
    smc = modules.get("smc") or {}
    order_flow = modules.get("order_flow") or {}
    chanlun = modules.get("chanlun") or {}
    counter = snapshot.get("counter_evidence", {})
    strategies = load_active_strategies()

    bias = "neutral"
    pa_struct = pa.get("market_structure")
    if pa_struct == "bullish" and momentum.get("direction") != "bearish":
        bias = "bullish"
    elif pa_struct == "bearish" and momentum.get("direction") != "bullish":
        bias = "bearish"
    elif pa_struct == "transition":
        # Near breakout — bias follows momentum direction
        if momentum.get("direction") == "bullish":
            bias = "bullish"
        elif momentum.get("direction") == "bearish":
            bias = "bearish"
        else:
            bias = "mixed"
    elif pa_struct not in ("range", "unknown"):
        bias = "mixed"

    contradiction = counter.get("contradiction_level", "medium")

    # 评分逻辑：基于市场状态的多因子评分
    # 基础分从 0.55 开始
    base = 0.55

    # 价格行为确认（最高 +0.20）
    pa_structure = pa.get("market_structure")
    last_event = pa.get("last_event", "")
    range_status = pa.get("range_status", "")
    if pa_structure in ("bullish", "bearish"):
        base += 0.15
        if last_event in ("bullish_bos", "bearish_bos", "bullish_choch", "bearish_choch"):
            base += 0.05
    elif pa_structure == "transition":
        # Near breakout — moderate bonus
        base += 0.08
        if last_event in ("bullish_bos", "bearish_bos", "bullish_choch", "bearish_choch"):
            base += 0.07
    elif range_status in ("breakout", "breakout_retest", "structure_shift"):
        # Range with breakout signal — small bonus
        base += 0.05

    # 动能确认（最高 +0.15）
    momentum_dir = momentum.get("direction")
    momentum_score = momentum.get("momentum_score", 50)
    if momentum_dir in ("bullish", "bearish"):
        base += 0.10
        if momentum.get("quality") == "healthy":
            base += 0.05

    # 趋势阶段确认（最高 +0.15）
    trend_stage = trend.get("trend_stage")
    trend_policy = trend.get("strategy_policy", "")
    if trend_stage in ("early", "middle"):
        base += 0.10
        if trend_policy == "allow_if_risk_valid":
            base += 0.05
    elif trend_stage == "transition":
        base += 0.05

    # SMC 确认（最高 +0.10）
    smc_liquidity = (smc.get("liquidity") or {}).get("last_event")
    smc_fvg = (smc.get("fvg") or {}).get("exists")
    smc_setup = smc.get("setup", "none")
    if smc_liquidity in {"sell_side_liquidity_sweep", "buy_side_liquidity_sweep"}:
        base += 0.06
    if smc_fvg:
        base += 0.04

    # 订单流确认（最高 +0.10）
    flow_confirmation = order_flow.get("flow_confirmation")
    if flow_confirmation in ("supports_long", "supports_short"):
        if (bias == "bullish" and flow_confirmation == "supports_long") or (bias == "bearish" and flow_confirmation == "supports_short"):
            base += 0.08
        else:
            base -= 0.05

    # 缠论确认（最高 +0.05）
    chanlun_signal = chanlun.get("signal")
    if chanlun_signal in {"class_1_buy_candidate", "class_2_buy_candidate", "class_3_buy_candidate"}:
        if bias == "bullish":
            base += 0.05
    elif chanlun_signal in {"class_1_sell_candidate", "class_2_sell_candidate", "class_3_sell_candidate"}:
        if bias == "bearish":
            base += 0.05

    # 反向证据调整
    if contradiction == "low":
        base += 0.08
    elif contradiction == "high":
        base -= 0.15

    # 趋势末端惩罚
    if trend_stage == "late":
        base -= 0.08

    # 震荡区间惩罚（减小幅度，range 不再一刀切）
    if trend_stage == "range" and pa_structure == "range":
        # Both trend and structure are pure range — moderate penalty
        base -= 0.05
    elif trend_stage == "range" or pa_structure == "range":
        # Only one dimension is range — light penalty
        base -= 0.03

    # SMC setup 在 range 中的加分（breakout retest 是 range 内有效策略）
    if pa_structure in ("range", "transition") and smc_setup in {"liquidity_sweep", "fvg_fill"}:
        base += 0.06

    # 确保分数在合理范围内（含 candidate 调整）
    score = max(0.0, min(0.95, base + score_adjustment))

    strategy = _match_strategy(strategies, pa, momentum, trend, smc)
    counter_evidence = counter.get("neutral_or_risk_evidence", []) + ([] if contradiction != "high" else ["多空证据冲突较高"])
    if not counter_evidence:
        counter_evidence = ["未发现明确反向证据，但仍需等待价格确认。"]

    return {
        "strategy_name": strategy.get("strategy_name", "deterministic_sop"),
        "strategy_version": str(strategy.get("version", "1.0")),
        "score": score,
        "signal_grade": grade_from_score(score),
        "market_bias": bias,
        "risk_filters_passed": contradiction != "high" and trend.get("trend_stage") not in ("late",),
        "evidence": counter.get("bullish_evidence" if bias == "bullish" else "bearish_evidence", []) + list(smc.get("evidence", [])) + ([] if order_flow.get("flow_confirmation") in {None, "not_available", "neutral"} else [f"订单流确认：{order_flow.get('flow_confirmation')}"]) + ([] if not chanlun.get("signal") else [f"缠论候选：{chanlun.get('signal')}"]),
        "counter_evidence": counter_evidence,
    }
