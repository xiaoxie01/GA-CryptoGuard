from __future__ import annotations

from typing import Any


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def analyze_momentum(candles: list[dict[str, Any]], *, analysis_time_utc: int) -> dict[str, Any]:
    if len(candles) < 26:
        return {
            "module": "momentum",
            "direction": "neutral",
            "momentum_score": 0,
            "quality": "insufficient_data",
            "price_momentum": "unknown",
            "volume_confirmed": False,
            "atr_state": "unknown",
            "divergence": False,
            "risk": "样本不足",
            "confidence": 0.0,
            "analysis_time_utc": analysis_time_utc,
        }

    closes = [float(c["close"]) for c in candles]
    close = closes[-1]
    close_5 = closes[-6]
    close_14 = closes[-15]
    roc_5 = (close - close_5) / close_5 if close_5 else 0.0
    roc_14 = (close - close_14) / close_14 if close_14 else 0.0
    volumes = [float(c["volume"]) for c in candles[-15:-1]]
    avg_volume = sum(volumes) / len(volumes) if volumes else 0.0
    volume_ratio = float(candles[-1]["volume"]) / avg_volume if avg_volume else 0.0
    volume_impulse = avg_volume > 0 and volume_ratio > 1.25
    atr_now = _atr(candles[-15:], 14)
    atr_prev = _atr(candles[-30:-15], 14) if len(candles) >= 30 else atr_now
    atr_ratio = atr_now / atr_prev if atr_prev else 1.0
    atr_expanding = atr_ratio > 1.08
    rsi_values = _rsi_series(closes, 14)
    rsi = rsi_values[-1] if rsi_values else 50.0
    rsi_slope = (rsi_values[-1] - rsi_values[-4]) / 3 if len(rsi_values) >= 4 else 0.0
    macd_line, signal_line, histogram = _macd(closes)
    histogram_slope = histogram[-1] - histogram[-4] if len(histogram) >= 4 else 0.0
    body_strength = _body_strength(candles[-8:])
    pullback_strength = _pullback_strength(candles[-12:])
    divergence = _momentum_divergence(candles, rsi_values, histogram)

    raw_score = 50 + (roc_5 * 600) + (roc_14 * 300)
    raw_score += rsi_slope * 1.8
    raw_score += histogram_slope * 30
    raw_score += (body_strength - 0.5) * 16
    if volume_impulse:
        raw_score += 8 if roc_5 >= 0 else -8
    if atr_expanding:
        raw_score += 5 if abs(roc_5) > 0.002 else 0
    score = max(0, min(100, round(raw_score)))

    if score >= 58:
        direction = "bullish"
    elif score <= 42:
        direction = "bearish"
    else:
        direction = "neutral"

    overheated = (score >= 78 and rsi >= 70) or (score <= 22 and rsi <= 30)
    exhausted = divergence or (abs(roc_5) > 0.018 and abs(rsi_slope) < 0.2 and abs(histogram_slope) < 0.02)
    if overheated:
        quality = "overheated"
        risk = "短线动能过热，避免追价"
    elif exhausted:
        quality = "exhausted"
        risk = "价格推进但动能背离或衰竭"
    elif 60 <= score <= 77 or 23 <= score <= 40:
        quality = "healthy"
        risk = "动能延续但仍需结构确认"
    else:
        quality = "range"
        risk = "动能不足"

    return {
        "module": "momentum",
        "direction": direction,
        "momentum_score": score,
        "quality": quality,
        "price_momentum": "expanding" if abs(roc_5) > 0.003 else "flat",
        "volume_confirmed": volume_impulse,
        "atr_state": "expanding" if atr_expanding else "contracting" if atr_ratio < 0.92 else "normal",
        "rsi": rsi,
        "rsi_slope": rsi_slope,
        "macd": {"line": macd_line[-1], "signal": signal_line[-1], "histogram": histogram[-1], "histogram_slope": histogram_slope},
        "atr": {"current": atr_now, "previous": atr_prev, "ratio": atr_ratio},
        "volume_impulse": {"confirmed": volume_impulse, "ratio": volume_ratio},
        "body_strength": body_strength,
        "pullback_strength": pullback_strength,
        "roc_5": roc_5,
        "roc_14": roc_14,
        "divergence": divergence,
        "overheated": overheated,
        "exhausted": exhausted,
        "risk": risk,
        "confidence": min(0.82, abs(score - 50) / 50 + 0.35),
        "analysis_time_utc": analysis_time_utc,
    }


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) <= period:
        return []
    values: list[float] = []
    for end in range(period, len(closes)):
        gains = 0.0
        losses = 0.0
        sample = closes[end - period : end + 1]
        for idx in range(1, len(sample)):
            delta = sample[idx] - sample[idx - 1]
            if delta >= 0:
                gains += delta
            else:
                losses += abs(delta)
        if losses == 0:
            values.append(100.0)
        else:
            rs = gains / losses
            values.append(100 - (100 / (1 + rs)))
    return values


def _macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    line = [a - b for a, b in zip(ema12, ema26)]
    signal = _ema(line, 9)
    histogram = [a - b for a, b in zip(line, signal)]
    return line, signal, histogram


def _body_strength(candles: list[dict[str, Any]]) -> float:
    strengths = []
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        spread = high - low
        if spread <= 0:
            continue
        strengths.append(abs(float(candle["close"]) - float(candle["open"])) / spread)
    return sum(strengths) / len(strengths) if strengths else 0.0


def _pullback_strength(candles: list[dict[str, Any]]) -> float:
    if len(candles) < 3:
        return 0.0
    closes = [float(c["close"]) for c in candles]
    impulse = abs(closes[-1] - closes[0]) or 1.0
    adverse = max(closes) - min(closes)
    return max(0.0, min(1.0, adverse / impulse))


def _momentum_divergence(candles: list[dict[str, Any]], rsi_values: list[float], histogram: list[float]) -> bool:
    if len(candles) < 10 or len(rsi_values) < 6 or len(histogram) < 6:
        return False
    price_now = float(candles[-1]["close"])
    price_prev = float(candles[-6]["close"])
    rsi_now = rsi_values[-1]
    rsi_prev = rsi_values[-6]
    hist_now = histogram[-1]
    hist_prev = histogram[-6]
    bearish_div = price_now > price_prev and (rsi_now < rsi_prev or hist_now < hist_prev)
    bullish_div = price_now < price_prev and (rsi_now > rsi_prev or hist_now > hist_prev)
    return bearish_div or bullish_div
