from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.reasoning.decision_schema import no_edge_decision, validate_json
from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot


def _get_min_risk_distance(entry: float) -> float:
    """Get minimum risk distance based on entry price magnitude."""
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    min_sl_pct = float(risk_cfg.get("min_sl_distance_pct", 0.8)) / 100.0
    return entry * min_sl_pct


def _match_price_precision(price: float, reference: float) -> float:
    """Round price to match reference price's decimal precision."""
    ref_str = f"{reference:.10f}".rstrip("0")
    if "." in ref_str:
        decimals = len(ref_str.split(".")[1])
    else:
        decimals = 0
    return round(price, decimals)


def _build_trade_plan(snapshot: dict[str, Any], side: str) -> dict[str, Any] | None:
    pa = snapshot["modules"].get("price_action") or {}
    momentum = snapshot["modules"].get("momentum") or {}
    smc = snapshot["modules"].get("smc") or {}
    levels = pa.get("key_levels", {})
    support = levels.get("support") or []
    resistance = levels.get("resistance") or []
    invalid = pa.get("invalid_level")
    rng = pa.get("range") or {}
    atr = ((momentum.get("atr") or {}).get("current") or 0)

    # 1. Determine invalid_level (stop loss) first from structure
    if invalid is None:
        if side == "LONG":
            swing_lows = pa.get("swing_lows") or []
            if swing_lows:
                invalid = min(float(s["price"]) for s in swing_lows[-3:])
        else:
            swing_highs = pa.get("swing_highs") or []
            if swing_highs:
                invalid = max(float(s["price"]) for s in swing_highs[-3:])

    if invalid is None:
        if side == "LONG" and support:
            invalid = min(float(s) for s in support)
        elif side == "SHORT" and resistance:
            invalid = max(float(s) for s in resistance)

    if invalid is None:
        if side == "LONG":
            invalid = rng.get("low")
        else:
            invalid = rng.get("high")

    if invalid is None:
        mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
        if mid > 0:
            offset = mid * 0.015
            invalid = mid - offset if side == "LONG" else mid + offset

    if invalid is None:
        return None

    # 2. Determine entry price (single calculation point)
    entry = None
    entry_type = "limit"
    trigger_price = None

    if side == "LONG":
        entry = support[-1] if support else None
        if entry is None:
            fvg = (smc.get("fvg") or {}).get("range")
            if fvg and len(fvg) == 2:
                entry = (fvg[0] + fvg[1]) / 2
        if entry is None and momentum.get("quality") in ("healthy", "building"):
            last_high = rng.get("high")
            if last_high and last_high > invalid:
                entry = float(last_high)
                entry_type = "trigger"
                trigger_price = float(last_high)
        if entry is None:
            mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
            if mid > 0 and mid > invalid:
                entry = mid
    else:
        entry = resistance[-1] if resistance else None
        if entry is None:
            fvg = (smc.get("fvg") or {}).get("range")
            if fvg and len(fvg) == 2:
                entry = (fvg[0] + fvg[1]) / 2
        if entry is None and momentum.get("quality") in ("healthy", "building"):
            last_low = rng.get("low")
            if last_low and last_low < invalid:
                entry = float(last_low)
                entry_type = "trigger"
                trigger_price = float(last_low)
        if entry is None:
            mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
            if mid > 0 and mid < invalid:
                entry = mid

    if entry is None:
        return None

    # 3. Validate and adjust stop distance
    if side == "LONG":
        if invalid >= entry:
            invalid = entry - entry * 0.001
        risk = entry - invalid
        min_risk = max(_get_min_risk_distance(entry), atr * 0.2 if atr > 0 else 0)
        if risk < min_risk:
            # Try to find a wider swing low
            swing_lows = pa.get("swing_lows") or []
            candidates = [float(s["price"]) for s in swing_lows if float(s["price"]) < entry and entry - float(s["price"]) >= min_risk]
            if candidates:
                invalid = max(candidates)  # nearest valid swing low
            else:
                invalid = entry - min_risk
            risk = entry - invalid
    else:
        if invalid <= entry:
            invalid = entry + entry * 0.001
        risk = invalid - entry
        min_risk = max(_get_min_risk_distance(entry), atr * 0.2 if atr > 0 else 0)
        if risk < min_risk:
            swing_highs = pa.get("swing_highs") or []
            candidates = [float(s["price"]) for s in swing_highs if float(s["price"]) > entry and float(s["price"]) - entry >= min_risk]
            if candidates:
                invalid = min(candidates)
            else:
                invalid = entry + min_risk
            risk = invalid - entry

    # 4. Match precision and build plan
    entry = _match_price_precision(entry, entry)
    invalid = _match_price_precision(invalid, entry)

    if side == "LONG":
        return {
            "side": "LONG",
            "entry_type": entry_type,
            "entry_price": float(entry),
            "trigger_price": trigger_price,
            "stop_loss": float(invalid),
            "take_profits": [
                {"price": float(_match_price_precision(entry + risk * 1.5, entry)), "ratio": 0.5},
                {"price": float(_match_price_precision(entry + risk * 2.5, entry)), "ratio": 0.5},
            ],
            "risk_percent": 0.5,
            "invalid_condition": f"15m 收盘跌破 {invalid}",
            "reason": "结构偏多，等待回踩确认；仅用于模拟盘",
        }
    else:
        return {
            "side": "SHORT",
            "entry_type": entry_type,
            "entry_price": float(entry),
            "trigger_price": trigger_price,
            "stop_loss": float(invalid),
            "take_profits": [
                {"price": float(_match_price_precision(entry - risk * 1.5, entry)), "ratio": 0.5},
                {"price": float(_match_price_precision(entry - risk * 2.5, entry)), "ratio": 0.5},
            ],
            "risk_percent": 0.5,
            "invalid_condition": f"15m 收盘站回 {invalid}",
            "reason": "结构偏空，等待反抽确认；仅用于模拟盘",
        }


