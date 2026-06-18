from __future__ import annotations

from typing import Any


def analyze_trend_stage(price_action: dict[str, Any], momentum: dict[str, Any], *, analysis_time_utc: int) -> dict[str, Any]:
    structure = price_action.get("market_structure", "unknown")
    quality = momentum.get("quality", "unknown")
    score = float(momentum.get("momentum_score", 50) or 50)

    if structure == "range":
        stage = "range"
        main_risk = "震荡区间内信号容易互相打架"
    elif structure == "transition":
        # Near breakout — treat as early trend if momentum confirms
        if quality in {"healthy", "building"}:
            stage = "early"
            main_risk = "接近突破位，等待确认回踩"
        else:
            stage = "transition"
            main_risk = "结构切换期，等待确认"
    elif quality in {"extended", "overheated", "exhausted"}:
        stage = "late"
        main_risk = "趋势末端或短线过热，追价风险高"
    elif price_action.get("last_event") in ("bullish_bos", "bearish_bos"):
        stage = "early"
        main_risk = "突破后需要确认回踩是否成立"
    elif structure in ("bullish", "bearish") and 42 < score < 78:
        stage = "middle"
        main_risk = "趋势延续中，注意失效位"
    else:
        stage = "transition"
        main_risk = "结构切换期，等待确认"

    return {
        "module": "trend_stage",
        "stage": stage,
        "trend_stage": stage,
        "structure": structure,
        "main_risk": main_risk,
        "confidence": 0.62 if stage != "transition" else 0.48,
        "analysis_time_utc": analysis_time_utc,
    }


def fuse_trend_stage(profiles: dict[str, Any], primary_stage: dict[str, Any], *, analysis_time_utc: int) -> dict[str, Any]:
    weights = {"4h": 0.35, "1h": 0.25, "15m": 0.20, "5m": 0.20}
    stage_scores: dict[str, float] = {}
    structure_scores: dict[str, float] = {}
    for timeframe, profile in profiles.items():
        weight = weights.get(timeframe, 1)
        stage = profile.get("trend_stage") or "transition"
        structure = profile.get("market_structure") or "unknown"
        stage_scores[stage] = stage_scores.get(stage, 0) + weight
        structure_scores[structure] = structure_scores.get(structure, 0) + weight

    dominant_stage = max(stage_scores.items(), key=lambda item: item[1])[0] if stage_scores else primary_stage.get("trend_stage", "transition")
    dominant_structure = max(structure_scores.items(), key=lambda item: item[1])[0] if structure_scores else primary_stage.get("structure", "unknown")
    primary = primary_stage.get("trend_stage", dominant_stage)

    high_tf_range = any((profiles.get(tf) or {}).get("trend_stage") == "range" or (profiles.get(tf) or {}).get("market_structure") == "range" for tf in ("4h",))
    has_breakout_signal = any(
        (profiles.get(tf) or {}).get("last_event", "").endswith(("bos", "choch"))
        or (profiles.get(tf) or {}).get("range_status") in ("breakout", "breakout_retest", "structure_shift")
        or (profiles.get(tf) or {}).get("market_structure") == "transition"
        for tf in ("4h", "1h", "15m")
    )
    if has_breakout_signal and dominant_stage in {"early", "transition", "range"}:
        fused = "early"
        policy = "allow_watch_not_chase"
        main_risk = "突破信号出现，等待回踩确认入场。"
    elif high_tf_range and dominant_stage == "range" and dominant_structure == "range":
        fused = "range"
        policy = "filter_trend_strategy"
        main_risk = "多周期偏震荡，趋势策略过滤。"
    elif dominant_stage == "range":
        fused = "range"
        policy = "filter_trend_strategy"
        main_risk = "多周期偏震荡，趋势策略过滤。"
    elif primary == "late" or dominant_stage == "late":
        fused = "late"
        policy = "downgrade_chasing_signal"
        main_risk = "高周期或当前周期偏末端，追单信号降级。"
    elif primary == "early" and dominant_structure in {"bullish", "bearish"}:
        fused = "early"
        policy = "allow_watch_not_chase"
        main_risk = "趋势早期，等待回踩或二次确认。"
    elif dominant_stage == "middle":
        fused = "middle"
        policy = "allow_if_risk_valid"
        main_risk = "趋势中段，按失效位控制风险。"
    else:
        fused = "transition"
        policy = "monitor_only"
        main_risk = "多周期结构切换，等待确认。"

    return {
        **primary_stage,
        "module": "trend_stage",
        "stage": fused,
        "trend_stage": fused,
        "primary_stage": primary,
        "multi_timeframe_stage": dominant_stage,
        "multi_timeframe_structure": dominant_structure,
        "stage_scores": stage_scores,
        "structure_scores": structure_scores,
        "intraday_weights": weights,
        "htf_confirmation": "closed_4h_1h_15m_only",
        "strategy_policy": policy,
        "main_risk": main_risk,
        "stage_change_event": None,
        "confidence": 0.68 if fused not in {"transition", "range"} else 0.54,
        "analysis_time_utc": analysis_time_utc,
    }
