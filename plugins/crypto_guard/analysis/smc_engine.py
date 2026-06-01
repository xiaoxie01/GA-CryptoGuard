from __future__ import annotations

from typing import Any


def analyze_smc(candles: list[dict[str, Any]], price_action: dict[str, Any], *, analysis_time_utc: int) -> dict[str, Any]:
    if len(candles) < 5:
        return {"module": "smc", "implemented": False, "confidence": 0.0, "analysis_time_utc": analysis_time_utc}
    cur = candles[-1]
    prior_high = max(float(c["high"]) for c in candles[-8:-1])
    prior_low = min(float(c["low"]) for c in candles[-8:-1])
    sweep_low = cur["low"] < prior_low and cur["close"] > prior_low
    sweep_high = cur["high"] > prior_high and cur["close"] < prior_high
    if sweep_low:
        last_event = "sell_side_liquidity_sweep"
        direction = "bullish"
    elif sweep_high:
        last_event = "buy_side_liquidity_sweep"
        direction = "bearish"
    else:
        last_event = "none"
        direction = price_action.get("market_structure", "neutral")
    fvg = _latest_fvg(candles)
    order_block = _latest_order_block(candles, direction)
    premium_discount = _premium_discount(cur, price_action, prior_high, prior_low)
    return {
        "module": "smc",
        "implemented": True,
        "liquidity": {
            "last_event": last_event,
            "reclaimed": bool(sweep_low or sweep_high),
            "sweep_level": prior_low if sweep_low else prior_high if sweep_high else None,
        },
        "structure_shift": {"choch": False, "bos": "bos" in str(price_action.get("last_event", "")), "direction": direction},
        "fvg": fvg,
        "order_block": order_block,
        "premium_discount": premium_discount,
        "setup": "liquidity_sweep" if sweep_low or sweep_high else "none",
        "evidence": _evidence(last_event, fvg, order_block, premium_discount),
        "confidence": 0.62 if sweep_low or sweep_high or fvg.get("exists") else 0.32,
        "analysis_time_utc": analysis_time_utc,
    }


def _latest_fvg(candles: list[dict[str, Any]]) -> dict[str, Any]:
    for idx in range(len(candles) - 1, 1, -1):
        left = candles[idx - 2]
        right = candles[idx]
        if float(left["high"]) < float(right["low"]):
            return {
                "exists": True,
                "direction": "bullish",
                "range": [float(left["high"]), float(right["low"])],
                "status": "unfilled",
                "time": right["close_time"],
            }
        if float(left["low"]) > float(right["high"]):
            return {
                "exists": True,
                "direction": "bearish",
                "range": [float(right["high"]), float(left["low"])],
                "status": "unfilled",
                "time": right["close_time"],
            }
    return {"exists": False, "direction": None, "range": None, "status": None}


def _latest_order_block(candles: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    bullish = direction == "bullish"
    for candle in reversed(candles[-12:-1]):
        open_price = float(candle["open"])
        close = float(candle["close"])
        is_down = close < open_price
        is_up = close > open_price
        if bullish and is_down:
            return {"exists": True, "direction": "bullish", "range": [float(candle["low"]), float(candle["high"])], "mitigated": False}
        if direction == "bearish" and is_up:
            return {"exists": True, "direction": "bearish", "range": [float(candle["low"]), float(candle["high"])], "mitigated": False}
    return {"exists": False, "direction": None, "range": None, "mitigated": None}


def _premium_discount(cur: dict[str, Any], price_action: dict[str, Any], prior_high: float, prior_low: float) -> str:
    pa_range = price_action.get("range") or {}
    high = float(pa_range.get("high") or prior_high)
    low = float(pa_range.get("low") or prior_low)
    mid = (high + low) / 2
    return "discount" if float(cur["close"]) <= mid else "premium"


def _evidence(last_event: str, fvg: dict[str, Any], order_block: dict[str, Any], premium_discount: str) -> list[str]:
    items: list[str] = []
    if last_event != "none":
        items.append(f"流动性事件：{last_event}")
    if fvg.get("exists"):
        items.append(f"存在 {fvg.get('direction')} FVG")
    if order_block.get("exists"):
        items.append(f"存在 {order_block.get('direction')} order block")
    items.append(f"当前位置处于 {premium_discount}")
    return items
