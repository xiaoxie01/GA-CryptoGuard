from __future__ import annotations

from typing import Any


EXTREME_REGIMES = {"extreme_volatility", "funding_shock", "news_like_event", "low_liquidity"}


def classify_market_regime(candles: list[dict[str, Any]], *, analysis_time_utc: int) -> dict[str, Any]:
    if len(candles) < 30:
        return {
            "module": "market_regime",
            "regime": "normal",
            "extreme": False,
            "atr_percentile": 0.0,
            "volume_ratio": 0.0,
            "range_ratio": 0.0,
            "reasons": ["样本不足，按 normal 保守处理，不触发极端行情进化。"],
            "analysis_time_utc": analysis_time_utc,
        }

    ranges = [float(c["high"]) - float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    recent_range = ranges[-1]
    sorted_ranges = sorted(ranges[-60:] if len(ranges) >= 60 else ranges)
    atr_percentile = _percentile_rank(sorted_ranges, recent_range)
    close = closes[-1] or 1.0
    range_ratio = recent_range / close
    avg_volume = sum(volumes[-30:-1]) / max(1, len(volumes[-30:-1]))
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 0.0
    wick_count = _recent_wick_count(candles[-5:])

    reasons: list[str] = []
    regime = "normal"
    if range_ratio >= 0.08 or (atr_percentile >= 0.95 and volume_ratio >= 2.5):
        regime = "news_like_event"
        reasons.append("单根波幅和成交量同时异常，按新闻/黑天鹅类事件处理。")
    elif atr_percentile >= 0.90 or range_ratio >= 0.045:
        regime = "extreme_volatility"
        reasons.append("ATR/单根波幅处于极端区间。")
    elif wick_count >= 3:
        regime = "low_liquidity"
        reasons.append("近期连续插针，可能存在流动性异常。")
    elif volume_ratio >= 2.2:
        regime = "high_volatility"
        reasons.append("成交量显著放大。")
    else:
        reasons.append("波动、成交量和插针行为未达到极端阈值。")

    return {
        "module": "market_regime",
        "regime": regime,
        "extreme": regime in EXTREME_REGIMES,
        "evolution_trigger_allowed": regime not in EXTREME_REGIMES,
        "atr_percentile": atr_percentile,
        "volume_ratio": volume_ratio,
        "range_ratio": range_ratio,
        "recent_wick_count": wick_count,
        "reasons": reasons,
        "analysis_time_utc": analysis_time_utc,
    }


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    if not sorted_values:
        return 0.0
    below = len([x for x in sorted_values if x <= value])
    return below / len(sorted_values)


def _recent_wick_count(candles: list[dict[str, Any]]) -> int:
    count = 0
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close = float(candle["close"])
        spread = high - low
        if spread <= 0:
            continue
        body = abs(close - open_price)
        wick = spread - body
        if wick / spread >= 0.72:
            count += 1
    return count
