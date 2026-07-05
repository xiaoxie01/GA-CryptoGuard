from __future__ import annotations

import logging
from typing import Any

from plugins.crypto_guard.analysis.counter_evidence_engine import build_counter_evidence
from plugins.crypto_guard.analysis.market_regime_engine import classify_market_regime
from plugins.crypto_guard.analysis.trend_stage_engine import fuse_trend_stage
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.market_semantics import normalize_snapshot_semantics
from plugins.crypto_guard.skills.runner import execute_market_skills
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import INTERVAL_MS


logger = logging.getLogger(__name__)


# R1: 1D must enter the snapshot as a real profile (not just background text).
DEFAULT_TIMEFRAMES = ["1d", "4h", "1h", "15m", "5m"]


def _analyze_timeframe(
    repo: CryptoGuardRepository,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    previous_analysis_state: dict[str, Any] | None,
    *,
    read_limit: int | None = None,
) -> dict[str, Any]:
    """Analyze one timeframe.

    R1: ``read_limit`` replaces the old hardcoded ``limit=120``. Callers pass
    ``cfg.market_data.analysis_window[timeframe]`` (250/250/250/200/150).
    R4.1: when a gap exists, callers pass ``contiguous_count`` as the limit so
    indicators only receive the post-gap suffix (no gap-crossing).
    """
    limit = read_limit if read_limit is not None else 120
    candles = repo.get_candles(symbol, timeframe, analysis_time_utc=analysis_time_utc, limit=limit)
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
    return {
        "timeframe": timeframe,
        "candles_count": len(candles),
        "modules": modules,
        "preprocessing": _preprocessing_provenance(),
        "candles": candles,
    }


