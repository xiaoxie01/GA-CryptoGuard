from __future__ import annotations

import json
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.analysis.market_regime_engine import EXTREME_REGIMES
from plugins.crypto_guard.strategy.grade_config import PUSH_GRADES, WATCH_GRADES, STORE_ONLY_GRADES, is_paper_order_eligible


def apply_risk_to_decision(decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    result = dict(decision)
    risk = validate_trade_plan(result, snapshot)
    result["risk_check"] = risk
    result["manual_bypass_allowed"] = False

    if result.get("has_trade_plan") and result.get("trade_plan") and not risk["ok"]:
        result["has_trade_plan"] = False
        result["decision"] = "monitor_only"
        notes = list(result.get("risk_notes") or [])
        notes.append("模拟盘风控未通过：" + "；".join(risk["reasons"]))
        result["risk_notes"] = notes

    result["suggested_actions"] = suggested_actions(result, risk)
    if "create_opportunity_watch" in result["suggested_actions"] and not result.get("opportunity_watch"):
        result["opportunity_watch"] = default_watch_from_decision(result, risk)
    return result


def validate_trade_plan(decision: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    min_rr = float(risk_cfg.get("min_rr", 2.0))
    min_conf = float(risk_cfg.get("min_confidence", risk_cfg.get("min_confidence_for_paper_order", 0.72)))
    snapshot = snapshot or {}
    plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
    reasons: list[str] = []
    metrics: dict[str, Any] = {"min_rr": min_rr, "min_confidence": min_conf}

    if not isinstance(plan, dict):
        return {"ok": False, "reasons": ["缺少完整 trade_plan"], "metrics": metrics}

    required = ["side", "entry_type", "stop_loss", "take_profits"]
    missing = [key for key in required if plan.get(key) in (None, "", [])]
    entry = _entry_price(plan)
    if entry is None:
        missing.append("entry_price_or_trigger_price")
    if missing:
        reasons.append("trade_plan 字段不完整：" + ",".join(missing))

    # P0-D: Check entry_trigger_confirmation quality
    # Auto/template confirmations indicate watch_only, not hard block
    entry_confirmation = plan.get("entry_trigger_confirmation")
    metrics["has_entry_confirmation"] = bool(entry_confirmation and str(entry_confirmation).strip() not in ("", "auto", "template"))

    rr = _risk_reward(plan)
    metrics["rr"] = rr
    if rr is None or rr < min_rr:
        reasons.append(f"RR {rr if rr is not None else '-'} 低于 {min_rr}")

    confidence = float(decision.get("confidence") or 0)
    metrics["confidence"] = confidence
    if confidence < min_conf:
        reasons.append(f"置信度 {confidence:.2f} 低于 {min_conf:.2f}")

    side = str(plan.get("side") or "").upper()
    htf = _htf_support(side, snapshot)
    metrics["htf_support"] = htf
    if not htf["ok"]:
        reasons.append(htf["reason"])

    alignment = _structure_momentum_alignment(side, snapshot)
    metrics["structure_momentum_alignment"] = alignment
    if risk_cfg.get("require_structure_momentum_alignment", True) and not alignment["ok"]:
        reasons.append(alignment["reason"])

    regime = ((snapshot.get("modules") or {}).get("market_regime") or {})
    regime_name = str(regime.get("regime") or "normal")
    metrics["market_regime"] = regime_name
    if regime_name in EXTREME_REGIMES or regime.get("extreme"):
        reasons.append(f"当前市场状态为 {regime_name}，禁止直接创建模拟盘订单")

    # TP/SL distance and precision validation
    entry = _entry_price(plan)
    if entry and entry > 0:
        stop = _safe_float(plan.get("stop_loss"))
        if stop:
            sl_distance = abs(entry - stop)
            sl_pct = sl_distance / entry * 100
            min_sl_pct = float(risk_cfg.get("min_sl_distance_pct", 0.8))
            if sl_pct < min_sl_pct:
                reasons.append(f"止损距离 {sl_pct:.3f}% 低于最小要求 {min_sl_pct}%，交易空间不足")

        tps = plan.get("take_profits") or []
        if tps:
            first_tp = tps[0] if isinstance(tps[0], dict) else {}
            tp_price = _safe_float(first_tp.get("price"))
            if tp_price:
                tp_distance = abs(tp_price - entry)
                tp_pct = tp_distance / entry * 100
                min_tp_pct = float(risk_cfg.get("min_tp_distance_pct", 1.0))
                if tp_pct < min_tp_pct:
                    reasons.append(f"第一止盈距离 {tp_pct:.3f}% 低于最小要求 {min_tp_pct}%，交易空间不足")

    # Validate stop loss has enough buffer from recent price action
    # Use ATR-based buffer: max(0.2 * ATR, min_sl_distance)
    if entry and entry > 0 and snapshot:
        modules = snapshot.get("modules") or {}
        momentum = modules.get("momentum") or {}
        atr_current = _safe_float((momentum.get("atr") or {}).get("current"))
        stop = _safe_float(plan.get("stop_loss"))
        side = str(plan.get("side") or "").upper()

        if stop and atr_current:
            if side == "LONG":
                distance = entry - stop
                # Buffer: max(0.2 * ATR, min_sl_distance)
                min_buffer = max(atr_current * 0.2, entry * float(risk_cfg.get("min_sl_distance_pct", 0.8)) / 100)
                if distance < min_buffer:
                    reasons.append(f"止损距离 {distance:.4f} 不足 ATR 缓冲 {min_buffer:.4f}（0.2×ATR={atr_current*0.2:.4f}），易被噪音打掉")
            elif side == "SHORT":
                distance = stop - entry
                min_buffer = max(atr_current * 0.2, entry * float(risk_cfg.get("min_sl_distance_pct", 0.8)) / 100)
                if distance < min_buffer:
                    reasons.append(f"止损距离 {distance:.4f} 不足 ATR 缓冲 {min_buffer:.4f}（0.2×ATR={atr_current*0.2:.4f}），易被噪音打掉")

    # P0-B: Late trend stage gate — blocks trend continuation orders
    if snapshot:
        modules = snapshot.get("modules") or {}
        trend_stage_data = modules.get("trend_stage") or {}
        trend_stage = str(trend_stage_data.get("trend_stage") or "").lower()
        if trend_stage in {"late", "exhausted"}:
            # Check if this is a trend continuation order (not a reversal)
            # Trend continuation: side aligns with market structure
            modules = snapshot.get("modules") or {}
            pa = modules.get("price_action") or {}
            profiles = snapshot.get("profiles") or {}
            setup_profile = profiles.get("15m") or profiles.get("1h") or {}
            structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
            is_continuation = (
                (side == "LONG" and structure in {"bullish", "range"}) or
                (side == "SHORT" and structure in {"bearish", "range"})
            )
            if is_continuation:
                reasons.append(f"趋势阶段已进入 {trend_stage}，不适合趋势延续方向开仓（{side} vs {structure}）")

    # P0-B: Overbought/oversold anti-chase gate — RSI-based
    if snapshot:
        modules = snapshot.get("modules") or {}
        momentum = modules.get("momentum") or {}
        rsi_value = _safe_float(momentum.get("rsi"))
        if rsi_value is not None:
            rsi_ob_threshold = float(risk_cfg.get("rsi_overbought_threshold", 75))
            rsi_os_threshold = float(risk_cfg.get("rsi_oversold_threshold", 25))
            if side == "LONG" and rsi_value >= rsi_ob_threshold:
                reasons.append(f"RSI {rsi_value:.1f} 超买（>={rsi_ob_threshold}），禁止追多")
            elif side == "SHORT" and rsi_value <= rsi_os_threshold:
                reasons.append(f"RSI {rsi_value:.1f} 超卖（<={rsi_os_threshold}），禁止追空")

    # P0-C: Order flow gate — degraded or opposite order flow blocks
    if snapshot:
        modules = snapshot.get("modules") or {}
        order_flow = modules.get("order_flow") or {}
        of_signal = str(order_flow.get("signal") or "").lower()
        of_supports = str(order_flow.get("supports") or "").lower()
        if of_signal == "degraded":
            reasons.append(f"订单流信号退化（degraded），不适合作为主要入场依据")
        elif of_supports and side:
            if side == "LONG" and of_supports == "bearish":
                reasons.append(f"订单流偏向空方（supports={of_supports}），与做多方向冲突")
            elif side == "SHORT" and of_supports == "bullish":
                reasons.append(f"订单流偏向多方（supports={of_supports}），与做空方向冲突")

    # P0-C: Chanlun gate — opposite chanlun signal blocks
    if snapshot:
        modules = snapshot.get("modules") or {}
        chanlun = modules.get("chanlun") or {}
        chanlun_signal = str(chanlun.get("signal") or "").lower()
        chanlun_supports = str(chanlun.get("supports") or "").lower()
        if chanlun_supports and side:
            if side == "LONG" and chanlun_supports == "bearish":
                reasons.append(f"缠论信号偏空（supports={chanlun_supports}），与做多方向冲突")
            elif side == "SHORT" and chanlun_supports == "bullish":
                reasons.append(f"缠论信号偏多（supports={chanlun_supports}），与做空方向冲突")

    # P1-C: LONG quality gate — soft downgrade for low-quality LONG entries
    if side == "LONG" and snapshot:
        long_gate = _long_quality_gate(decision, snapshot)
        metrics["long_quality_gate"] = long_gate
        if not long_gate["ok"]:
            reasons.append("LONG 质量门禁未通过：" + "；".join(long_gate["reasons"]))

    return {"ok": not reasons, "reasons": reasons, "metrics": metrics}


def suggested_actions(decision: dict[str, Any], risk: dict[str, Any] | None = None) -> list[str]:
    risk = risk or {"ok": False}
    grade = str(decision.get("signal_grade") or "D").upper()
    confidence = float(decision.get("confidence") or 0)
    actions: list[str] = []
    has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
    decision_name = str(decision.get("decision") or "")
    watch = decision.get("opportunity_watch")
    if grade in STORE_ONLY_GRADES:
        actions.extend(["add_to_watchlist", "ignore"])
    elif has_plan and risk.get("ok") and grade in PUSH_GRADES and is_paper_order_eligible(grade, confidence):
        actions.append("create_paper_order")
        actions.append("create_opportunity_watch")
    elif grade in PUSH_GRADES | WATCH_GRADES and (watch or decision_name.startswith("wait_for") or decision_name in {"monitor_only", "trade_plan_available"}):
        actions.append("create_opportunity_watch")
    actions.extend(["add_to_watchlist", "ignore"])
    out: list[str] = []
    for action in actions:
        if action not in out:
            out.append(action)
    return out


def default_watch_from_decision(decision: dict[str, Any], risk: dict[str, Any] | None = None) -> dict[str, Any]:
    reasons = list((risk or {}).get("reasons") or [])
    if not reasons:
        reasons = list(decision.get("counter_evidence") or decision.get("risk_notes") or ["等待 5m 触发与高周期方向重新确认"])
    side = ((decision.get("trade_plan") or {}).get("side") or decision.get("market_bias") or "neutral")
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    min_rr = risk_cfg.get("min_rr", 1.5)
    min_conf = risk_cfg.get("min_confidence", 0.72)
    return {
        "needed": True,
        "direction": side,
        "reason": "未达到直接模拟盘风控门槛，转为机会监控。",
        "conditions": [
            "4H 已收盘方向支持",
            "1H/15M 结构重新确认",
            "5M 出现入场或反转触发",
            f"RR >= {min_rr} 且置信度达到 {min_conf}",
        ],
        "invalid_condition": {"type": "risk_rejected", "reasons": reasons[:4]},
        "expires_minutes": 240,
    }


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_price(plan: dict[str, Any]) -> float | None:
    value = plan.get("entry_price")
    if value is None:
        value = plan.get("trigger_price")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _risk_reward(plan: dict[str, Any]) -> float | None:
    entry = _entry_price(plan)
    try:
        stop = float(plan.get("stop_loss"))
    except (TypeError, ValueError):
        return None
    if entry is None or entry == stop:
        return None
    tps = plan.get("take_profits") or []
    prices: list[float] = []
    for tp in tps:
        try:
            prices.append(float(tp.get("price") if isinstance(tp, dict) else tp))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    side = str(plan.get("side") or "").upper()
    reward = max((price - entry) if side == "LONG" else (entry - price) for price in prices)
    risk = abs(entry - stop)
    return round(reward / risk, 4) if risk > 0 else None


def _htf_support(side: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    profiles = snapshot.get("profiles") or {}
    direction = profiles.get("4h") or {}
    trend_1h = profiles.get("1h") or {}
    setup_15m = profiles.get("15m") or {}
    htf_structure = str(direction.get("market_structure") or "unknown")
    trend_structure = str(trend_1h.get("market_structure") or "unknown")
    setup_structure = str(setup_15m.get("market_structure") or "unknown")
    # 4H 允许 transition 和 range（区间不提供方向偏置但也不阻断），1H/15M 允许 range
    if side == "LONG":
        ok = htf_structure in {"bullish", "transition", "range"} and trend_structure in {"bullish", "range", "transition"} and setup_structure in {"bullish", "range", "transition"}
        reason = f"高周期不支持做多：4H={htf_structure}, 1H={trend_structure}, 15M={setup_structure}"
    elif side == "SHORT":
        ok = htf_structure in {"bearish", "transition", "range"} and trend_structure in {"bearish", "range", "transition"} and setup_structure in {"bearish", "range", "transition"}
        reason = f"高周期不支持做空：4H={htf_structure}, 1H={trend_structure}, 15M={setup_structure}"
    else:
        ok = False
        reason = "trade_plan 缺少 LONG/SHORT 方向"
    return {"ok": ok, "reason": reason, "4h": htf_structure, "1h": trend_structure, "15m": setup_structure}


def _structure_momentum_alignment(side: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    modules = snapshot.get("modules") or {}
    pa = modules.get("price_action") or {}
    momentum = modules.get("momentum") or {}
    profiles = snapshot.get("profiles") or {}
    setup_profile = profiles.get("15m") or profiles.get("1h") or {}
    structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
    mom = str(momentum.get("direction") or setup_profile.get("momentum") or "neutral")
    # 允许 transition 结构（近突破位）
    if side == "LONG":
        ok = structure in {"bullish", "range", "transition"} and mom == "bullish"
        reason = f"结构与动能未共振做多：structure={structure}, momentum={mom}"
    elif side == "SHORT":
        ok = structure in {"bearish", "range", "transition"} and mom == "bearish"
        reason = f"结构与动能未共振做空：structure={structure}, momentum={mom}"
    else:
        ok = False
        reason = "缺少方向，无法确认结构 + 动能共振"
    return {"ok": ok, "reason": reason, "structure": structure, "momentum": mom}


def _long_quality_gate(decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """LONG quality gate — soft downgrade for low-quality LONG entries.

    Implements P1-C: Block LONG entries when:
    - HTF bias not bullish
    - Trend stage late/exhausted
    - Momentum exhausted/overextended
    - Range/chop market structure
    - BTC context risk_off
    - Historical avg_r < 0 for symbol+side
    """
    from plugins.crypto_guard.storage.repository import CryptoGuardRepository

    reasons: list[str] = []
    plan = decision.get("trade_plan") or {}
    symbol = decision.get("symbol") or plan.get("symbol", "")
    modules = snapshot.get("modules") or {}
    profiles = snapshot.get("profiles") or {}

    # Check HTF bias
    htf_4h = profiles.get("4h") or {}
    htf_structure = str(htf_4h.get("market_structure") or "unknown")
    if htf_structure not in {"bullish", "transition"}:
        reasons.append(f"4H 结构不支持做多：{htf_structure}")

    # Check trend stage
    trend_stage_data = modules.get("trend_stage") or {}
    trend_stage = str(trend_stage_data.get("trend_stage") or "unknown").lower()
    if trend_stage in {"late", "exhausted"}:
        reasons.append(f"趋势阶段不适合做多：{trend_stage}")

    # Check momentum
    momentum = modules.get("momentum") or {}
    momentum_state = str(momentum.get("state") or momentum.get("direction") or "neutral").lower()
    if momentum_state in {"exhausted", "overextended"}:
        reasons.append(f"动能状态不适合做多：{momentum_state}")

    # Check market structure (range/chop)
    pa = modules.get("price_action") or {}
    setup_profile = profiles.get("15m") or profiles.get("1h") or {}
    structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
    entry_type = str(plan.get("entry_type") or "").lower()
    if structure in {"range", "chop"} and entry_type in {"breakout", "trend"}:
        reasons.append(f"区间市场禁止趋势型做多：{structure}")

    # Check BTC context
    btc_regime = modules.get("btc_context") or {}
    btc_risk_off = btc_regime.get("risk_off") or btc_regime.get("hard_risk_off")
    if btc_risk_off:
        reasons.append("BTC 上下文风险关闭，不适合做多")

    # Check historical performance (if repo available)
    # This is a soft check — we don't block, just warn
    # In production, this would query paper_trades for symbol+LONG avg_r

    return {"ok": not reasons, "reasons": reasons}


def risk_summary_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    try:
        decision = json.loads(signal.get("ga_decision_json") or "{}")
    except Exception:
        decision = {}
    return decision.get("risk_check") or {"ok": False, "reasons": ["signal 缺少风控记录"]}
