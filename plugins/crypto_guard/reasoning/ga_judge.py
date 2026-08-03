from __future__ import annotations

import math
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.reasoning.decision_schema import no_edge_decision, validate_json
from plugins.crypto_guard.reasoning.watch_conditions import normalize_opportunity_watch
from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot
from plugins.crypto_guard.utils import _strict_positive_int_ms


def _get_min_risk_distance(entry: float) -> float:
    """Get minimum risk distance based on entry price magnitude."""
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    min_sl_pct = float(risk_cfg.get("min_sl_distance_pct", 0.8)) / 100.0
    return entry * min_sl_pct


def _snapshot_analysis_time_utc(snapshot: dict[str, Any]) -> int:
    """Phase G (07-05): extract strict-positive-int analysis_time_utc from
    the snapshot for the no_edge fallback. The schema requires
    ``analysis_time_utc: integer, minimum=1``; passing 0 or a wall-clock
    fallback would re-introduce the original defect (schema-invalid no_edge
    fallback crashing the chain on the second validate_json call). When
    the snapshot lacks a usable value, raise — the caller must fail-closed
    rather than emit a wall-clock time, per PRD FR-7.
    """
    at = _strict_positive_int_ms(snapshot.get("analysis_time_utc"))
    if at is None:
        raise ValueError(
            "no_edge_decision requires snapshot-authoritative analysis_time_utc; "
            "snapshot lacks a strict positive int. PRD FR-7 forbids wall-clock fallbacks."
        )
    return at


def _invalid_condition_buffer_pct() -> float:
    """BTC#9 fix: invalid_condition 与 stop_loss 之间的最小缓冲（比例 0-1）。

    Returns a ratio where:
    - 0.0 = same as stop_loss (no buffer)
    - 0.3 = 30% from stop toward entry
    - 1.0 = at entry price

    Config key: risk.invalid_condition_buffer_ratio (default 0.3).

    R3-F: NaN/non-finite config values fail closed (returns default 0.3).
    """
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    raw = risk_cfg.get("invalid_condition_buffer_ratio", 0.3)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.3
    if not math.isfinite(val):
        return 0.3
    return val


def _invalid_condition_price(invalid: float, side: str, entry: float | None = None) -> float | None:
    """Calculate invalid_condition_price with correct ordering.

    LONG:  stop_loss < invalid_condition_price < entry_price
          invalid_condition_price = invalid + (entry - invalid) * buffer_ratio

    SHORT: entry_price < invalid_condition_price < stop_loss
           invalid_condition_price = invalid - (invalid - entry) * buffer_ratio

    invalid_condition = structural invalidation (early fail)
    stop_loss = hard risk protection (last resort)

    BTC#9 fix: entry is required — if missing, return None (fail-closed).
    The old percentage-based fallback produced wrong-direction values.

    R3-F: NaN/Infinity/non-finite values for invalid or entry fail closed.
    """
    if entry is None or entry <= 0 or entry == invalid:
        return None
    if not math.isfinite(invalid) or not math.isfinite(entry):
        return None

    buffer_ratio = _invalid_condition_buffer_pct()
    buffer_ratio = min(max(buffer_ratio, 0.1), 0.5)  # clamp 10%-50%

    if side == "LONG":
        # invalid_condition_price = invalid + (entry - invalid) * buffer_ratio
        # Moves from stop_loss toward entry
        return invalid + (entry - invalid) * buffer_ratio
    else:
        # invalid_condition_price = invalid - (invalid - entry) * buffer_ratio
        # Moves from stop_loss toward entry
        return invalid - (invalid - entry) * buffer_ratio


def _match_price_precision(price: float, reference: float) -> float:
    """Round price to match reference price's decimal precision."""
    ref_str = f"{reference:.10f}".rstrip("0")
    if "." in ref_str:
        decimals = len(ref_str.split(".")[1])
    else:
        decimals = 0
    return round(price, decimals)