def build_market_state_snapshot(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    analysis_time_utc: int,
    mode: str,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    tfs = timeframes or DEFAULT_TIMEFRAMES
    cfg = load_config()
    market_data_cfg = cfg.market_data
    required_samples = market_data_cfg.get("required_samples", {})
    analysis_window = market_data_cfg.get("analysis_window", {})

    # R3: Assess health for each TF before building profiles.
    health_by_tf: dict[str, dict[str, Any]] = {}
    any_degraded = False
    for tf in tfs:
        required = int(required_samples.get(tf, 200))
        health = assess_health(
            repo, symbol, tf,
            analysis_time_utc=analysis_time_utc,
            required_count=required,
        )
        health_by_tf[tf] = health
        if not health["ready"]:
            any_degraded = True

    # Pass 7 P0 (07-03 final review): backtest / shadow_test mode loads a
    # single TF (e.g. ``timeframes=["15m"]``) via historical_replay. The
    # 4-TF fail-closed gate in normalize_market_semantics Step 1 would
    # otherwise mark the missing 1d/4h/1h as ``closed=False`` and force the
    # decision to C/0.3/unknown/monitor_only — destroying real trade
    # samples. Expose a separate ``partial_tf_mode`` flag (NOT merged into
    # ``analysis_degraded``) so normalize_market_semantics Step 1 skips the
    # 4-TF fail-closed via ``snap.get("partial_tf_mode")`` without routing
    # the decision into the degraded branch. ``analysis_degraded`` is
    # reserved for real data-quality problems (``any_degraded`` from
    # health_by_tf); mixing partial_tf_mode into it caused ga_judge to
    # produce ``strategy_name="ga_sop_degraded"`` for healthy partial-TF
    # replays. The loaded TFs still contribute their real
    # trend_stage/momentum to the fused result so backtests have
    # meaningful signals.
    REQUIRED_TFS_FOR_FAIL_CLOSED = ("1d", "4h", "1h", "15m")
    partial_tf_mode = (
        mode == "shadow_test"
        and not all(tf in tfs for tf in REQUIRED_TFS_FOR_FAIL_CLOSED)
    )

    profiles: dict[str, Any] = {}
    primary_modules: dict[str, Any] = {}
    previous_analysis_state = repo.latest_analysis_state(symbol)
    for tf in tfs:
        # R1: per-TF read limit from analysis_window (DELETE hardcoded 120).
        # R4.1: when degraded, read only the contiguous tail to prevent
        # indicator gap-crossing. When healthy, read the full analysis_window.
        health = health_by_tf.get(tf) or {}
        contiguous = int(health.get("contiguous_count") or 0)
        window = int(analysis_window.get(tf, 200))
        if contiguous > 0:
            read_limit = min(contiguous, window)
        else:
            read_limit = 0  # no closed candles — empty profile

        result = _analyze_timeframe(
            repo, symbol, tf, analysis_time_utc, previous_analysis_state,
            read_limit=read_limit,
        )
        # Set loaded_count in health (4-field split)
        health_by_tf[tf]["loaded_count"] = len(result["candles"])

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

    # R4: When degraded, force trend_stage and market_structure to unknown.
    # Pass 6 P1 #2: partial_tf_mode (shadow_test with <4 TFs) is NOT a data
    # quality problem — the loaded TFs have real, healthy candles. Only
    # force-unknown when real data quality is degraded, not when we merely
    # have fewer TFs in a backtest.
    data_quality_degraded = any_degraded
    if data_quality_degraded:
        for tf in tfs:
            profiles[tf]["trend_stage"] = "unknown"
            profiles[tf]["market_structure"] = "unknown"
            profiles[tf]["momentum"] = "neutral"

    fused_trend = fuse_trend_stage(profiles, primary_modules.get("trend_stage") or {}, analysis_time_utc=analysis_time_utc)
    # R4: When degraded, force fused trend_stage to unknown.
    if data_quality_degraded:
        fused_trend["trend_stage"] = "unknown"
        fused_trend["stage"] = "unknown"
    market_regime = classify_market_regime(primary_candles, analysis_time_utc=analysis_time_utc)
    # R4: When degraded, regime = unknown, no boost.
    if data_quality_degraded:
        market_regime["regime"] = "unknown"
        market_regime["extreme"] = False
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

    # R4: market_bias = "unknown" when degraded (stricter than "neutral" per follow-up §4)
    if data_quality_degraded:
        primary_modules["market_bias"] = "unknown"

    data_quality = _data_quality(profiles, analysis_time_utc, health_by_tf, any_degraded)

    # Determine degraded status string for data_quality
    degraded_status = _degraded_status(health_by_tf) if data_quality_degraded else "complete"

    # Pass 7 P0: partial_tf_mode (shadow_test with <4 TFs) must NOT be
    # treated as analysis_degraded. The loaded TFs have real, healthy
    # candles — the caller intentionally requested a partial-TF replay.
    # Setting analysis_degraded=True would route ga_judge into the
    # degraded path (monitor_only/C/0.3/unknown) and destroy real trade
    # samples in historical_replay. Instead, expose a separate
    # ``partial_tf_mode`` flag so normalize_market_semantics can skip
    # the 4-TF fail-closed without triggering the degraded path.
    snapshot_analysis_degraded = any_degraded

    snapshot = {
        "symbol": symbol,
        "analysis_time_utc": int(analysis_time_utc),
        "mode": mode,
        "profiles": profiles,
        "modules": primary_modules,
        "counter_evidence": build_counter_evidence(primary_modules),
        "data_quality": data_quality,
        "analysis_degraded": snapshot_analysis_degraded,
        "partial_tf_mode": partial_tf_mode,
        "has_trade_plan": not data_quality_degraded and bool(primary_modules.get("trade_plan")),
        "trade_plan": None if data_quality_degraded else primary_modules.get("trade_plan"),
        "decision": "opportunity_watch" if data_quality_degraded else (primary_modules.get("decision") or "monitor_only"),
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
    # R4: Override data_quality status with degraded value when applicable.
    if data_quality_degraded:
        snapshot["data_quality"]["status"] = degraded_status

    # Phase B (07-03): structured multi-timeframe context + alignment +
    # htf_conflict + bias/stage contract normalization. Surfaces
    # ``timeframe_context``, ``alignment``, ``htf_conflict`` and
    # ``market_reason_codes`` on the snapshot and corrects the top-level
    # market_bias/trend_stage so downstream GA decisions inherit the
    # corrected semantics.
    market_semantics_cfg = (cfg.trading_mode.get("market_semantics") or {}) if hasattr(cfg, "trading_mode") else {}
    normalize_snapshot_semantics(
        snapshot,
        market_semantics_cfg,
        health_by_tf=health_by_tf,
        analysis_time_utc=int(analysis_time_utc),
    )

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


def _degraded_status(health_by_tf: dict[str, dict[str, Any]]) -> str:
    """Map health reasons to a single data_quality.status string."""
    reasons = {h.get("reason", "") for h in health_by_tf.values() if not h.get("ready")}
    if "future_candle" in reasons:
        return "future_candle"
    if "stale" in reasons:
        return "stale"
    if "gapped" in reasons:
        return "gapped"
    if "duplicate_open_time" in reasons:
        return "duplicate_open_time"
    return "insufficient"


def _data_quality(
    profiles: dict[str, Any],
    analysis_time_utc: int,
    health_by_tf: dict[str, dict[str, Any]] | None = None,
    any_degraded: bool = False,
) -> dict[str, Any]:
    missing = [tf for tf, profile in profiles.items() if int(profile.get("candles_count") or 0) == 0]
    partial = [tf for tf, profile in profiles.items() if 0 < int(profile.get("candles_count") or 0) < 30]
    status = "complete" if not (missing or partial) and not any_degraded else (
        _degraded_status(health_by_tf) if any_degraded and health_by_tf else "partial"
    )
    result: dict[str, Any] = {
        "status": status,
        "closed_candles_only": True,
        "analysis_time_utc": int(analysis_time_utc),
        "missing_timeframes": missing,
        "low_sample_timeframes": partial,
        "note": "所有 K 线查询均限制 close_time <= analysis_time_utc。R3: contiguity + freshness gate。",
    }
    # R1/R3: 4-field split per TF in data_quality.health[tf]
    if health_by_tf:
        result["health"] = {}
        for tf, health in health_by_tf.items():
            result["health"][tf] = {
                "total_count": health.get("total_count", 0),
                "loaded_count": health.get("loaded_count", 0),
                "contiguous_count": health.get("contiguous_count", 0),
                "required_count": health.get("required_count", 0),
                # Full health fields for downstream consumers
                "ready": health.get("ready", False),
                "reason": health.get("reason", ""),
                "gap_count": health.get("gap_count", 0),
                "largest_gap_bars": health.get("largest_gap_bars", 0),
                "last_close_time": health.get("last_close_time"),
                "expected_last_close_time": health.get("expected_last_close_time"),
                "stale_bars": health.get("stale_bars", 0),
                "missing_ranges": health.get("missing_ranges", []),
                "first_close_time": health.get("first_close_time"),
                "total_closed_count": health.get("total_closed_count", 0),
                "contiguous_tail_count": health.get("contiguous_tail_count", 0),
            }
    return result


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
