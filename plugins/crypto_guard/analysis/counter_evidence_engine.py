from __future__ import annotations

from typing import Any


def build_counter_evidence(modules: dict[str, Any]) -> dict[str, Any]:
    bullish: list[str] = []
    bearish: list[str] = []
    neutral: list[str] = []

    pa = modules.get("price_action") or {}
    momentum = modules.get("momentum") or {}
    trend = modules.get("trend_stage") or {}
    smc = modules.get("smc") or {}
    order_flow = modules.get("order_flow") or {}

    if pa.get("market_structure") == "bullish":
        bullish.append("价格结构为 HH/HL")
    elif pa.get("market_structure") == "bearish":
        bearish.append("价格结构为 LH/LL")
    else:
        neutral.append("价格结构偏震荡或样本不足")

    if momentum.get("direction") == "bullish":
        bullish.append("动能偏多")
    elif momentum.get("direction") == "bearish":
        bearish.append("动能偏空")
    else:
        neutral.append("动能不清晰")
    if momentum.get("divergence"):
        neutral.append("价格与动能出现背离")
    if momentum.get("quality") in {"overheated", "exhausted"}:
        neutral.append(f"动能质量为 {momentum.get('quality')}，追价风险上升")

    if smc.get("liquidity", {}).get("last_event") == "sell_side_liquidity_sweep":
        bullish.append("出现卖方流动性扫盘后回收")
    if smc.get("liquidity", {}).get("last_event") == "buy_side_liquidity_sweep":
        bearish.append("出现买方流动性扫盘后回落")
    if order_flow.get("delta_divergence"):
        neutral.append("价格与 CVD/主动成交出现背离")
    if order_flow.get("flow_confirmation") == "supports_long" and pa.get("market_structure") == "bearish":
        neutral.append("订单流偏多但价格结构偏空")
    if order_flow.get("flow_confirmation") == "supports_short" and pa.get("market_structure") == "bullish":
        neutral.append("订单流偏空但价格结构偏多")

    if trend.get("trend_stage") == "late":
        neutral.append("趋势阶段偏末端，追价风险高")
    if trend.get("trend_stage") == "range":
        neutral.append("高概率震荡，方向延续性不足")

    # Require at least 2 items on each side for "high" contradiction
    # Single-item disagreements (e.g. 4H bearish vs 1H bullish) are normal multi-TF divergence
    if len(bullish) >= 2 and len(bearish) >= 2:
        level = "high"
    elif bullish and bearish:
        level = "medium"
    elif len(neutral) >= 2 or (not bullish and not bearish):
        level = "medium"
    else:
        level = "low"
    return {
        "bullish_evidence": bullish,
        "bearish_evidence": bearish,
        "neutral_or_risk_evidence": neutral,
        "contradiction_level": level,
    }
