from __future__ import annotations

from typing import Any


def analyze_price_action(candles: list[dict[str, Any]], *, analysis_time_utc: int) -> dict[str, Any]:
    if len(candles) < 8:
        return {
            "module": "price_action",
            "market_structure": "unknown",
            "swing_sequence": "insufficient_data",
            "last_event": "none",
            "range_status": "unknown",
            "key_levels": {"support": [], "resistance": []},
            "entry_context": "monitor_only",
            "invalid_level": None,
            "confidence": 0.0,
            "analysis_time_utc": analysis_time_utc,
        }

    swings_high, swings_low = detect_swings(candles)

    highs = swings_high[-5:]
    lows = swings_low[-5:]
    close = candles[-1]["close"]
    prev_close = candles[-2]["close"]
    resistance = [x["price"] for x in highs[-3:]]
    support = [x["price"] for x in lows[-3:]]
    swing_labels = label_swing_sequence(highs, lows)

    # Minimum 0.3% magnitude to classify as HH/HL/LH/LL — prevents marginal noise
    min_mag = 0.003
    higher_high = len(highs) >= 2 and highs[-1]["price"] > highs[-2]["price"] * (1 + min_mag)
    higher_low = len(lows) >= 2 and lows[-1]["price"] > lows[-2]["price"] * (1 + min_mag)
    lower_high = len(highs) >= 2 and highs[-1]["price"] < highs[-2]["price"] * (1 - min_mag)
    lower_low = len(lows) >= 2 and lows[-1]["price"] < lows[-2]["price"] * (1 - min_mag)
    range_width_pct = _range_width_pct(highs, lows, close)

    last_high = highs[-1]["price"] if highs else max(c["high"] for c in candles[-12:])
    last_low = lows[-1]["price"] if lows else min(c["low"] for c in candles[-12:])
    previous_high = highs[-2]["price"] if len(highs) >= 2 else last_high
    previous_low = lows[-2]["price"] if len(lows) >= 2 else last_low

    # Detect if we're near a breakout (close within 1.0% of range boundary)
    near_breakout_high = len(highs) >= 2 and close > previous_high * 0.990
    near_breakout_low = len(lows) >= 2 and close < previous_low * 1.010

    if higher_high and higher_low:
        structure = "bullish"
        swing_sequence = "HH_HL"
    elif lower_high and lower_low:
        structure = "bearish"
        swing_sequence = "LH_LL"
    elif range_width_pct < 0.012 and len(highs) >= 2 and len(lows) >= 2:
        # Very tight compression — still range but with tighter threshold (1.2%)
        structure = "range"
        swing_sequence = "range_compression"
    elif near_breakout_high or near_breakout_low:
        # Near range boundary — treat as transition, not pure range
        structure = "transition"
        swing_sequence = "near_breakout"
    else:
        structure = "range"
        swing_sequence = "mixed"
    fake_breakout_high = candles[-1]["high"] > previous_high and close < previous_high
    fake_breakout_low = candles[-1]["low"] < previous_low and close > previous_low
    retest_high = previous_high and candles[-1]["low"] <= previous_high <= candles[-1]["close"]
    retest_low = previous_low and candles[-1]["high"] >= previous_low >= candles[-1]["close"]

    if close > previous_high and prev_close <= previous_high:
        last_event = "bullish_bos"
        range_status = "breakout"
        entry_context = "wait_for_retest"
    elif close < previous_low and prev_close >= previous_low:
        last_event = "bearish_bos"
        range_status = "breakout"
        entry_context = "wait_for_retest"
    elif structure == "bearish" and close > previous_high:
        last_event = "bullish_choch"
        range_status = "structure_shift"
        entry_context = "wait_for_reclaim"
    elif structure == "bullish" and close < previous_low:
        last_event = "bearish_choch"
        range_status = "structure_shift"
        entry_context = "wait_for_reclaim"
    elif fake_breakout_high:
        last_event = "bull_trap_fake_breakout"
        range_status = "fake_breakout"
        entry_context = "avoid_chop"
    elif fake_breakout_low:
        last_event = "bear_trap_fake_breakout"
        range_status = "fake_breakout"
        entry_context = "avoid_chop"
    elif retest_high:
        last_event = "breakout_retest"
        range_status = "breakout_retest"
        entry_context = "wait_for_confirmation"
    elif retest_low:
        last_event = "breakdown_retest"
        range_status = "breakout_retest"
        entry_context = "wait_for_confirmation"
    elif close > last_high:
        last_event = "bullish_continuation"
        range_status = "above_range"
        entry_context = "avoid_chasing"
    elif close < last_low:
        last_event = "bearish_continuation"
        range_status = "below_range"
        entry_context = "avoid_chasing"
    else:
        last_event = "range_bound"
        range_status = "inside_range"
        entry_context = "monitor_only"

    confidence = 0.68 if structure in ("bullish", "bearish") else 0.55 if structure == "transition" else 0.45
    invalid_level = last_low if structure == "bullish" else last_high if structure == "bearish" else None
    return {
        "module": "price_action",
        "market_structure": structure,
        "swing_sequence": swing_sequence,
        "last_event": last_event,
        "range_status": range_status,
        "key_levels": {"support": support, "resistance": resistance},
        "entry_context": entry_context,
        "invalid_level": invalid_level,
        "swing_highs": highs,
        "swing_lows": lows,
        "swing_labels": swing_labels,
        "structure_events": _structure_events(last_event, previous_high, previous_low, close),
        "range": {"high": last_high, "low": last_low, "width_pct": range_width_pct},
        "explanation": explain_structure(last_event, structure, invalid_level),
        "confidence": confidence,
        "analysis_time_utc": analysis_time_utc,
    }


