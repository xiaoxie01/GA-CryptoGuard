"""Phase C (07-05): MultiTimeframeFeaturePack builder.

Builds a bounded, schema-versioned per-TF compact feature pack that survives
into ``snapshot["timeframe_modules"]`` and the LLM prompt payload. The
deterministic tools (price_action/momentum/trend_stage/smc/order_flow/chanlun)
consume raw 1d/4h/1h/15m/5m closed candles and emit structured module dicts;
this builder retains a *compact* per-TF view (sample_count, data_as_of, bias,
structure, momentum, indicators, smc, order_flow, chanlun, health) without
raw candle arrays, full swing histories, skill prompts, or logs.

Phase D (07-05) adds ``build_analysis_continuity``: a strict, schema-versioned
PreviousAnalysisCompact + AnalysisDelta block that surfaces prior grade/bias/
stage/key_levels/next_triggers to the LLM prompt and exposes
``delta.trigger_progress`` (confirmed/invalidated) for the deterministic
continuity gate. The block is bounded; raw state blobs are never included.

Size budget: 24 KiB serialized JSON. The builder enforces this and trims
key_levels / structure_events / indicators when exceeded.
"""

from __future__ import annotations

import json
from typing import Any

FEATURE_PACK_VERSION = 2
FEATURE_PACK_SIZE_BUDGET_BYTES = 24 * 1024
MAX_KEY_LEVELS_PER_TF = 6
MAX_STRUCTURE_EVENTS_PER_TF = 4
MAX_NEXT_TRIGGERS = 6

ANALYSIS_CONTINUITY_VERSION = 1
ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES = 12 * 1024
CONTINUITY_MAX_AGE_MS = 24 * 60 * 60 * 1000  # 24h; older = stale
CONTINUITY_MAX_TRIGGERS = 6


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compact_indicators(momentum_mod: dict[str, Any]) -> dict[str, Any]:
    """Build compact indicator view from the momentum module."""
    if not isinstance(momentum_mod, dict):
        return {}
    rsi = momentum_mod.get("rsi")
    macd = momentum_mod.get("macd") or {}
    if not isinstance(macd, dict):
        macd = {}
    atr = momentum_mod.get("atr") or {}
    if not isinstance(atr, dict):
        atr = {}
    return {
        "rsi": _safe_float(rsi) if rsi is not None else None,
        "rsi_slope": _safe_float(momentum_mod.get("rsi_slope"), 0.0) if momentum_mod.get("rsi_slope") is not None else None,
        "macd_hist": _safe_float(macd.get("histogram")) if macd.get("histogram") is not None else None,
        "macd_hist_slope": _safe_float(macd.get("histogram_slope"), 0.0) if macd.get("histogram_slope") is not None else None,
        "atr_ratio": _safe_float(atr.get("ratio"), 1.0) if atr.get("ratio") is not None else None,
        "momentum_score": momentum_mod.get("momentum_score"),
        "divergence": bool(momentum_mod.get("divergence", False)),
        "volume_confirmed": bool(momentum_mod.get("volume_confirmed", False)),
    }


def _compact_smc(smc_mod: dict[str, Any]) -> dict[str, Any]:
    """Build compact SMC view."""
    if not smc_mod or not smc_mod.get("implemented", True):
        return {"implemented": False}
    fvg = smc_mod.get("fvg") or {}
    if not isinstance(fvg, dict):
        fvg = {}
    ob = smc_mod.get("order_block") or {}
    if not isinstance(ob, dict):
        ob = {}
    pd = smc_mod.get("premium_discount") or {}
    # P1-7 defensive: premium_discount may be a string in some fixtures
    # / older profiles. Coerce to dict-like or use the string as the zone.
    if isinstance(pd, str):
        pd_zone = pd
        pd = {}
    elif not isinstance(pd, dict):
        pd = {}
        pd_zone = None
    else:
        pd_zone = pd.get("zone") or pd.get("status")
    liq = smc_mod.get("liquidity") or {}
    if not isinstance(liq, dict):
        liq = {}
    return {
        "implemented": True,
        "fvg_exists": bool(fvg.get("exists", False)),
        "fvg_direction": fvg.get("direction"),
        "order_block_exists": bool(ob.get("exists", False)),
        "ob_direction": ob.get("direction"),
        "premium_discount": pd_zone,
        "last_event": liq.get("last_event"),
    }


def _compact_order_flow(of_mod: dict[str, Any]) -> dict[str, Any]:
    """Build compact order flow view."""
    if not isinstance(of_mod, dict):
        return {"degraded": True}
    return {
        "cvd_slope": of_mod.get("cvd_slope"),
        "aggressive_buy_ratio": of_mod.get("aggressive_buy_ratio"),
        "flow_confirmation": of_mod.get("flow_confirmation"),
        "delta_divergence": of_mod.get("delta_divergence"),
        "degraded": bool(of_mod.get("degraded", False)),
    }


