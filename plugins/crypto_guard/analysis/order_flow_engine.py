from __future__ import annotations

from typing import Any


def analyze_order_flow(candles: list[dict[str, Any]] | None = None, *, analysis_time_utc: int, flow_data: dict[str, Any] | None = None) -> dict[str, Any]:
    if not flow_data:
        synthetic = _synthetic_from_candles(candles or [])
        if not synthetic:
            return {
                "module": "order_flow",
                "cvd_slope": "unknown",
                "aggressive_buy_ratio": None,
                "large_trade_bias": "unknown",
                "delta_divergence": None,
                "volume_impulse": None,
                "flow_confirmation": "not_available",
                "degraded": True,
                "reason": "无实时订单流数据，降级为结构化占位。",
                "confidence": 0.0,
                "analysis_time_utc": analysis_time_utc,
            }
        flow_data = synthetic

    cvd_values = [float(x) for x in flow_data.get("cvd_values", [])]
    aggressive_buy_ratio = flow_data.get("aggressive_buy_ratio")
    price_change = float(flow_data.get("price_change", 0.0))
    cvd_delta = cvd_values[-1] - cvd_values[0] if len(cvd_values) >= 2 else float(flow_data.get("cvd_delta", 0.0))
    cvd_slope = "up" if cvd_delta > 0 else "down" if cvd_delta < 0 else "flat"
    buy_ratio = float(aggressive_buy_ratio) if aggressive_buy_ratio is not None else 0.5
    if cvd_slope == "up" and buy_ratio >= 0.55:
        confirmation = "supports_long"
    elif cvd_slope == "down" and buy_ratio <= 0.45:
        confirmation = "supports_short"
    else:
        confirmation = "neutral"
    divergence = (price_change > 0 and cvd_delta < 0) or (price_change < 0 and cvd_delta > 0)
    return {
        "module": "order_flow",
        "cvd_slope": cvd_slope,
        "cvd_delta": cvd_delta,
        "aggressive_buy_ratio": buy_ratio,
        "large_trade_bias": "buy" if buy_ratio >= 0.6 else "sell" if buy_ratio <= 0.4 else "balanced",
        "delta_divergence": divergence,
        "volume_impulse": flow_data.get("volume_impulse"),
        "flow_confirmation": confirmation,
        "degraded": bool(flow_data.get("degraded", False)),
        "confidence": 0.58 if not flow_data.get("degraded") else 0.25,
        "analysis_time_utc": analysis_time_utc,
    }


def _synthetic_from_candles(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(candles) < 6:
        return None
    sample = candles[-6:]
    deltas = []
    cvd = [0.0]
    for candle in sample:
        body = float(candle["close"]) - float(candle["open"])
        signed_volume = float(candle["volume"]) if body >= 0 else -float(candle["volume"])
        deltas.append(signed_volume)
        cvd.append(cvd[-1] + signed_volume)
    buy_volume = sum(max(0.0, x) for x in deltas)
    total = sum(abs(x) for x in deltas) or 1.0
    avg_vol = sum(float(c["volume"]) for c in sample[:-1]) / max(1, len(sample) - 1)
    return {
        "cvd_values": cvd,
        "aggressive_buy_ratio": buy_volume / total,
        "price_change": float(sample[-1]["close"]) - float(sample[0]["close"]),
        "volume_impulse": avg_vol > 0 and float(sample[-1]["volume"]) > avg_vol * 1.3,
        "degraded": True,
    }
