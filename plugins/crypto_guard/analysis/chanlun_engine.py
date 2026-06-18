from __future__ import annotations

from typing import Any


def analyze_chanlun(candles: list[dict[str, Any]] | None = None, *, analysis_time_utc: int) -> dict[str, Any]:
    candles = candles or []
    if len(candles) < 12:
        return {
            "module": "chanlun",
            "implemented": True,
            "trend_direction": "unknown",
            "current_structure": "insufficient_data",
            "signal": None,
            "fractals": [],
            "strokes": [],
            "central_zone": None,
            "divergence_candidate": False,
            "confidence": 0.0,
            "analysis_time_utc": analysis_time_utc,
        }
    normalized = normalize_inclusion(candles)
    fractals = detect_fractals(normalized)
    strokes = detect_strokes(fractals)
    central_zone = detect_central_zone(strokes)
    current_direction = strokes[-1]["direction"] if strokes else "unknown"
    divergence = detect_divergence(strokes)
    signal = buy_sell_candidate(strokes, central_zone, divergence)
    return {
        "module": "chanlun",
        "implemented": True,
        "trend_direction": "up" if current_direction == "up" else "down" if current_direction == "down" else "unknown",
        "current_structure": f"bi_{current_direction}" if strokes else "fractals_only",
        "current_bi_direction": current_direction,
        "signal": signal,
        "fractals": fractals[-12:],
        "strokes": strokes[-8:],
        "central_zone": central_zone,
        "divergence_candidate": divergence,
        "evidence_role": "supporting_only",
        "confidence": 0.58 if strokes else 0.28,
        "analysis_time_utc": analysis_time_utc,
    }


def normalize_inclusion(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for candle in candles:
        item = dict(candle)
        high = float(item["high"])
        low = float(item["low"])
        if normalized:
            prev = normalized[-1]
            prev_high = float(prev["high"])
            prev_low = float(prev["low"])
            contains = high <= prev_high and low >= prev_low
            contained_by = high >= prev_high and low <= prev_low
            if contains or contained_by:
                direction_up = float(item["close"]) >= float(prev["close"])
                prev["high"] = max(prev_high, high) if direction_up else min(prev_high, high)
                prev["low"] = max(prev_low, low) if direction_up else min(prev_low, low)
                prev["close"] = item["close"]
                prev["close_time"] = item["close_time"]
                prev["included_count"] = int(prev.get("included_count", 1)) + 1
                continue
        item["included_count"] = 1
        normalized.append(item)
    return normalized


def detect_fractals(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fractals: list[dict[str, Any]] = []
    for idx in range(1, len(candles) - 1):
        prev = candles[idx - 1]
        cur = candles[idx]
        nxt = candles[idx + 1]
        if float(cur["high"]) > float(prev["high"]) and float(cur["high"]) > float(nxt["high"]):
            fractals.append({"type": "top", "price": float(cur["high"]), "time": cur["close_time"], "index": idx})
        if float(cur["low"]) < float(prev["low"]) and float(cur["low"]) < float(nxt["low"]):
            fractals.append({"type": "bottom", "price": float(cur["low"]), "time": cur["close_time"], "index": idx})
    return fractals


def detect_strokes(fractals: list[dict[str, Any]], min_gap: int = 3) -> list[dict[str, Any]]:
    strokes: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    for fractal in fractals:
        if last is None:
            last = fractal
            continue
        if fractal["type"] == last["type"]:
            if (fractal["type"] == "top" and fractal["price"] > last["price"]) or (fractal["type"] == "bottom" and fractal["price"] < last["price"]):
                last = fractal
            continue
        if int(fractal["index"]) - int(last["index"]) < min_gap:
            continue
        direction = "up" if last["type"] == "bottom" and fractal["type"] == "top" else "down"
        strokes.append(
            {
                "direction": direction,
                "start": last,
                "end": fractal,
                "high": max(float(last["price"]), float(fractal["price"])),
                "low": min(float(last["price"]), float(fractal["price"])),
                "strength": abs(float(fractal["price"]) - float(last["price"])),
            }
        )
        last = fractal
    return strokes


def detect_central_zone(strokes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(strokes) < 3:
        return None
    sample = strokes[-3:]
    zg = min(float(s["high"]) for s in sample)
    zd = max(float(s["low"]) for s in sample)
    if zd <= zg:
        return {"low": zd, "high": zg, "stroke_count": 3, "status": "active"}
    return None


def detect_divergence(strokes: list[dict[str, Any]]) -> bool:
    if len(strokes) < 3:
        return False
    same_direction = [s for s in strokes if s["direction"] == strokes[-1]["direction"]]
    if len(same_direction) < 2:
        return False
    return float(same_direction[-1]["strength"]) < float(same_direction[-2]["strength"]) * 0.75


def buy_sell_candidate(strokes: list[dict[str, Any]], central_zone: dict[str, Any] | None, divergence: bool) -> str | None:
    if not strokes:
        return None
    last = strokes[-1]
    if last["direction"] == "down" and divergence:
        return "class_1_buy_candidate"
    if central_zone and last["direction"] == "up" and float(last["end"]["price"]) > float(central_zone["high"]):
        return "class_3_buy_candidate"
    if central_zone and last["direction"] == "down" and float(last["end"]["price"]) >= float(central_zone["low"]):
        return "class_2_buy_candidate"
    return None