def _compact_chanlun(chanlun_mod: dict[str, Any]) -> dict[str, Any]:
    """Build compact chanlun view.

    R5 P0-1 fix: read the real ``chanlun_engine`` output fields
    (``current_bi_direction``, ``central_zone``, ``signal``,
    ``trend_direction``, ``current_structure``, ``divergence_candidate``).
    The previous implementation read ``bi.direction`` / ``zd.level`` which
    never existed in the engine output, so production feature packs always
    carried ``None`` chanlun semantics — a direct PRD FR-2 violation.
    """
    if not isinstance(chanlun_mod, dict):
        return {}
    central_zone = chanlun_mod.get("central_zone")
    if not isinstance(central_zone, dict):
        central_zone = {}
    return {
        "current_bi_direction": chanlun_mod.get("current_bi_direction"),
        "trend_direction": chanlun_mod.get("trend_direction"),
        "current_structure": chanlun_mod.get("current_structure"),
        "signal": chanlun_mod.get("signal"),
        "central_zone": {
            "high": central_zone.get("high"),
            "low": central_zone.get("low"),
            "exists": bool(central_zone),
        } if central_zone else None,
        "divergence_candidate": bool(chanlun_mod.get("divergence_candidate", False)),
        # R6 REC-R6-2: surface confidence + evidence_role so the LLM can
        # distinguish high-confidence chanlun signals from low-confidence
        # ones. PRD FR-2 requires "最近且可信的" evidence — confidence is
        # the credibility signal.
        "confidence": chanlun_mod.get("confidence"),
        "evidence_role": chanlun_mod.get("evidence_role"),
    }


def _compact_health(health: dict[str, Any] | None) -> dict[str, Any]:
    """Build compact health view from the per-TF health dict."""
    if not isinstance(health, dict):
        return {"ready": False, "reason": "missing"}
    return {
        "ready": bool(health.get("ready", False)),
        "last_close_time": int(health.get("last_close_time") or 0),
        "contiguous_count": int(health.get("contiguous_count") or 0),
        "required_count": int(health.get("required_count") or 0),
        "stale_bars": int(health.get("stale_bars") or 0),
        "reason": str(health.get("reason") or ""),
    }


