from __future__ import annotations

from typing import Any

from plugins.crypto_guard.analysis.counter_evidence_engine import build_counter_evidence
from plugins.crypto_guard.analysis.market_regime_engine import classify_market_regime
from plugins.crypto_guard.analysis.trend_stage_engine import fuse_trend_stage
from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.skills.runner import execute_market_skills
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


DEFAULT_TIMEFRAMES = ["4h", "1h", "15m", "5m"]


def _analyze_timeframe(repo: CryptoGuardRepository, symbol: str, timeframe: str, analysis_time_utc: int, previous_analysis_state: dict[str, Any] | None) -> dict[str, Any]:
    candles = repo.get_candles(symbol, timeframe, analysis_time_utc=analysis_time_utc, limit=120)
    modules = execute_market_skills(
        repo,
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        analysis_time_utc=analysis_time_utc,
        previous_analysis_state=previous_analysis_state,
    )
    for name, result in modules.items():
        repo.save_module_result(symbol, timeframe, analysis_time_utc, name, result, result.get("confidence"))
    return {"timeframe": timeframe, "candles_count": len(candles), "modules": modules, "preprocessing": _preprocessing_provenance(), "candles": candles}


def build_market_state_snapshot(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    analysis_time_utc: int,
    mode: str,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    tfs = timeframes or DEFAULT_TIMEFRAMES
    profiles: dict[str, Any] = {}
    primary_modules: dict[str, Any] = {}
    previous_analysis_state = repo.latest_analysis_state(symbol)
    for tf in tfs:
        result = _analyze_timeframe(repo, symbol, tf, analysis_time_utc, previous_analysis_state)
        profiles[tf] = {
            "candles_count": result["candles_count"],
            "trend_stage": result["modules"]["trend_stage"].get("trend_stage"),
            "market_structure": result["modules"]["price_action"].get("market_structure"),
            "momentum": result["modules"]["momentum"].get("direction"),
            "role": _timeframe_role(tf),
            "weight": _timeframe_weight(tf),
        }
        if tf == "5m" or (tf == "15m" and not primary_modules) or not primary_modules:
            primary_modules = result["modules"]
            primary_candles = result.get("candles") or []
    primary_candles = locals().get("primary_candles", [])
    fused_trend = fuse_trend_stage(profiles, primary_modules.get("trend_stage") or {}, analysis_time_utc=analysis_time_utc)
    market_regime = classify_market_regime(primary_candles, analysis_time_utc=analysis_time_utc)
    previous_stage = _previous_trend_stage(repo, symbol, analysis_time_utc)
    if previous_stage and previous_stage != fused_trend.get("trend_stage"):
        fused_trend["stage_change_event"] = {
            "from": previous_stage,
            "to": fused_trend.get("trend_stage"),
            "notify_feishu": True,
            "analysis_time_utc": int(analysis_time_utc),
        }
    primary_modules["trend_stage"] = fused_trend
    primary_modules["market_regime"] = market_regime
    repo.save_module_result(symbol, "multi", analysis_time_utc, "trend_stage_fusion", fused_trend, fused_trend.get("confidence"))
    repo.save_module_result(symbol, "multi", analysis_time_utc, "market_regime", market_regime, 0.7)
    snapshot = {
        "symbol": symbol,
        "analysis_time_utc": int(analysis_time_utc),
        "mode": mode,
        "profiles": profiles,
        "modules": primary_modules,
        "counter_evidence": build_counter_evidence(primary_modules),
        "data_quality": _data_quality(profiles, analysis_time_utc),
        "paper_context": {},
        "previous_analysis_state": (previous_analysis_state or {}).get("state") if previous_analysis_state else None,
        "active_opportunity_watches": repo.list_active_opportunity_watches_for_symbol(symbol),
        "open_paper_orders": repo.list_open_paper_orders_for_symbol(symbol),
        "intraday_framework": {
            "mode": "intraday",
            "background": ["1d", "4h"],
            "direction": "4h",
            "trend": ["1h", "15m"],
            "entry": ["15m", "5m"],
            "weights": {"daily": 0.10, "4h": 0.35, "1h": 0.30, "15m": 0.25},
            "default_intraday_weights": {"4h": 0.35, "1h": 0.30, "15m": 0.25, "5m": 0.10},
            "rule": "顺大逆小：顺 4H/1H 已收盘方向，只在 15M/5M 寻找回调反转触发；5M 只用于数据获取，不用于分析决策。",
        },
        "preprocessing_policy": {
            "llm_geometry_allowed": False,
            "geometry_conflict_resolution": "calculation_engine_wins",
            "logic_resolution": "GA synthesizes deterministic evidence",
        },
        "global_context": {"time_policy": "UTC; closed candles only; HTF confirmation uses last closed 4h/1h/15m candles"},
    }
    ok, err = validate_json("market_state_snapshot.schema.json", snapshot)
    if not ok:
        raise ValueError(f"MarketStateSnapshot schema 校验失败: {err}")
    return snapshot


def _timeframe_role(timeframe: str) -> str:
    return {"1d": "background_filter", "4h": "direction_filter", "1h": "trend_context", "15m": "setup_context", "5m": "entry_trigger", "1m": "micro_trigger"}.get(timeframe, "context")


def _timeframe_weight(timeframe: str) -> float:
    return {"1d": 0.10, "4h": 0.30, "1h": 0.25, "15m": 0.20, "5m": 0.15, "1m": 0.0}.get(timeframe, 0.0)


def _preprocessing_provenance() -> dict[str, Any]:
    return {
        "source": "ga_dynamic_skills",
        "llm_geometry_allowed": False,
        "geometry_authority": "skill_deterministic_tools",
        "logic_authority": "GA evidence synthesis",
    }


def _data_quality(profiles: dict[str, Any], analysis_time_utc: int) -> dict[str, Any]:
    missing = [tf for tf, profile in profiles.items() if int(profile.get("candles_count") or 0) == 0]
    partial = [tf for tf, profile in profiles.items() if 0 < int(profile.get("candles_count") or 0) < 30]
    return {
        "status": "complete" if not missing and not partial else "partial",
        "closed_candles_only": True,
        "analysis_time_utc": int(analysis_time_utc),
        "missing_timeframes": missing,
        "low_sample_timeframes": partial,
        "note": "所有 K 线查询均限制 close_time <= analysis_time_utc。",
    }


def _previous_trend_stage(repo: CryptoGuardRepository, symbol: str, analysis_time_utc: int) -> str | None:
    row = repo.conn.execute(
        """
        SELECT result_json FROM module_analysis_results
        WHERE symbol=? AND module='trend_stage_fusion' AND analysis_time < ?
        ORDER BY analysis_time DESC
        LIMIT 1
        """,
        (symbol, int(analysis_time_utc)),
    ).fetchone()
    if not row:
        return None
    import json

    try:
        return json.loads(row["result_json"]).get("trend_stage")
    except Exception:
        return None