def detect_swings(candles: list[dict[str, Any]], window: int = 2) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    swings_high: list[dict[str, Any]] = []
    swings_low: list[dict[str, Any]] = []
    for i in range(window, len(candles) - window):
        sample = candles[i - window : i + window + 1]
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        if high >= max(float(c["high"]) for c in sample):
            swings_high.append({"time": candles[i]["close_time"], "price": high, "index": i})
        if low <= min(float(c["low"]) for c in sample):
            swings_low.append({"time": candles[i]["close_time"], "price": low, "index": i})
    return swings_high, swings_low


def label_swing_sequence(highs: list[dict[str, Any]], lows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for idx, high in enumerate(highs):
        label = "H" if idx == 0 else "HH" if high["price"] > highs[idx - 1]["price"] else "LH"
        labels.append({**high, "type": "swing_high", "label": label})
    for idx, low in enumerate(lows):
        label = "L" if idx == 0 else "HL" if low["price"] > lows[idx - 1]["price"] else "LL"
        labels.append({**low, "type": "swing_low", "label": label})
    return sorted(labels, key=lambda x: x.get("index", 0))[-10:]


def explain_structure(last_event: str, structure: str, invalid_level: Any) -> str:
    invalid = f"，失效位 {invalid_level}" if invalid_level is not None else ""
    mapping = {
        "bullish_bos": "收盘突破前高，出现偏多 BOS",
        "bearish_bos": "收盘跌破前低，出现偏空 BOS",
        "bullish_choch": "空头结构中向上突破，出现 bullish CHoCH",
        "bearish_choch": "多头结构中向下跌破，出现 bearish CHoCH",
        "breakout_retest": "突破位回踩中，等待确认",
        "breakdown_retest": "跌破位反抽中，等待确认",
        "bull_trap_fake_breakout": "上破后收回区间，疑似假突破",
        "bear_trap_fake_breakout": "下破后收回区间，疑似假跌破",
        "range_bound": "价格仍在区间内运行",
    }
    return f"{mapping.get(last_event, last_event)}；当前结构 {structure}{invalid}。"


def _range_width_pct(highs: list[dict[str, Any]], lows: list[dict[str, Any]], close: float) -> float:
    if not highs or not lows or not close:
        return 1.0
    return abs(float(highs[-1]["price"]) - float(lows[-1]["price"])) / float(close)


def _structure_events(last_event: str, previous_high: float, previous_low: float, close: float) -> list[dict[str, Any]]:
    event_type = "none"
    if "bos" in last_event:
        event_type = "BOS"
    elif "choch" in last_event:
        event_type = "CHoCH"
    elif "fake_breakout" in last_event:
        event_type = "fake_breakout"
    elif "retest" in last_event:
        event_type = "retest"
    return [{"event": last_event, "type": event_type, "reference_high": previous_high, "reference_low": previous_low, "close": close}]