def _extract_structured_entry_confirmation(
    snapshot: dict[str, Any],
    side: str,
    entry: float,
) -> dict[str, Any] | None:
    """Extract structured entry_trigger_confirmation from PA/SMC module structure_events.

    BTC#9 fix: traverses real ``price_action.structure_events`` and
    ``smc.structure_events`` lists. Forbids defaulting
    timeframe/closed/direction — missing fields reject the event.

    Selection criteria (newest valid first):
    - direction matches trade side (LONG→bullish, SHORT→bearish)
    - closed must be strictly True (identity check, R4-D5)
    - candle_close_time <= snapshot.analysis_time_utc (no future leak)
    - price > 0 and finite

    Returns None if no structured event is available (deterministic plans
    that can't source real confirmation won't have one — blocked by
    require_ec gate and downgraded to opportunity_watch).

    R9-1: symbol is mandatory — sourced from snapshot.symbol. If snapshot
    lacks symbol, return None (fail-closed). This aligns the generation
    end with R8's symbol-mandatory shape check contract.
    """
    import math

    snap_symbol = str(snapshot.get("symbol") or "")
    if not snap_symbol:
        return None

    modules = snapshot.get("modules") or {}
    # R11-5: snapshot.analysis_time_utc must be strict positive int
    analysis_time = _strict_positive_int_ms(snapshot.get("analysis_time_utc"))
    if analysis_time is None:
        return None

    # Collect candidate events from both PA and SMC modules
    candidates: list[dict[str, Any]] = []

    for module_key in ("price_action", "smc"):
        module_data = modules.get(module_key) or {}
        events = module_data.get("structure_events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            candidates.append({**event, "_source": module_key})

    # Also check SMC events under a different key for compatibility
    smc = modules.get("smc") or {}
    smc_events = smc.get("structure_events")
    if isinstance(smc_events, list):
        for event in smc_events:
            if not isinstance(event, dict):
                continue
            if "_source" not in event:
                candidates.append({**event, "_source": "smc"})

    if not candidates:
        return None

    expected_dir = "bullish" if side == "LONG" else "bearish"

    # Filter and validate events
    valid_events: list[tuple[int, dict[str, Any]]] = []
    for event in candidates:
        # Parse event_type
        raw_event_type = str(event.get("event") or event.get("type") or "").upper()
        for prefix, canonical in [("BULLISH_BOS", "BOS"), ("BEARISH_BOS", "BOS"),
                                   ("BULLISH_CHOCH", "CHOCH"), ("BEARISH_CHOCH", "CHOCH"),
                                   ("BOS", "BOS"), ("CHOCH", "CHOCH"),
                                   ("RECLAIM", "RECLAIM"), ("BREAKOUT_RETEST", "BREAKOUT_RETEST")]:
            if raw_event_type == prefix:
                raw_event_type = canonical
                break
        if raw_event_type not in {"BOS", "CHOCH", "RECLAIM", "BREAKOUT_RETEST"}:
            continue

        # Direction: must be explicitly present, no defaulting
        direction = str(event.get("direction") or "").lower()
        if direction not in {"bullish", "bearish"}:
            # Try to derive from the raw event name only if it contains explicit direction
            raw_name = str(event.get("event") or event.get("type") or "").lower()
            if "bullish" in raw_name:
                direction = "bullish"
            elif "bearish" in raw_name:
                direction = "bearish"
            else:
                # No explicit direction — reject this event
                continue

        if direction != expected_dir:
            continue

        # Timeframe: must be explicitly present and valid, no defaulting
        timeframe = str(event.get("timeframe") or "")
        if timeframe not in {"1m", "5m", "15m", "1h", "4h"}:
            continue

        # candle_close_time: R11-5 must be strict positive int
        close_time = _strict_positive_int_ms(event.get("candle_close_time") or event.get("close_time"))
        if close_time is None:
            continue

        # No future leak (R10-4: analysis_time is guaranteed positive here)
        if close_time > analysis_time:
            continue

        # Price: must be finite positive
        price = event.get("price") or event.get("close")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue

        # R4-D5: closed must be strictly True — reject None, False, "false" string, etc.
        # Previously: `if closed is not None and not bool(closed)` which accepted
        # closed=None (missing) and closed="false" (bool("false")==True in Python).
        closed = event.get("closed")
        if closed is not True:
            continue

        source = event.get("_source", "price_action")
        valid_events.append((close_time, {
            "type": "closed_candle_confirmation",
            "timeframe": timeframe,
            "event_type": raw_event_type,
            "direction": direction,
            "candle_close_time": close_time,
            "price": price,
            "source": source,
            "symbol": snap_symbol,
        }))

    if not valid_events:
        return None

    # Sort by close_time descending (newest valid first)
    valid_events.sort(key=lambda x: x[0], reverse=True)
    return valid_events[0][1]


# P1-3: structural-break event types accepted as closed-candle confirmation of
# a direction flip. Mirrors report_diagnostics._STRUCTURAL_BREAK_TYPES so the
# producer gate and the post-hoc diagnostic share one definition of "a real
# breakout confirms the new direction".
_FLIP_CONFIRMATION_BREAK_TYPES = frozenset({
    "BOS", "BREAK_OF_STRUCTURE",
    "CHOCH", "CHANGE_OF_CHARACTER",
    "BREAKOUT", "BREAKDOWN",
})

# P1-3: timeframes whose structure_events may confirm a flip. Includes 1d
# (mirrors report_diagnostics._SUPPORTED_TIMEFRAMES) so a higher-timeframe
# closed-candle breakout counts just as much as an intraday one.
_FLIP_CONFIRMATION_TIMEFRAMES = frozenset({
    "1m", "5m", "15m", "1h", "4h", "1d",
})

# P1-3: map production price_action structure_event names to canonical break
# types. Mirrors report_diagnostics._EVENT_NAME_TO_TYPE so the in-memory gate
# accepts exactly the same events the DB-based diagnostic accepts.
_FLIP_EVENT_NAME_TO_TYPE: dict[str, str] = {
    "bullish_bos": "BOS",
    "bearish_bos": "BOS",
    "bullish_choch": "CHOCH",
    "bearish_choch": "CHOCH",
    "bullish_breakout": "BREAKOUT",
    "bearish_breakout": "BREAKOUT",
    "bullish_breakdown": "BREAKDOWN",
    "bearish_breakdown": "BREAKDOWN",
}


def _normalize_in_memory_event(
    raw: dict[str, Any], *, timeframe: str
) -> dict[str, Any] | None:
    """P1-3: map an in-memory structure_event dict to the canonical shape.

    Mirrors ``report_diagnostics._normalize_snapshot_event`` but runs on the
    snapshot's in-memory ``timeframe_modules[tf].price_action.structure_events``
    (and ``smc`` equivalents) — no DB access. ``ga_judge`` has no repo handle,
    so the producer-side flip gate must read structured evidence straight from
    the snapshot that the analyzer already built.

    Canonical shape: ``{"event_type", "timeframe", "closed", "time", "direction"}``.

    Strict rules (same as the diagnostic):
    - event time MUST come ONLY from the source event's ``close_time`` (candle
      close), never invented and never substituted with ``time`` /
      ``event_time`` / a module ``analysis_time``. 终审返工 P1-3 (2026-07-25):
      an event that carries only ``time`` or ``event_time`` (missing
      ``close_time``) MUST be rejected - previously the helper fell back to
      ``time``/``event_time`` and could confirm a flip from a non-candle-close
      timestamp, defeating the closed-candle guarantee.
    - ``closed`` MUST be strictly ``is True`` (identity), mirroring the
      production ``price_action_engine`` shape (see ga_judge.py line ~230:
      ``if closed is not True: ...``). 终审返工 P1-3 (2026-07-25): explicit
      ``closed=False``, missing ``closed``, and truthy strings ("true"/"1"/
      "yes") are ALL rejected - no invented ``True``. Previously truthy strings
      were accepted, which let a non-boolean ``closed`` confirm a flip.
    - event_type must map to a structural-break type;
    - direction must be derivable (event name prefix or explicit field);
    - timeframe must be supported and non-empty.
    """
    event_name = str(raw.get("event", "")).lower().strip()
    direct_type = raw.get("event_type")
    if direct_type:
        event_type = str(direct_type).upper()
    elif event_name in _FLIP_EVENT_NAME_TO_TYPE:
        event_type = _FLIP_EVENT_NAME_TO_TYPE[event_name]
    else:
        type_field = str(raw.get("type", "")).upper().strip()
        if not type_field or type_field == "NONE":
            return None
        event_type = type_field

    if event_type not in _FLIP_CONFIRMATION_BREAK_TYPES:
        return None

    direction = ""
    if event_name.startswith("bullish") or event_name.startswith("bos_bull") or event_name.startswith("choch_bull"):
        direction = "bullish"
    elif event_name.startswith("bearish") or event_name.startswith("bos_bear") or event_name.startswith("choch_bear"):
        direction = "bearish"
    elif raw.get("direction"):
        direction = str(raw.get("direction")).lower().strip()
    if direction not in {"bullish", "bearish"}:
        return None

    tf = str(timeframe or "").lower().strip()
    if not tf or tf not in _FLIP_CONFIRMATION_TIMEFRAMES:
        return None

    # 终审返工 P1-3 (2026-07-25): the event time MUST come ONLY from the
    # source event's ``close_time`` (candle close). Fallback to ``time`` /
    # ``event_time`` / module ``analysis_time`` is FORBIDDEN - a flip must be
    # confirmed by a CLOSED CANDLE's close time, not an arbitrary timestamp
    # carried on the event. An event missing ``close_time`` is rejected.
    event_time = raw.get("close_time")
    if event_time is None:
        return None
    try:
        event_time_ms = int(event_time)
    except (TypeError, ValueError):
        return None
    if event_time_ms <= 0:
        return None

    # 终审返工 P1-3 (2026-07-25): ``closed`` MUST be strictly ``is True``
    # (identity), mirroring the production ``price_action_engine`` shape
    # (ga_judge.py line ~230: ``if closed is not True: ...``). Truthy strings
    # ("true"/"1"/"yes"), ints, missing, and False are ALL rejected - no
    # invented ``True``.
    if raw.get("closed") is not True:
        return None
    closed = True

    return {
        "event_type": event_type,
        "timeframe": tf,
        "closed": closed,
        "time": event_time_ms,
        "direction": direction,
    }


def _collect_in_memory_flip_events(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """P1-3: gather normalized structural-break events from the snapshot.

    Reads ``snapshot["timeframe_modules"]`` (per-TF module dicts, the analyzer
    keeps these for every required timeframe) and ``snapshot["modules"]``
    (primary TF modules) — each module dict may carry ``price_action`` and
    ``smc`` sub-dicts whose ``structure_events`` / ``events`` / ``structure_breaks``
    lists hold the production shape events. This is the in-memory analogue of
    ``report_diagnostics._lookup_snapshot_events``.
    """
    normalized: list[dict[str, Any]] = []
    seen_ids: set[tuple] = set()

    def _absorb(module_data: dict[str, Any], *, fallback_tf: str) -> None:
        for module_key in ("price_action", "smc"):
            sub = module_data.get(module_key) or {}
            if not isinstance(sub, dict):
                continue
            for list_key in ("structure_events", "events", "structure_breaks", "breakouts", "breakdowns"):
                items = sub.get(list_key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    tf = str(item.get("timeframe") or "").lower().strip() or fallback_tf
                    canon = _normalize_in_memory_event(item, timeframe=tf)
                    if canon is None:
                        continue
                    # De-dup by (type, tf, time, dir) — the same event may be
                    # surfaced under both price_action and smc, and once in
                    # primary modules and once in timeframe_modules.
                    key = (canon["event_type"], canon["timeframe"], canon["time"], canon["direction"])
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    normalized.append(canon)

    tf_modules = snapshot.get("timeframe_modules") or {}
    if isinstance(tf_modules, dict):
        for tf, modules_for_tf in tf_modules.items():
            if isinstance(modules_for_tf, dict):
                _absorb(modules_for_tf, fallback_tf=str(tf).lower().strip())

    primary = snapshot.get("modules") or {}
    if isinstance(primary, dict):
        _absorb(primary, fallback_tf=str(primary.get("price_action", {}).get("timeframe") or "").lower().strip())

    return normalized


def _has_in_memory_closed_candle_flip_confirmation(
    snapshot: dict[str, Any], new_side: str, *, prev_ts: int = 0
) -> bool:
    """P1-3: producer-side gate — closed-candle breakout confirms a flip.

    Returns True when the snapshot's in-memory structural-break events contain
    a closed-candle break (BOS / BREAK_OF_STRUCTURE / CHOCH / CHANGE_OF_CHARACTER
    / BREAKOUT / BREAKDOWN) on a supported timeframe whose candle-close time is
    strictly after ``prev_ts`` (the previous decision time) and not after the
    current ``snapshot.analysis_time_utc``, and whose direction matches
    ``new_side`` (LONG→bullish, SHORT→bearish).

    Mirrors ``report_diagnostics._has_structured_confirmation`` but operates on
    the in-memory snapshot rather than the DB, so ``ga_judge`` (no repo) can
    apply the same fail-closed rule the post-hoc diagnostic applies. Text /
    inline evidence is never accepted — only structured module events.
    """
    new_side_norm = str(new_side or "").upper().strip()
    if new_side_norm not in {"LONG", "SHORT"}:
        return False
    analysis_time = _strict_positive_int_ms(snapshot.get("analysis_time_utc"))
    if analysis_time is None:
        return False

    for event in _collect_in_memory_flip_events(snapshot):
        event_time_ms = int(event.get("time") or 0)
        if event_time_ms <= 0:
            continue
        if prev_ts > 0 and event_time_ms <= prev_ts:
            continue
        if event_time_ms > analysis_time:
            continue
        direction = str(event.get("direction", "")).lower().strip()
        if new_side_norm == "LONG" and direction in {"bullish", "long", "up"}:
            return True
        if new_side_norm == "SHORT" and direction in {"bearish", "short", "down"}:
            return True
    return False


def _build_trade_plan(snapshot: dict[str, Any], side: str) -> dict[str, Any] | None:
    pa = snapshot["modules"].get("price_action") or {}
    momentum = snapshot["modules"].get("momentum") or {}
    smc = snapshot["modules"].get("smc") or {}
    levels = pa.get("key_levels", {})
    support = levels.get("support") or []
    resistance = levels.get("resistance") or []
    invalid = pa.get("invalid_level")
    rng = pa.get("range") or {}
    atr = ((momentum.get("atr") or {}).get("current") or 0)

    # 1. Determine invalid_level (stop loss) first from structure
    if invalid is None:
        if side == "LONG":
            swing_lows = pa.get("swing_lows") or []
            if swing_lows:
                invalid = min(float(s["price"]) for s in swing_lows[-3:])
        else:
            swing_highs = pa.get("swing_highs") or []
            if swing_highs:
                invalid = max(float(s["price"]) for s in swing_highs[-3:])

    if invalid is None:
        if side == "LONG" and support:
            invalid = min(float(s) for s in support)
        elif side == "SHORT" and resistance:
            invalid = max(float(s) for s in resistance)

    if invalid is None:
        if side == "LONG":
            invalid = rng.get("low")
        else:
            invalid = rng.get("high")

    if invalid is None:
        mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
        if mid > 0:
            offset = mid * 0.015
            invalid = mid - offset if side == "LONG" else mid + offset

    if invalid is None:
        return None

    # 2. Determine entry price (single calculation point)
    entry = None
    entry_type = "limit"
    trigger_price = None

    if side == "LONG":
        entry = support[-1] if support else None
        if entry is None:
            fvg = (smc.get("fvg") or {}).get("range")
            if fvg and len(fvg) == 2:
                entry = (fvg[0] + fvg[1]) / 2
        if entry is None and momentum.get("quality") in ("healthy", "building"):
            last_high = rng.get("high")
            if last_high and last_high > invalid:
                entry = float(last_high)
                entry_type = "trigger"
                trigger_price = float(last_high)
        if entry is None:
            mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
            if mid > 0 and mid > invalid:
                entry = mid
    else:
        entry = resistance[-1] if resistance else None
        if entry is None:
            fvg = (smc.get("fvg") or {}).get("range")
            if fvg and len(fvg) == 2:
                entry = (fvg[0] + fvg[1]) / 2
        if entry is None and momentum.get("quality") in ("healthy", "building"):
            last_low = rng.get("low")
            if last_low and last_low < invalid:
                entry = float(last_low)
                entry_type = "trigger"
                trigger_price = float(last_low)
        if entry is None:
            mid = ((rng.get("high") or 0) + (rng.get("low") or 0)) / 2
            if mid > 0 and mid < invalid:
                entry = mid

    if entry is None:
        return None

    # 3. Validate and adjust stop distance
    if side == "LONG":
        if invalid >= entry:
            invalid = entry - entry * 0.001
        risk = entry - invalid
        min_risk = max(_get_min_risk_distance(entry), atr * 0.2 if atr > 0 else 0)
        if risk < min_risk:
            # Try to find a wider swing low
            swing_lows = pa.get("swing_lows") or []
            candidates = [float(s["price"]) for s in swing_lows if float(s["price"]) < entry and entry - float(s["price"]) >= min_risk]
            if candidates:
                invalid = max(candidates)  # nearest valid swing low
            else:
                invalid = entry - min_risk
            risk = entry - invalid
    else:
        if invalid <= entry:
            invalid = entry + entry * 0.001
        risk = invalid - entry
        min_risk = max(_get_min_risk_distance(entry), atr * 0.2 if atr > 0 else 0)
        if risk < min_risk:
            swing_highs = pa.get("swing_highs") or []
            candidates = [float(s["price"]) for s in swing_highs if float(s["price"]) > entry and float(s["price"]) - entry >= min_risk]
            if candidates:
                invalid = min(candidates)
            else:
                invalid = entry + min_risk
            risk = invalid - entry

    # 4. Match precision and build plan
    entry = _match_price_precision(entry, entry)
    invalid = _match_price_precision(invalid, entry)

    if side == "LONG":
        raw_icp = _invalid_condition_price(float(invalid), "LONG", entry)
        if raw_icp is None:
            return None
        invalid_cond_price = _match_price_precision(raw_icp, entry)
        # Try to extract structured entry_trigger_confirmation from PA module
        entry_confirm = _extract_structured_entry_confirmation(snapshot, side, entry)
        return {
            "side": "LONG",
            "entry_type": entry_type,
            "entry_price": float(entry),
            "trigger_price": trigger_price,
            "stop_loss": float(invalid),
            "take_profits": [
                {"price": float(_match_price_precision(entry + risk * 1.5, entry)), "ratio": 0.5},
                {"price": float(_match_price_precision(entry + risk * 2.5, entry)), "ratio": 0.5},
            ],
            "risk_percent": 0.5,
            "invalid_condition": f"15m 收盘跌破 {invalid_cond_price}",
            "entry_trigger_confirmation": entry_confirm,
            "reason": "结构偏多，等待回踩确认；仅用于模拟盘",
        }
    else:
        raw_icp = _invalid_condition_price(float(invalid), "SHORT", entry)
        if raw_icp is None:
            return None
        invalid_cond_price = _match_price_precision(raw_icp, entry)
        entry_confirm = _extract_structured_entry_confirmation(snapshot, side, entry)
        return {
            "side": "SHORT",
            "entry_type": entry_type,
            "entry_price": float(entry),
            "trigger_price": trigger_price,
            "stop_loss": float(invalid),
            "take_profits": [
                {"price": float(_match_price_precision(entry - risk * 1.5, entry)), "ratio": 0.5},
                {"price": float(_match_price_precision(entry - risk * 2.5, entry)), "ratio": 0.5},
            ],
            "risk_percent": 0.5,
            "invalid_condition": f"15m 收盘站回 {invalid_cond_price}",
            "entry_trigger_confirmation": entry_confirm,
            "reason": "结构偏空，等待反抽确认；仅用于模拟盘",
        }


def _build_sop_watch(side: str, plan: dict[str, Any] | None, *, reason: str) -> dict[str, Any] | None:
    """P0-3 (08-02): deterministic SOP opportunity-watch builder.

    Builds a structured watch — the exact shape the opportunity watcher
    (``opportunity_watcher._condition_hit``) can evaluate — from the
    candidate plan via the shared normalizer. Text conditions are NEVER
    authored here; the normalizer either keeps structured conditions or
    builds them deterministically from the plan
    (pullback/breakout/reclaim + stop invalidation). Returns None on
    fail-closed (no usable side/plan structure); callers MUST then drop
    ``create_opportunity_watch`` from ``suggested_actions``.
    """
    base = {"needed": True, "direction": side, "reason": reason, "expires_minutes": 240}
    watch, _notes = normalize_opportunity_watch(base, plan)
    return watch


def run_ga_sop_decision(snapshot: dict[str, Any], *, score_adjustment: float = 0.0) -> dict[str, Any]:
    """Run deterministic SOP decision.

    Args:
        snapshot: Market state snapshot
        score_adjustment: Optional score adjustment for candidate evaluation
    """
    # P0-3: Generation layer fail-closed. When analysis_degraded=True (set by
    # market_state_builder when data health fails), force the decision to a
    # degraded-but-recorded shape: market_bias=unknown, confidence_tier=C,
    # has_trade_plan=False, no create_paper_order in suggested_actions. Do NOT
    # call _build_trade_plan — the data is too degraded to author a trade plan.
    analysis_degraded = bool(snapshot.get("analysis_degraded") or
                             ((snapshot.get("data_quality") or {}).get("analysis_degraded")))
    if analysis_degraded:
        symbol = snapshot["symbol"]
        # R1-3 (07-03 final review): degraded decisions must still satisfy
        # the tightened schema (timeframe_context/alignment/htf_conflict/
        # market_reason_codes required). Build structured fields from the
        # snapshot so the degraded record is schema-valid and downstream
        # diagnostics can reason about the degraded state.
        from plugins.crypto_guard.reasoning.market_semantics import (
            build_timeframe_context, compute_alignment,
        )
        snap_dq = snapshot.get("data_quality") or {}
        snap_health = snap_dq.get("health_by_tf") or snap_dq.get("health") or {}
        tf_ctx = build_timeframe_context(
            snapshot.get("profiles") or {},
            closed_candles_only=True,
            analysis_degraded=True,
            health_by_tf=snap_health if snap_health else None,
            analysis_time_utc=snapshot.get("analysis_time_utc"),
        )
        alignment, htf_conflict = compute_alignment(snapshot.get("profiles") or {}, tf_ctx)
        result = {
            "symbol": symbol,
            "decision": "monitor_only",
            "signal_grade": "C",
            "market_bias": "unknown",
            "trend_stage": "unknown",
            "confidence": 0.3,
            # Phase F (07-05): degraded decisions still carry raw_signal_grade
            # / raw_score for audit parity. They equal the degraded values
            # because no SOP scoring ran.
            "raw_signal_grade": "C",
            "raw_score": 0.3,
            "summary": f"{symbol} 行情数据不完整，分析降级，方向不可靠，仅记录本次分析。",
            "evidence": [],
            "counter_evidence": ["行情数据不完整，无法产生可靠方向判断"],
            "risk_notes": ["分析降级：数据不完整，不生成交易计划。", "不构成实盘建议，仅用于模拟盘与策略研究。"],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["monitor_only"],
            "strategy_name": "ga_sop_degraded",
            "strategy_version": "1.0",
            "analysis_time_utc": snapshot.get("analysis_time_utc"),
            "degraded_reason": "analysis_degraded: market data health check failed (contiguity/freshness/gap)",
            "timeframe_context": tf_ctx,
            "alignment": alignment,
            "htf_conflict": htf_conflict,
            "market_reason_codes": ["data_incomplete"],
        }
        ok, err = validate_json("ga_decision.schema.json", result)
        if not ok:
            # Schema validation failed — fall back to no_edge rather than
            # raising; degraded decisions must still be recorded. Phase G
            # (07-05): pass snapshot-authoritative analysis_time_utc so the
            # no_edge fallback is schema-valid on the second validate_json
            # call (the schema requires analysis_time_utc: integer, minimum=1).
            fallback = no_edge_decision(
                symbol,
                err or "degraded schema error",
                analysis_time_utc=_snapshot_analysis_time_utc(snapshot),
                timeframe_context=result.get("timeframe_context") or tf_ctx,
            )
            ok2, err2 = validate_json("ga_decision.schema.json", fallback)
            if not ok2:
                raise ValueError(f"no_edge fallback schema 校验失败: {err2}")
            return fallback
        return result

    scoring = score_snapshot(snapshot, score_adjustment=score_adjustment)
    symbol = snapshot["symbol"]
    grade = scoring["signal_grade"]
    trend_stage = (snapshot["modules"].get("trend_stage") or {}).get("trend_stage", "unknown")
    bias = scoring["market_bias"]
    # Phase F (07-05): capture raw_signal_grade / raw_score BEFORE any
    # hysteresis, clamp, or risk gate adjustments run. These are the
    # deterministic SOP's pre-gate conclusions and must be persisted
    # alongside the effective (post-gate) grade/score so the report can
    # render "原始评分 95% · 执行等级 B（评级迟滞 C→S 暂缓）" without
    # losing the original signal strength. canonical signal_grade remains
    # the effective grade for backward compatibility.
    raw_signal_grade = grade
    raw_score = round(float(scoring.get("score", 0.0)), 4)
    # Determine side: bias first, then momentum direction as fallback
    side = "LONG" if bias == "bullish" else "SHORT" if bias == "bearish" else None
    if side is None:
        momentum_dir = (snapshot["modules"].get("momentum") or {}).get("direction")
        if momentum_dir == "bullish" and scoring["score"] >= 0.72:
            side = "LONG"
            bias = "bullish"
        elif momentum_dir == "bearish" and scoring["score"] >= 0.72:
            side = "SHORT"
            bias = "bearish"
    # Generate trade plan for A/S grades when side is available
    # Grade threshold (A>=0.72) already incorporates risk assessment
    trade_plan = _build_trade_plan(snapshot, side) if side and scoring["score"] >= 0.72 else None

    # Phase D (07-05): deterministic continuity gate. If the prior round's
    # next_triggers have been invalidated for the current side, withhold the
    # trade plan and downgrade to wait_for_pullback. This prevents executing
    # a candidate plan whose prior confirmation context has flipped against
    # it. The LLM still sees the prior context (via analysis_continuity in
    # _compact_snapshot) and can refine; the deterministic path fail-closes.
    continuity = snapshot.get("analysis_continuity") or {}
    delta = (continuity.get("delta") or {}) if continuity else {}
    trigger_progress = delta.get("trigger_progress") or []
    side_invalidated = False
    side_str = ""
    if trade_plan and trigger_progress:
        side_str = str(trade_plan.get("side") or "")
        for trig in trigger_progress:
            ttype = str(trig.get("type") or "")
            status = str(trig.get("status") or "")
            if status != "invalidated":
                continue
            # breakout_confirm invalidated → bearish flip → invalidate LONG.
            # breakdown_confirm invalidated → bullish flip → invalidate SHORT.
            if side_str == "LONG" and ttype in {"breakout_confirm", "momentum_confirm"}:
                side_invalidated = True
                break
            if side_str == "SHORT" and ttype in {"breakdown_confirm", "momentum_confirm"}:
                side_invalidated = True
                break
    # P0-3 (08-02): fail-closed diagnostics. When a branch WANTED a watch but
    # the normalizer could not build structured conditions, record a note so
    # the decision row surfaces why no auto watch was materialized. Initialized
    # BEFORE the side_invalidated / direction-flip branches so their notes are
    # not overwritten by a later re-initialization (08-02 review P2-B).
    result_watch_fail_closed_note = None
    if side_invalidated:
        # Downgrade to wait_for_pullback but preserve the candidate plan dict
        # for audit (Phase E: candidate_trade_plan). Set structured
        # plan_status / plan_blockers so downstream consumers (report,
        # diagnostics) can distinguish "withheld due to invalidated prior
        # trigger" from "no plan ever generated".
        invalidated_plan = trade_plan
        trade_plan = None
        invalidated_reasons = [
            f"前次 {side_str} 触发已被反转 invalidated（continuity gate）",
        ]
        # Re-route to wait_for_pullback so the watch list still tracks it.
        decision = "wait_for_pullback"
        suggested = ["create_opportunity_watch", "add_to_watchlist", "ignore"]
        watch = _build_sop_watch(side_str, invalidated_plan, reason="前次触发已被反转，等待结构重新确认")
        if watch is None:
            # Fail-closed: no usable structure -> no auto watch, and the
            # manual button must not fire on a watch-less decision.
            suggested = [a for a in suggested if a != "create_opportunity_watch"]
            result_watch_fail_closed_note = "机会监控条件无法结构化，fail-closed：不自动创建机会监控。"
        # Stash the invalidated candidate so the result carries it as
        # candidate_trade_plan (Phase E contract).
        result_invalidated_candidate = invalidated_plan
        result_invalidated_reasons = invalidated_reasons
        result_invalidated_blockers = [
            {
                "code": "continuity_trigger_invalidated",
                "stage": "synthesis",
                "detail": f"前次 {side_str} 触发已被反转 invalidated",
            }
        ]
        result_invalidated_plan_status = "withheld"
        result_invalidated_plan_source = "deterministic_sop"
    else:
        result_invalidated_candidate = None
        result_invalidated_reasons = None
        result_invalidated_blockers = None
        result_invalidated_plan_status = None
        result_invalidated_plan_source = None

    # P1-3 (07-22 production review): producer-side direction-flip gate. When
    # the prior round had a concrete side (LONG/SHORT) and the current
    # candidate trade_plan flips to the opposite side, the flip MUST be backed
    # by a closed-candle structural breakout/failure event on the snapshot.
    # Without it, the candidate plan is withheld - the system keeps observing
    # rather than generating a new-direction candidate. This is the producer
    # counterpart of report_diagnostics._check_direction_flip_without_closed_candle
    # (which only warns post-hoc). Existing execution blocking (risk_gate,
    # side_invalidated) stays as defense-in-depth; this gate stops the new
    # candidate from even being proposed.
    # Only applies when side_invalidated did NOT already withhold the plan
    # (avoid double-gating) and a real candidate trade_plan still exists.
    direction_flip_withheld = False
    if trade_plan:
        prev_block = (snapshot.get("analysis_continuity") or {}).get("previous") or {}
        prev_side = str(prev_block.get("side") or "").upper().strip() if isinstance(prev_block, dict) else ""
        cur_side = str(trade_plan.get("side") or "").upper().strip()
        if prev_side in {"LONG", "SHORT"} and cur_side in {"LONG", "SHORT"} and prev_side != cur_side:
            prev_ts = int(prev_block.get("analysis_time") or 0) if isinstance(prev_block, dict) else 0
            if not _has_in_memory_closed_candle_flip_confirmation(snapshot, cur_side, prev_ts=prev_ts):
                direction_flip_withheld = True
                withheld_flip_plan = trade_plan
                withheld_flip_side = cur_side
                trade_plan = None
                result_invalidated_candidate = withheld_flip_plan
                result_invalidated_reasons = [
                    f"方向由 {prev_side} 翻转至 {cur_side}，缺已收盘 K 线突破/失败证据，暂缓新方向候选（继续观察）",
                ]
                result_invalidated_blockers = [
                    {
                        "code": "direction_flip_without_closed_candle_confirmation",
                        "stage": "synthesis",
                        "detail": f"方向 {prev_side}->{cur_side} 翻转无已收盘结构突破确认",
                    }
                ]
                result_invalidated_plan_status = "withheld"
                result_invalidated_plan_source = "deterministic_sop"
                decision = "wait_for_pullback"
                suggested = ["create_opportunity_watch", "add_to_watchlist", "ignore"]
                watch = _build_sop_watch(withheld_flip_side, withheld_flip_plan, reason="方向翻转缺已收盘突破确认，继续观察等待结构确认")
                if watch is None:
                    suggested = [a for a in suggested if a != "create_opportunity_watch"]
                    result_watch_fail_closed_note = "机会监控条件无法结构化，fail-closed：不自动创建机会监控。"
    # Track whether the flip gate fired so the decision-routing block below
    # can branch on it (the side_invalidated branch already routed via pass).
    _flip_gate_routed = direction_flip_withheld
    if trade_plan:
        decision = "trade_plan_available"
        suggested = ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore"]
        watch = _build_sop_watch(trade_plan["side"], trade_plan, reason="若限价未成交，可继续观察回踩条件")
        if watch is None:
            suggested = [a for a in suggested if a != "create_opportunity_watch"]
            result_watch_fail_closed_note = "机会监控条件无法结构化，fail-closed：不自动创建机会监控。"
    elif result_invalidated_candidate is not None and _flip_gate_routed:
        # P1-3 flip-gate branch already set decision/suggested/watch above.
        pass
    elif result_invalidated_candidate is not None:
        # side_invalidated branch already set decision/suggested/watch above.
        pass
    elif scoring["score"] >= 0.65 and side:
        decision = "wait_for_pullback" if bias in ("bullish", "bearish") else "monitor_only"
        # P0-3: no candidate plan exists here (score in [0.65, 0.72) did not
        # produce a trade_plan), so a watch is unbuildable -> fail-closed to
        # None and drop create_opportunity_watch. Pre-fix this branch emitted
        # a text-condition watch that could never trigger.
        watch = _build_sop_watch(side, None, reason="方向有倾向但交易计划不完整，等待结构确认")
        if watch is None:
            suggested = ["add_to_watchlist", "ignore"]
            result_watch_fail_closed_note = "机会监控条件无法结构化（无交易计划），fail-closed：不自动创建机会监控。"
        else:
            suggested = ["create_opportunity_watch", "add_to_watchlist", "ignore"]
    elif scoring["score"] >= 0.50:
        decision = "monitor_only"
        suggested = ["add_to_watchlist", "ignore"]
        watch = None
    else:
        decision = "no_edge"
        suggested = ["add_to_watchlist", "ignore"]
        watch = None

    result = {
        "symbol": symbol,
        "decision": decision,
        "signal_grade": grade,
        "market_bias": bias if bias in ("bullish", "bearish", "neutral", "mixed") else "mixed",
        "trend_stage": trend_stage if trend_stage in ("early", "middle", "late", "range", "transition") else "unknown",
        "confidence": round(scoring["score"], 4),
        # Phase F (07-05): raw pre-gate grade/score. effective grade is
        # canonical signal_grade (set by controller after hysteresis/clamp);
        # raw_signal_grade / raw_score never change after this point.
        "raw_signal_grade": raw_signal_grade,
        "raw_score": raw_score,
        "summary": _summary(symbol, decision, grade, bias, trend_stage),
        "evidence": scoring.get("evidence", []),
        "counter_evidence": scoring.get("counter_evidence", []),
        "risk_notes": scoring.get("counter_evidence", []) + ["不构成实盘建议，仅用于模拟盘与策略研究。"] + (
            [result_watch_fail_closed_note] if result_watch_fail_closed_note else []
        ),
        "has_trade_plan": bool(trade_plan),
        "trade_plan": trade_plan,
        "opportunity_watch": watch,
        "suggested_actions": suggested,
        "strategy_name": scoring["strategy_name"],
        "strategy_version": scoring["strategy_version"],
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "plan_status": "executable" if trade_plan else (
            result_invalidated_plan_status if result_invalidated_plan_status else "no_plan"
        ),
        "plan_source": "deterministic_sop" if trade_plan else (
            result_invalidated_plan_source if result_invalidated_plan_source else "deterministic_sop"
        ),
        "plan_blockers": list(result_invalidated_blockers) if result_invalidated_blockers else [],
    }
    if trade_plan:
        # Executable plan — also surface as candidate for audit parity.
        result["candidate_trade_plan"] = trade_plan
    elif result_invalidated_candidate is not None:
        result["candidate_trade_plan"] = result_invalidated_candidate
    # Phase D (07-05): if the continuity gate invalidated the candidate plan,
    # surface the invalidated candidate and reason on the result so Phase E
    # can formalize it as candidate_trade_plan. For now the audit fields
    # allow downstream consumers (controller, report) to distinguish a
    # withheld-due-to-invalidated-trigger plan from a never-generated one.
    if result_invalidated_candidate is not None:
        result["invalidated_candidate_plan"] = result_invalidated_candidate
        notes = list(result.get("risk_notes") or [])
        notes.extend(result_invalidated_reasons or [])
        result["risk_notes"] = notes
    # Phase B (07-03): structured market-semantic normalization. Surfaces
    # timeframe_context/alignment/htf_conflict/market_reason_codes and applies
    # the bias+stage contract + htf_conflict confidence cap + grade downgrade.
    # Must run BEFORE schema validation so downstream consumers see the
    # normalized state.
    from plugins.crypto_guard.reasoning.market_semantics import normalize_market_semantics
    market_semantics_cfg = (load_config().trading_mode.get("market_semantics") or {})
    # Propagate snapshot-level alignment/htf_conflict/timeframe_context onto
    # the decision so normalize_market_semantics can read them.
    if snapshot.get("timeframe_context"):
        result.setdefault("timeframe_context", snapshot.get("timeframe_context"))
    if snapshot.get("alignment"):
        result.setdefault("alignment", snapshot.get("alignment"))
    if snapshot.get("htf_conflict") is not None:
        result.setdefault("htf_conflict", snapshot.get("htf_conflict"))
    result = normalize_market_semantics(result, snapshot, market_semantics_cfg)
    ok, err = validate_json("ga_decision.schema.json", result)
    if not ok:
        # Phase G (07-05): pass snapshot-authoritative analysis_time_utc so
        # the no_edge fallback is schema-valid on the second validate_json
        # call (the schema requires analysis_time_utc: integer, minimum=1).
        fallback = no_edge_decision(
            symbol,
            err or "unknown schema error",
            analysis_time_utc=_snapshot_analysis_time_utc(snapshot),
            timeframe_context=result.get("timeframe_context") or snapshot.get("timeframe_context"),
        )
        ok2, err2 = validate_json("ga_decision.schema.json", fallback)
        if not ok2:
            raise ValueError(f"no_edge fallback schema 校验失败: {err2}")
        return fallback
    return result


def _summary(symbol: str, decision: str, grade: str, bias: str, trend_stage: str) -> str:
    if decision == "trade_plan_available":
        return f"{symbol} 当前为 {grade} 级模拟盘候选，结构倾向 {bias}，趋势阶段 {trend_stage}。"
    if decision.startswith("wait_for"):
        return f"{symbol} 当前为 {grade} 级观察机会，方向有倾向但需要等待触发条件。"
    if decision == "monitor_only":
        return f"{symbol} 当前仅适合观察，暂不生成模拟盘计划。"
    return f"{symbol} 当前无明显优势，系统仅记录本次分析。"