def _compact_module_for_tf(
    tf: str,
    modules: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    *,
    health: dict[str, Any] | None,
    analysis_time_utc: int,
) -> dict[str, Any]:
    """Build a compact per-TF module view.

    Returns: {tf, sample_count, data_as_of, bias, structure, momentum,
              trend_stage, invalid_level, indicators, smc, order_flow,
              chanlun, health, key_levels, structure_events}

    R5 P0-1 fix: ``trend_stage`` and ``invalid_level`` are exposed as
    top-level fields (previously buried inside ``modules.trend_stage`` /
    ``modules.price_action`` and never surfaced in the compacted view).
    Downstream consumers (LLM prompt, feishu cards) can now read these
    without re-entering the full modules dict.

    P0-1 fix: ``data_as_of`` uses the per-TF ``last_close_time`` from health,
    not the global analysis_time_utc. This correctly distinguishes 1D/4H bars
    (which close hours before the 15m batch) from the 15m/5m bars.

    P0-2 fix: ``bias`` reads from ``profile.market_structure`` (the per-TF
    market structure) rather than ``profile.momentum``. ``profile.momentum``
    is the momentum direction — a different axis. The bias enum follows
    market_structure: bullish/bearish/neutral/range/transition/unknown.
    """
    modules = modules if isinstance(modules, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    price_action = modules.get("price_action") or {}
    if not isinstance(price_action, dict):
        price_action = {}
    momentum_mod = modules.get("momentum") or {}
    if not isinstance(momentum_mod, dict):
        momentum_mod = {}
    trend_stage_mod = modules.get("trend_stage") or {}
    if not isinstance(trend_stage_mod, dict):
        trend_stage_mod = {}
    smc_mod = modules.get("smc") or {}
    order_flow_mod = modules.get("order_flow") or {}
    chanlun_mod = modules.get("chanlun") or {}

    # P0-2 fix: bias from market_structure, NOT momentum.
    bias = (
        profile.get("market_structure")
        or price_action.get("market_structure")
        or trend_stage_mod.get("trend_stage")
        or "unknown"
    )
    structure = (
        profile.get("market_structure")
        or price_action.get("market_structure")
        or trend_stage_mod.get("structure")
        or "unknown"
    )
    momentum_dir = (
        momentum_mod.get("direction")
        or profile.get("momentum")
        or "unknown"
    )

    # P0-1 fix: per-TF data_as_of = last_close_time from health.
    last_close = int((health or {}).get("last_close_time") or 0)
    data_as_of = last_close if last_close > 0 else int(analysis_time_utc)

    key_levels_raw = price_action.get("key_levels") or {}
    if isinstance(key_levels_raw, dict):
        support = list(key_levels_raw.get("support") or [])[:MAX_KEY_LEVELS_PER_TF]
        resistance = list(key_levels_raw.get("resistance") or [])[:MAX_KEY_LEVELS_PER_TF]
        key_levels = {"support": support, "resistance": resistance}
    else:
        key_levels = {"support": [], "resistance": []}

    structure_events_raw = price_action.get("structure_events") or []
    if isinstance(structure_events_raw, list):
        structure_events = list(structure_events_raw)[:MAX_STRUCTURE_EVENTS_PER_TF]
    else:
        structure_events = []

    return {
        "tf": tf,
        "sample_count": int(profile.get("candles_count") or 0),
        "data_as_of": data_as_of,
        "bias": str(bias),
        "structure": str(structure),
        "momentum": str(momentum_dir),
        "trend_stage": str(
            trend_stage_mod.get("trend_stage")
            or trend_stage_mod.get("stage")
            or "unknown"
        ),
        "invalid_level": price_action.get("invalid_level"),
        "indicators": _compact_indicators(momentum_mod),
        "smc": _compact_smc(smc_mod),
        "order_flow": _compact_order_flow(order_flow_mod),
        "chanlun": _compact_chanlun(chanlun_mod),
        "health": _compact_health(health),
        "key_levels": key_levels,
        "structure_events": structure_events,
    }


def build_multi_timeframe_feature_pack(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded, schema-versioned MultiTimeframeFeaturePack.

    Reads per-TF modules from ``snapshot["timeframe_modules"]`` (when the
    market_state_builder populated it) or falls back to deriving a compact
    view from ``snapshot["profiles"]`` + the primary ``snapshot["modules"]``.

    Per-TF health is read from ``snapshot["data_quality"]["health"][tf]``
    (populated by market_state_builder). When absent, falls back to a
    per-TF health dict with ``ready=False`` so the feature pack always
    carries a health view.

    The pack is bounded to FEATURE_PACK_SIZE_BUDGET_BYTES. When exceeded,
    structure_events and key_levels are trimmed first; if still over budget,
    indicators/smc/order_flow/chanlun are dropped in that order.
    """
    profiles = snapshot.get("profiles") or {}
    timeframe_modules = snapshot.get("timeframe_modules") or {}
    analysis_time_utc = int(snapshot.get("analysis_time_utc") or 0)
    health_by_tf = ((snapshot.get("data_quality") or {}).get("health") or {})

    # Determine the TF set. Always include the standard 5 TFs that the
    # deterministic tools consume; tolerate partial presence.
    tfs = []
    for tf in ("1d", "4h", "1h", "15m", "5m"):
        if tf in profiles or tf in timeframe_modules or tf in health_by_tf:
            tfs.append(tf)

    per_tf: dict[str, dict[str, Any]] = {}
    for tf in tfs:
        per_tf[tf] = _compact_module_for_tf(
            tf,
            timeframe_modules.get(tf) or {},
            profiles.get(tf) or {},
            health=health_by_tf.get(tf),
            analysis_time_utc=analysis_time_utc,
        )

    pack: dict[str, Any] = {
        "schema_version": FEATURE_PACK_VERSION,
        "size_budget_bytes": FEATURE_PACK_SIZE_BUDGET_BYTES,
        "analysis_time_utc": analysis_time_utc,
        "symbol": snapshot.get("symbol"),
        "timeframes": tfs,
        "modules": per_tf,
    }

    # Enforce size budget. Trim in order: structure_events, key_levels,
    # indicators, smc, order_flow, chanlun.
    serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf].pop("structure_events", None)
        serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf]["key_levels"] = {
                "support": (per_tf[tf].get("key_levels", {}).get("support") or [])[:3],
                "resistance": (per_tf[tf].get("key_levels", {}).get("resistance") or [])[:3],
            }
        serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf].pop("indicators", None)
        serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf].pop("smc", None)
        serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf].pop("order_flow", None)
        serialized = json.dumps(pack, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > FEATURE_PACK_SIZE_BUDGET_BYTES:
        for tf in tfs:
            per_tf[tf].pop("chanlun", None)
        serialized = json.dumps(pack, ensure_ascii=False)

    pack["serialized_size_bytes"] = len(serialized.encode("utf-8"))
    pack["size_budget_exceeded"] = pack["serialized_size_bytes"] > FEATURE_PACK_SIZE_BUDGET_BYTES
    return pack


def attach_feature_pack_to_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Idempotently attach a MultiTimeframeFeaturePack to the snapshot.

    The pack is stored under ``snapshot["multi_timeframe_feature_pack"]`` so
    ``_compact_snapshot`` can surface it to the LLM prompt without modifying
    the snapshot's ``modules`` field (which carries the primary-TF modules
    for backward compatibility with deterministic consumers).
    """
    if snapshot.get("multi_timeframe_feature_pack"):
        return snapshot
    snapshot["multi_timeframe_feature_pack"] = build_multi_timeframe_feature_pack(snapshot)
    return snapshot


# ────────────────────────────────────────────────────────────────────────────
# Phase D (07-05): Analysis continuity — PreviousAnalysisCompact + AnalysisDelta
# ────────────────────────────────────────────────────────────────────────────


def _continuity_status(
    *,
    previous_row: dict[str, Any] | None,
    current_symbol: str,
    current_analysis_time_utc: int,
    current_batch_id: str | None,
) -> str:
    """Classify the previous-state row against the current analysis context.

    Returns one of: ``ok``, ``missing``, ``stale``, ``future``,
    ``same_batch``, ``cross_symbol``. ``ok`` means the row is a valid
    prior state for the continuity block; everything else means the row
    must not influence the current decision (audit-only).
    """
    if previous_row is None:
        return "missing"
    prev_symbol = str(previous_row.get("symbol") or "")
    if prev_symbol != current_symbol:
        return "cross_symbol"
    prev_time = int(previous_row.get("analysis_time") or 0)
    if prev_time <= 0:
        return "missing"
    if prev_time >= current_analysis_time_utc:
        # Same-time or future — same_batch is the most likely cause.
        return "same_batch" if prev_time == current_analysis_time_utc else "future"
    age_ms = current_analysis_time_utc - prev_time
    if age_ms > CONTINUITY_MAX_AGE_MS:
        return "stale"
    if current_batch_id and previous_row.get("batch_id") == current_batch_id:
        return "same_batch"
    return "ok"


def _is_executable_trade_plan(trade_plan: Any) -> bool:
    """Phase E (07-05) P1-4 fix: a real executable plan is a dict with at
    least side/entry/stop. The legacy ``has_trade_plan`` flag is unreliable
    because the controller's fail-closed path sets it to ``False`` even
    when a candidate plan exists; conversely, some legacy paths set it to
    ``True`` without a populated trade_plan.

    The canonical check is structural: dict + side + entry/trigger_price +
    stop_loss. This matches the trade_plan validator used by risk_engine.
    """
    if not isinstance(trade_plan, dict):
        return False
    if not trade_plan.get("side"):
        return False
    has_entry = bool(
        trade_plan.get("entry_price") is not None
        or trade_plan.get("trigger_price") is not None
    )
    has_stop = bool(trade_plan.get("stop_loss") is not None)
    return has_entry and has_stop


def _compact_previous_state(previous_row: dict[str, Any]) -> dict[str, Any]:
    """Build the PreviousAnalysisCompact view from the full row.

    P1-3 fix: ``grade`` reads from the joined ``ga_decisions.signal_grade``
    column (S/A/B/C/D) rather than ``state.trend_clarity.level`` (which is
    clear/mixed/unclear — a clarity axis, not a grade). The join is done by
    ``latest_analysis_state_for_continuity`` which returns the row with
    ``signal_grade`` populated when JOIN succeeded.

    P1-4 fix: ``plan_status`` is identified by structurally checking the
    trade_plan dict (side/entry/stop), not by reading the unreliable
    ``has_trade_plan`` flag.

    R5 P0-2 fix: when no executable trade_plan exists, the function falls
    back to ``state.candidate_trade_plan`` to recover trigger_price/side
    so the next round can judge whether the withheld candidate is still
    alive. Previously, a withheld candidate's trigger/side were silently
    None — breaking PRD FR-3 cross-round continuity for A-grade signals
    that were withheld by risk/judge.

    Includes: analysis_state_id, analysis_time, grade, confidence, bias,
    stage, timeframe_summary, key_levels, plan_status, reason_codes,
    next_triggers, trigger_price, side. Excludes raw state_json blobs,
    full trade_plan dicts, and any field not listed above.
    """
    state = previous_row.get("state") or {}
    market_structure = state.get("market_structure") or {}
    no_trade = state.get("no_trade_reason") or {}
    trade_plan = state.get("trade_plan") or {}
    # R5 P0-2 fix: read the withheld candidate persisted by
    # build_market_analysis_state. When the trade_plan is non-executable
    # (withheld by risk/judge), the candidate still carries the trigger
    # price/side that the next round needs to judge whether the candidate
    # is still alive. Without this fallback, the previous_compact's
    # trigger_price/side are silently None for every withheld round.
    candidate_trade_plan = state.get("candidate_trade_plan") or {}
    # P1-4 fix: structural check instead of has_trade_plan flag.
    if _is_executable_trade_plan(trade_plan):
        plan_status = "executable"
        # R5 P0-2: executable plan — read trigger/side from it.
        continuity_plan = trade_plan
    elif _is_executable_trade_plan(candidate_trade_plan):
        plan_status = "withheld"
        # R5 P0-2: fallback to the withheld candidate's trigger/side.
        continuity_plan = candidate_trade_plan
    elif no_trade.get("has_no_trade"):
        plan_status = "withheld"
        continuity_plan = candidate_trade_plan if isinstance(candidate_trade_plan, dict) else {}
    elif trade_plan:
        # Has a dict but not executable — withheld candidate.
        plan_status = "withheld"
        continuity_plan = candidate_trade_plan if isinstance(candidate_trade_plan, dict) else {}
    else:
        plan_status = "unknown"
        continuity_plan = {}
    # P1-3 fix: prefer signal_grade from the row (joined from ga_decisions)
    # — fall back to state's signal_grade if the JOIN column is absent.
    grade_value = (
        previous_row.get("signal_grade")
        or state.get("signal_grade")
        or state.get("grade")
    )
    # If still absent, derive from trade_plan existence (best effort).
    if not grade_value:
        grade_value = "B" if plan_status == "executable" else "D"
    return {
        "analysis_state_id": int(previous_row.get("id") or 0) or None,
        "analysis_time": int(previous_row.get("analysis_time") or 0),
        "grade": str(grade_value).upper(),
        "confidence": float(state.get("trend_clarity", {}).get("score") or 0.0),
        "bias": str(
            market_structure.get("direction_4h")
            or market_structure.get("direction_1d")
            or "unknown"
        ),
        "stage": str(market_structure.get("trend_1h") or "unknown"),
        "timeframe_summary": {
            "1d": market_structure.get("direction_1d") or "unknown",
            "4h": market_structure.get("direction_4h") or "unknown",
            "1h": market_structure.get("trend_1h") or "unknown",
            "15m": market_structure.get("structure_15m") or "unknown",
            "5m": market_structure.get("trigger_5m") or "unknown",
        },
        "key_levels": _compact_key_levels(state.get("key_levels") or {}),
        "plan_status": str(plan_status),
        "reason_codes": _compact_reason_codes(state, no_trade),
        "next_triggers": list(state.get("next_triggers") or [])[:CONTINUITY_MAX_TRIGGERS],
        # P1-5 fix: surface trigger_price + side so the delta can verify
        # trigger progress against the actual candle close at the trigger
        # time, not a heuristic proxy.
        # R5 P0-2 fix: when the executable plan is withheld, read
        # trigger_price/side from the withheld candidate (continuity_plan)
        # so the next round can judge whether the candidate is still alive.
        "trigger_price": _extract_trigger_price(continuity_plan),
        "side": str(continuity_plan.get("side") or "").upper() if isinstance(continuity_plan, dict) else "",
    }


def _extract_trigger_price(trade_plan: Any) -> float | None:
    """Extract the trigger price from a trade plan dict."""
    if not isinstance(trade_plan, dict):
        return None
    for key in ("trigger_price", "entry_price"):
        v = trade_plan.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _compact_key_levels(key_levels: dict[str, Any]) -> dict[str, Any]:
    """Trim key_levels to bounded support/resistance lists."""
    return {
        "support": list(key_levels.get("support") or [])[:3],
        "resistance": list(key_levels.get("resistance") or [])[:3],
        "breakout_boundary": key_levels.get("breakout_boundary") or {},
    }


def _compact_reason_codes(state: dict[str, Any], no_trade: dict[str, Any]) -> list[str]:
    """Collect reason codes from state and no_trade_reason."""
    codes: list[str] = []
    if no_trade.get("reason_code"):
        codes.append(str(no_trade.get("reason_code")))
    for evidence in (state.get("counter_evidence") or [])[:3]:
        if isinstance(evidence, dict):
            code = evidence.get("code") or evidence.get("reason_code")
            if code:
                codes.append(str(code))
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:5]


def _build_delta(
    *,
    previous_compact: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    current_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the AnalysisDelta between previous and current state.

    ``current_decision`` is optional — when supplied, the delta records
    grade/bias/stage transitions and trigger progress. When ``None`` (the
    LLM prompt path), the delta records only what the snapshot can tell
    us (timeframe changes vs the previous compact).
    """
    if previous_compact is None:
        return {
            "elapsed_bars": None,
            "grade_change": None,
            "bias_change": None,
            "stage_change": None,
            "timeframe_changes": {},
            "new_reason_codes": [],
            "cleared_reason_codes": [],
            "trigger_progress": [],
            "thesis_status": "unknown",
        }
    current_analysis_time = int(current_snapshot.get("analysis_time_utc") or 0)
    prev_time = int(previous_compact.get("analysis_time") or 0)
    elapsed_ms = max(0, current_analysis_time - prev_time) if prev_time else 0
    # Approximate bars at 15m granularity (continuity is hourly-decided).
    elapsed_bars = elapsed_ms // (15 * 60 * 1000) if elapsed_ms else None

    current_decision = current_decision or {}
    current_grade = str(current_decision.get("signal_grade") or "unknown")
    current_bias = str(current_decision.get("market_bias") or "unknown")
    current_stage = str(current_decision.get("trend_stage") or "unknown")

    prev_grade = str(previous_compact.get("grade") or "unknown")
    prev_bias = str(previous_compact.get("bias") or "unknown")
    prev_stage = str(previous_compact.get("stage") or "unknown")

    grade_change = (
        f"{prev_grade}->{current_grade}"
        if prev_grade != "unknown" and current_grade != "unknown" and prev_grade != current_grade
        else None
    )
    bias_change = (
        f"{prev_bias}->{current_bias}"
        if prev_bias != "unknown" and current_bias != "unknown" and prev_bias != current_bias
        else None
    )
    stage_change = (
        f"{prev_stage}->{current_stage}"
        if prev_stage != "unknown" and current_stage != "unknown" and prev_stage != current_stage
        else None
    )

    # Timeframe changes: compare previous_compact.timeframe_summary to current
    # snapshot.profiles (1d/4h/1h/15m/5m market_structure/trend_stage).
    profiles = current_snapshot.get("profiles") or {}
    prev_tf = previous_compact.get("timeframe_summary") or {}
    curr_tf = {
        "1d": (profiles.get("1d") or {}).get("market_structure") or "unknown",
        "4h": (profiles.get("4h") or {}).get("market_structure") or "unknown",
        "1h": (profiles.get("1h") or {}).get("trend_stage") or "unknown",
        "15m": (profiles.get("15m") or {}).get("market_structure") or "unknown",
        "5m": (profiles.get("5m") or {}).get("market_structure") or "unknown",
    }
    tf_changes: dict[str, dict[str, str]] = {}
    for tf in ("1d", "4h", "1h", "15m", "5m"):
        before = str(prev_tf.get(tf) or "unknown")
        after = str(curr_tf.get(tf) or "unknown")
        if before != "unknown" and after != "unknown" and before != after:
            tf_changes[tf] = {"from": before, "to": after}

    # Reason-code diff
    prev_codes = set(previous_compact.get("reason_codes") or [])
    curr_codes_set: set[str] = set()
    for code in (current_decision.get("market_reason_codes") or []):
        curr_codes_set.add(str(code))
    risk = current_decision.get("risk_check") or {}
    for reason in (risk.get("reasons") or [])[:5]:
        # Map common reason strings to codes; otherwise hash.
        curr_codes_set.add(str(reason)[:60])
    new_codes = sorted(curr_codes_set - prev_codes)
    cleared_codes = sorted(prev_codes - curr_codes_set)

    # Trigger progress: verify against actual candle close + trigger price.
    trigger_progress = _trigger_progress(
        previous_compact.get("next_triggers") or [],
        current_snapshot=current_snapshot,
        current_decision=current_decision,
        previous_compact=previous_compact,
    )

    # Thesis status: confirmed if bias unchanged and grade improved;
    # invalidated if bias flipped; otherwise unchanged/unknown.
    if bias_change and "bearish" in bias_change and "bullish" in bias_change:
        thesis = "invalidated"
    elif bias_change is None and prev_bias not in {"unknown", "neutral", "mixed"}:
        thesis = "confirmed" if grade_change and "->S" in (grade_change or "") else "unchanged"
    else:
        thesis = "unchanged"

    return {
        "elapsed_bars": elapsed_bars,
        "grade_change": grade_change,
        "bias_change": bias_change,
        "stage_change": stage_change,
        "timeframe_changes": tf_changes,
        "new_reason_codes": new_codes,
        "cleared_reason_codes": cleared_codes,
        "trigger_progress": trigger_progress,
        "thesis_status": thesis,
    }


def _latest_candle_close(
    snapshot: dict[str, Any], tf: str
) -> float | None:
    """Get the latest closed candle close price for a given TF from the snapshot.

    The snapshot does not carry raw candles (P0-2 contract: no raw candle
    arrays to LLM). However, the primary_modules carry the most-recent
    ``price_action.last_close`` field when populated by the price_action
    skill. For non-primary TFs, fall back to None.

    P1-5 fix: read from ``timeframe_modules[tf].price_action.last_close``
    first (production path), then ``snapshot.modules.price_action.last_close``
    (primary TF), then ``profiles[tf].last_close`` (legacy/test fixture).
    """
    tf_modules = (snapshot.get("timeframe_modules") or {}).get(tf) or {}
    pa = tf_modules.get("price_action") or {}
    close = pa.get("last_close")
    if close is not None:
        return float(close)
    # Fall back to primary modules (primary TF only in production).
    primary_pa = (snapshot.get("modules") or {}).get("price_action") or {}
    close = primary_pa.get("last_close")
    if close is not None:
        return float(close)
    # Fall back to profiles (test fixture path).
    profile = (snapshot.get("profiles") or {}).get(tf) or {}
    close = profile.get("last_close")
    if close is not None:
        return float(close)
    return None


def _trigger_progress(
    previous_triggers: list[dict[str, Any]],
    *,
    current_snapshot: dict[str, Any],
    current_decision: dict[str, Any],
    previous_compact: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Classify each previous next_trigger as confirmed/invalidated/pending.

    P1-5 fix: verify trigger progress by checking the actual candle close
    against the previous trade_plan's trigger_price (when available), not
    by heuristic 15m-structure + 5m-momentum proxy.

    R7 P0 fix: pre-R7 the consumer read ``previous_compact.trigger_price``
    (the candidate trade_plan's entry/trigger price, e.g. 100) while the
    actual breakout boundary (e.g. 110) lived only in the trigger's
    ``condition`` text. With last_close=105 the candidate entry was
    breached but the breakout was not — pre-R7 returned ``confirmed``
    erroneously. Fix: prefer the trigger's *own* structured ``level``
    field (persisted by ``_next_triggers`` since R7).

    R8 P0 fix: pre-R8 the fallback for legacy rows (without structured
    ``level``) was ``previous_compact.trigger_price`` — but that is the
    *candidate entry price*, not the breakout boundary, so it repeats
    the exact semantic error R7 fixed. After the production migration
    the first round may still read legacy rows; falling back to a
    known-wrong semantic is unsafe. Fix: legacy rows without structured
    ``level`` now return ``pending`` (fail-closed) instead of using the
    candidate entry as a proxy boundary.

    R9 P1-1 fix: pre-R9 the legacy fallback also used a structural
    proxy (``market_structure`` + ``momentum_dir``) to return
    ``confirmed``/``invalidated``. That proxy can return ``confirmed``
    for a legacy LONG candidate when momentum is bullish, even though
    the actual breakout boundary was NOT breached — repeating the R7/R8
    semantic error. Fix: legacy rows without structured ``level`` now
    return ``pending`` unconditionally (no structural proxy). Without a
    price boundary there is no semantically safe way to judge
    confirmation; ``pending`` is the fail-closed default.

    ``previous_compact.side`` is still used for ``momentum_confirm``
    (which checks side alignment, not price crossing) — that path is
    semantically safe. ``previous_compact.trigger_price`` is NOT read
    anywhere in this function after R8.

    Heuristics (deterministic; LLM may override textual interpretation):
    - breakout_confirm: confirmed if latest 15m close > level; invalidated
      if close < level * (1 - tolerance). Legacy rows without level →
      pending (no price boundary to test against).
    - breakdown_confirm: inverse.
    - momentum_confirm: confirmed if the 15m momentum direction matches
      the previous side; invalidated if opposite.
    - opportunity_watch: confirmed if create_opportunity_watch in
      current suggested_actions; invalidated if decision is no_edge.
    - fallback: pending.
    """
    out: list[dict[str, Any]] = []
    momentum_dir = (current_snapshot.get("modules") or {}).get("momentum", {}).get("direction")
    suggested = set(current_decision.get("suggested_actions") or [])
    decision = str(current_decision.get("decision") or "")

    # R9 P2-6 fix: correct prior misleading docstring — trigger_price
    # is NOT used for momentum_confirm; only side is. trigger_price is
    # not read anywhere in this function after R8.
    prev_side = ""
    if previous_compact:
        prev_side = str(previous_compact.get("side") or "").upper()

    # Latest 15m close from the snapshot's primary modules (price_action).
    last_close_15m = _latest_candle_close(current_snapshot, "15m")
    # Tolerance: 0.1% around the trigger price.
    tol = 0.001

    for trig in previous_triggers[:CONTINUITY_MAX_TRIGGERS]:
        ttype = str(trig.get("type") or "")
        status = "pending"
        # R7 P0 fix + R8 P0 fix: prefer the trigger's own structured
        # level (the actual breakout/breakdown boundary). Legacy rows
        # without ``level`` return ``pending`` — do NOT fall back to
        # ``previous_compact.trigger_price`` (candidate entry price)
        # because that repeats the R7 semantic error.
        # R9 P2-1 fix: use explicit ``is not None`` instead of truthiness
        # so ``level=0.0`` (a valid float, falsy in Python) is correctly
        # treated as a structured level.
        trig_level = trig.get("level")
        try:
            trig_level = float(trig_level) if trig_level is not None else None
        except (TypeError, ValueError):
            trig_level = None
        if ttype == "breakout_confirm" and trig_level is not None and last_close_15m is not None:
            if last_close_15m > trig_level:
                status = "confirmed"
            elif last_close_15m < trig_level * (1 - tol):
                status = "invalidated"
        elif ttype == "breakdown_confirm" and trig_level is not None and last_close_15m is not None:
            if last_close_15m < trig_level:
                status = "confirmed"
            elif last_close_15m > trig_level * (1 + tol):
                status = "invalidated"
        elif ttype in ("breakout_confirm", "breakdown_confirm"):
            # Legacy fallback (no structured level): fail-closed.
            # R8 P0: do NOT use candidate trigger_price — that is the
            # entry price, not the breakout boundary.
            # R9 P1-1: do NOT use structural proxy (market_structure +
            # momentum_dir) either — it can return 'confirmed' for a
            # legacy LONG candidate when momentum is bullish, even
            # though the actual breakout boundary was NOT breached.
            # Without a structured level we have no price boundary to
            # test against; pending is the only safe default.
            status = "pending"
        elif ttype == "momentum_confirm":
            # P1-5 fix: confirm by checking if prev_side matches current
            # momentum direction, not 5m-vs-5m proxy.
            if prev_side and momentum_dir:
                if prev_side == "LONG" and momentum_dir == "bullish":
                    status = "confirmed"
                elif prev_side == "SHORT" and momentum_dir == "bearish":
                    status = "confirmed"
                elif prev_side == "LONG" and momentum_dir == "bearish":
                    status = "invalidated"
                elif prev_side == "SHORT" and momentum_dir == "bullish":
                    status = "invalidated"
        elif ttype == "opportunity_watch":
            if "create_opportunity_watch" in suggested:
                status = "confirmed"
            elif decision == "no_edge":
                status = "invalidated"
        out.append({
            "trigger_id": trig.get("type") or trig.get("condition", "")[:60],
            "type": ttype,
            "timeframe": str(trig.get("timeframe") or ""),
            "condition": str(trig.get("condition") or "")[:120],
            "status": status,
        })
    return out


def build_analysis_continuity(
    snapshot: dict[str, Any],
    *,
    previous_row: dict[str, Any] | None,
    current_batch_id: str | None = None,
    current_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``analysis_continuity_v1`` block (Phase D, 07-05).

    The block contains:
      - ``contract_version``: ``analysis_continuity_v1``;
      - ``continuity_status``: ``ok|missing|stale|future|same_batch|cross_symbol``;
      - ``previous``: PreviousAnalysisCompact (or ``None`` when missing);
      - ``delta``: AnalysisDelta (always present; empty when no previous).

    The block is bounded to ``ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES``;
    when exceeded, ``next_triggers`` is trimmed first.
    """
    current_symbol = str(snapshot.get("symbol") or "")
    current_at = int(snapshot.get("analysis_time_utc") or 0)
    status = _continuity_status(
        previous_row=previous_row,
        current_symbol=current_symbol,
        current_analysis_time_utc=current_at,
        current_batch_id=current_batch_id,
    )

    if status != "ok":
        return {
            "contract_version": "analysis_continuity_v1",
            "schema_version": ANALYSIS_CONTINUITY_VERSION,
            "continuity_status": status,
            "previous": None,
            "delta": _build_delta(
                previous_compact=None,
                current_snapshot=snapshot,
                current_decision=current_decision,
            ),
            "size_budget_bytes": ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES,
        }

    previous_compact = _compact_previous_state(previous_row or {})
    delta = _build_delta(
        previous_compact=previous_compact,
        current_snapshot=snapshot,
        current_decision=current_decision,
    )
    block = {
        "contract_version": "analysis_continuity_v1",
        "schema_version": ANALYSIS_CONTINUITY_VERSION,
        "continuity_status": status,
        "previous": previous_compact,
        "delta": delta,
        "size_budget_bytes": ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES,
    }

    # Enforce size budget: trim next_triggers, then trigger_progress, then
    # timeframe_changes.
    serialized = json.dumps(block, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES:
        prev = block.get("previous") or {}
        prev["next_triggers"] = (prev.get("next_triggers") or [])[:3]
        serialized = json.dumps(block, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES:
            (block.get("delta") or {})["trigger_progress"] = (
                (block.get("delta") or {}).get("trigger_progress") or []
            )[:3]
            serialized = json.dumps(block, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES:
                (block.get("delta") or {})["timeframe_changes"] = {}

    block["serialized_size_bytes"] = len(json.dumps(block, ensure_ascii=False).encode("utf-8"))
    block["size_budget_exceeded"] = block["serialized_size_bytes"] > ANALYSIS_CONTINUITY_SIZE_BUDGET_BYTES
    return block


def attach_analysis_continuity_to_snapshot(
    snapshot: dict[str, Any],
    *,
    previous_row: dict[str, Any] | None,
    current_batch_id: str | None = None,
    current_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotently attach an ``analysis_continuity`` block to the snapshot.

    The block is stored under ``snapshot["analysis_continuity"]`` so
    ``_compact_snapshot`` can surface it to the LLM prompt without modifying
    the snapshot's ``previous_analysis_state`` field (which carries the
    raw state dict for backward-compat consumers).
    """
    if snapshot.get("analysis_continuity"):
        return snapshot
    snapshot["analysis_continuity"] = build_analysis_continuity(
        snapshot,
        previous_row=previous_row,
        current_batch_id=current_batch_id,
        current_decision=current_decision,
    )
    return snapshot
