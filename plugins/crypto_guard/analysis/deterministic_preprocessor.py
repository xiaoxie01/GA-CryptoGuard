from __future__ import annotations

from typing import Any

from plugins.crypto_guard.analysis.chanlun_engine import analyze_chanlun
from plugins.crypto_guard.analysis.momentum_engine import analyze_momentum
from plugins.crypto_guard.analysis.order_flow_engine import analyze_order_flow
from plugins.crypto_guard.analysis.price_action_engine import analyze_price_action
from plugins.crypto_guard.analysis.smc_engine import analyze_smc
from plugins.crypto_guard.analysis.trend_stage_engine import analyze_trend_stage


GEOMETRY_AUTHORITY_FIELDS = [
    "swing_highs",
    "swing_lows",
    "structure_events",
    "fvg.range",
    "order_block.range",
    "chanlun.fractals",
    "chanlun.strokes",
    "chanlun.central_zone",
    "momentum.rsi",
    "momentum.macd",
    "order_flow.cvd_delta",
]


def run_deterministic_preprocessing(candles: list[dict[str, Any]], *, analysis_time_utc: int) -> dict[str, Any]:
    pa = analyze_price_action(candles, analysis_time_utc=analysis_time_utc)
    momentum = analyze_momentum(candles, analysis_time_utc=analysis_time_utc)
    trend = analyze_trend_stage(pa, momentum, analysis_time_utc=analysis_time_utc)
    smc = analyze_smc(candles, pa, analysis_time_utc=analysis_time_utc)
    order_flow = analyze_order_flow(candles, analysis_time_utc=analysis_time_utc)
    chanlun = analyze_chanlun(candles, analysis_time_utc=analysis_time_utc)
    modules = {
        "price_action": pa,
        "smc": smc,
        "order_flow": order_flow,
        "momentum": momentum,
        "trend_stage": trend,
        "chanlun": chanlun,
    }
    for result in modules.values():
        result["deterministic_preprocessing"] = True
        result["geometry_authority"] = True
    return {
        "modules": modules,
        "provenance": {
            "source": "local_deterministic_preprocessor",
            "llm_geometry_allowed": False,
            "geometry_authority": "calculation_engine",
            "logic_authority": "GA evidence synthesis",
            "geometry_authority_fields": GEOMETRY_AUTHORITY_FIELDS,
        },
    }