def run_ga_sop_decision(snapshot: dict[str, Any], *, score_adjustment: float = 0.0) -> dict[str, Any]:
    """Run deterministic SOP decision.

    Args:
        snapshot: Market state snapshot
        score_adjustment: Optional score adjustment for candidate evaluation
    """
    scoring = score_snapshot(snapshot, score_adjustment=score_adjustment)
    symbol = snapshot["symbol"]
    grade = scoring["signal_grade"]
    trend_stage = (snapshot["modules"].get("trend_stage") or {}).get("trend_stage", "unknown")
    bias = scoring["market_bias"]
    # Determine side: bias first, then momentum direction as fallback
    side = "LONG" if bias == "bullish" else "SHORT" if bias == "bearish" else None
    if side is None:
        momentum_dir = (snapshot["modules"].get("momentum") or {}).get("direction")
        if momentum_dir == "bullish" and scoring["score"] >= 0.72:
            side = "LONG"
            bias = "bullish"
        elif momentum_dir == "bearish" and scoring["score"] >= 0.72:
            side = "SHORT"
            bias = "bearish"
    # Generate trade plan for A/S grades when side is available
    # Grade threshold (A>=0.72) already incorporates risk assessment
    trade_plan = _build_trade_plan(snapshot, side) if side and scoring["score"] >= 0.72 else None

    if trade_plan:
        decision = "trade_plan_available"
        suggested = ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore"]
        watch = {"needed": True, "direction": trade_plan["side"], "reason": "若限价未成交，可继续观察回踩条件", "conditions": [trade_plan["invalid_condition"]], "invalid_condition": trade_plan["invalid_condition"], "expires_minutes": 240}
    elif scoring["score"] >= 0.65 and side:
        decision = "wait_for_pullback" if bias in ("bullish", "bearish") else "monitor_only"
        suggested = ["create_opportunity_watch", "add_to_watchlist", "ignore"]
        watch = {"needed": True, "direction": side, "reason": "方向有倾向但交易计划不完整，等待结构确认", "conditions": ["等待回踩或突破后重新确认", "5m/15m 动能与结构同向"], "invalid_condition": "结构反向突破", "expires_minutes": 240}
    elif scoring["score"] >= 0.50:
        decision = "monitor_only"
        suggested = ["add_to_watchlist", "ignore"]
        watch = None
    else:
        decision = "no_edge"
        suggested = ["add_to_watchlist", "ignore"]
        watch = None

    result = {
        "symbol": symbol,
        "decision": decision,
        "signal_grade": grade,
        "market_bias": bias if bias in ("bullish", "bearish", "neutral", "mixed") else "mixed",
        "trend_stage": trend_stage if trend_stage in ("early", "middle", "late", "range", "transition") else "unknown",
        "confidence": round(scoring["score"], 4),
        "summary": _summary(symbol, decision, grade, bias, trend_stage),
        "evidence": scoring.get("evidence", []),
        "counter_evidence": scoring.get("counter_evidence", []),
        "risk_notes": scoring.get("counter_evidence", []) + ["不构成实盘建议，仅用于模拟盘与策略研究。"],
        "has_trade_plan": bool(trade_plan),
        "trade_plan": trade_plan,
        "opportunity_watch": watch,
        "suggested_actions": suggested,
        "strategy_name": scoring["strategy_name"],
        "strategy_version": scoring["strategy_version"],
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
    }
    ok, err = validate_json("ga_decision.schema.json", result)
    if not ok:
        fallback = no_edge_decision(symbol, err or "unknown schema error")
        ok2, err2 = validate_json("ga_decision.schema.json", fallback)
        if not ok2:
            raise ValueError(f"no_edge fallback schema 校验失败: {err2}")
        return fallback
    return result


def _summary(symbol: str, decision: str, grade: str, bias: str, trend_stage: str) -> str:
    if decision == "trade_plan_available":
        return f"{symbol} 当前为 {grade} 级模拟盘候选，结构倾向 {bias}，趋势阶段 {trend_stage}。"
    if decision.startswith("wait_for"):
        return f"{symbol} 当前为 {grade} 级观察机会，方向有倾向但需要等待触发条件。"
    if decision == "monitor_only":
        return f"{symbol} 当前仅适合观察，暂不生成模拟盘计划。"
    return f"{symbol} 当前无明显优势，系统仅记录本次分析。"
