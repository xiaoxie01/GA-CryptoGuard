"""Entry-confirmation lifecycle: canonical fingerprint + trusted-event resolver.

08-10 (design.md §5): the deterministic entry-confirmation lifecycle owns the
``entry_confirmation_events`` audit table and the
``resolve_trusted_entry_confirmation`` resolver. Step 3 ships the canonical
fingerprint + source allowlist + the persistence contract; Step 4 implements
the resolver itself. This module MUST NOT import storage -- the pure resolver
never writes, and the insert method in ``storage/repository.py`` imports only
these pure helpers (function-level, to avoid a circular import). Repository
reads reach the resolver only through the injected ``repo`` object; the strict
entry-confirmation extraction is the single source of truth for provenance
(``ga_judge`` re-exports it unchanged).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from plugins.crypto_guard.utils import _strict_positive_int_ms

# Canonical sources (schemas/ga_decision.schema.json line 61). Any other
# "module" (LLM fabrication, forged tool output, watch-reason text) is
# untrusted data and rejected fail-closed by the insert method.
VALID_CONFIRMATION_SOURCES: frozenset[str] = frozenset(
    {"price_action", "smc", "deterministic_rule"}
)

# Trusted canonical fields covered by the event fingerprint. Excludes ``type``
# (constant ``closed_candle_confirmation``), excludes ``source`` (a
# re-observation through a different module must dedupe against the original
# row), and excludes ``side`` (derivable from ``direction``). The fingerprint
# is the ON CONFLICT key for idempotency and the cross-check the verifier uses
# to pin a proposal to a specific trusted event.
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "symbol",
    "event_type",
    "timeframe",
    "direction",
    "candle_close_time",
    "price",
)

# Timeframes the lifecycle understands (design.md §4: confirmation lives on the
# 5m / 15m entry periods). Bar span and policy rows exist only for these.
_LIFECYCLE_TIMEFRAMES: tuple[str, ...] = ("5m", "15m")
_BAR_MS: dict[str, int] = {"5m": 300_000, "15m": 900_000}

# Deterministic tie-break: on an equal close time the HIGHER timeframe wins (a
# 15m structural event dominates a 5m one at the same close), then the event
# fingerprint for a final stable order.
_TIMEFRAME_PRIORITY: dict[str, int] = {"15m": 0, "5m": 1}

# Envelope check keys in the exact order of design.md §5.1.
_ENVELOPE_CHECK_KEYS: tuple[str, ...] = (
    "same_symbol",
    "same_side",
    "source_event_found",
    "geometry_ok",
    "price_invalidation_clear",
    "opposite_structure_absent",
    "closed_bar_sequence_complete",
)


def canonical_confirmation_fingerprint(confirmation: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical trusted fields (sorted-key JSON, compact).

    Field order / JSON key order / ``source`` / ``type`` never affect the
    fingerprint: ``canonical_confirmation_fingerprint`` is stable across
    re-observation by any module.
    """
    payload = {key: confirmation[key] for key in _FINGERPRINT_FIELDS}
    serialized = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _structure_event_direction(event: Mapping[str, Any]) -> str | None:
    """Structural direction of a structure event, name-first.

    The event NAME is the authoritative structural direction: a ``bullish_choch``
    is a bullish structure even when a contradictory ``direction`` field is
    present (a mismatched field is a data-quality artifact, not a structural
    fact). Falls back to an explicit ``direction`` field only when the name
    carries no direction, and returns None when neither names nor the field
    give a direction (fail-closed -- never default a direction).
    """
    raw_name = str(event.get("event") or event.get("type") or "").lower()
    if "bullish" in raw_name:
        return "bullish"
    if "bearish" in raw_name:
        return "bearish"
    direction = str(event.get("direction") or "").lower()
    if direction in {"bullish", "bearish"}:
        return direction
    return None


def _extract_structured_entry_confirmation(
    snapshot: dict[str, Any],
    side: str,
    entry: float,
) -> dict[str, Any] | None:
    """Extract structured entry_trigger_confirmation from PA/SMC structure_events.

    Preserved verbatim from the previous location in ``ga_judge.py`` (08-08
    P1-3 / R4-D5 / R9-1 provenance rules) -- this is the single source of
    truth, re-exported by ``ga_judge`` so generation and the lifecycle share
    one definition of "a legal closed confirmation".

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
    snap_symbol = str(snapshot.get("symbol") or "")
    if not snap_symbol:
        return None

    modules = snapshot.get("modules") or {}
    # 08-08 P1-3: also read the 15m/5m entry periods from ``timeframe_modules``
    # (in addition to the primary ``modules``), so a legal closed-candle
    # confirmation that lives only in a lower-timeframe entry period is found.
    tf_modules = snapshot.get("timeframe_modules") or {}
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

    # 08-08 P1-3: collect from the 15m/5m entry periods of ``timeframe_modules``.
    # ``_source`` records the timeframe provenance (e.g. "15m:price_action").
    for tf in ("15m", "5m"):
        tf_module = tf_modules.get(tf) or {}
        for module_key in ("price_action", "smc"):
            sub = tf_module.get(module_key) or {}
            events = sub.get("structure_events")
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                candidates.append({**event, "_source": f"{tf}:{module_key}"})

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

        # Direction: structural, name-first -- a ``bullish_choch`` is a bullish
        # structure regardless of a contradictory ``direction`` field; the
        # explicit direction field is used only when the name carries none;
        # never default a direction (missing -> reject this event).
        direction = _structure_event_direction(event)
        if direction is None:
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


def _now_ms() -> int:
    """Current wall time in milliseconds (system-owned, for validated_at)."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _expected_direction(side: str) -> str:
    """Direction implied by a plan side (LONG->bullish, SHORT->bearish)."""
    return "bearish" if side == "SHORT" else "bullish"


def _parse_invalidation_level(condition: Any) -> float | None:
    """Last numeric literal in the condition text = the structured level.

    Mirrors diagnostics/state_consistency.py
    ``_check_invalid_condition_equals_stop_loss`` (re.findall ... [-1]): the
    text also embeds non-price numbers (e.g. the timeframe "5m"), so the LAST
    literal is the price the condition contracts on.
    """
    if not isinstance(condition, str):
        return None
    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", condition)
    if not matches:
        return None
    try:
        value = float(matches[-1])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _snapshot_is_complete(snapshot: Mapping[str, Any]) -> bool:
    """``data_quality.status`` must be 'complete' (design §5.3 step 1)."""
    dq = snapshot.get("data_quality")
    if not isinstance(dq, Mapping):
        return False
    return str(dq.get("status")) == "complete"


def _tf_health(snapshot: Mapping[str, Any], tf: str) -> Mapping[str, Any] | None:
    """tf-specific market-data health (``health_by_tf`` primary, ``health`` alias)."""
    dq = snapshot.get("data_quality")
    if not isinstance(dq, Mapping):
        return None
    health_by_tf = dq.get("health_by_tf")
    if isinstance(health_by_tf, Mapping) and isinstance(health_by_tf.get(tf), Mapping):
        return health_by_tf[tf]
    legacy = dq.get("health")
    if isinstance(legacy, Mapping) and isinstance(legacy.get(tf), Mapping):
        return legacy[tf]
    return None


def _snapshot_last_close(snapshot: Mapping[str, Any], tf: str) -> float | None:
    """Latest closed candle price for ``tf`` (plan-tf module first, then primary).

    Fail-closed: missing / non-finite / non-positive prices yield None so the
    caller cannot prove price invalidation is clear.
    """
    tf_modules = snapshot.get("timeframe_modules")
    if isinstance(tf_modules, Mapping):
        tf_mod = tf_modules.get(tf)
        if isinstance(tf_mod, Mapping):
            pa = tf_mod.get("price_action")
            if isinstance(pa, Mapping):
                price = pa.get("last_close")
                if price is not None:
                    try:
                        value = float(price)
                    except (TypeError, ValueError):
                        value = None
                    if value is not None and math.isfinite(value) and value > 0:
                        return value
    modules = snapshot.get("modules")
    if isinstance(modules, Mapping):
        pa = modules.get("price_action")
        if isinstance(pa, Mapping):
            price = pa.get("last_close")
            if price is not None:
                try:
                    value = float(price)
                except (TypeError, ValueError):
                    value = None
                if value is not None and math.isfinite(value) and value > 0:
                    return value
    return None


def _later_opposing_structure(
    snapshot: Mapping[str, Any],
    side: str,
    after_ms: int,
) -> bool:
    """True if a closed structure event opposing ``side`` closed strictly after
    ``after_ms`` anywhere in the current snapshot (design §5.4
    opposite_structure; intrabar forming candles never count, closed must be
    strictly True)."""
    opposing = "bullish" if side == "SHORT" else "bearish"
    modules = snapshot.get("modules")
    tf_modules = snapshot.get("timeframe_modules")

    def _events_of(container: Any) -> list[Any]:
        if not isinstance(container, Mapping):
            return []
        events = container.get("structure_events")
        return events if isinstance(events, list) else []

    def _scan(events: list[Any]) -> bool:
        for ev in events:
            if not isinstance(ev, Mapping):
                continue
            if ev.get("closed") is not True:
                continue
            close_time = _strict_positive_int_ms(
                ev.get("candle_close_time") or ev.get("close_time")
            )
            if close_time is None or close_time <= after_ms:
                continue
            if _structure_event_direction(ev) == opposing:
                return True
        return False

    if isinstance(modules, Mapping):
        for module_key in ("price_action", "smc"):
            if _scan(_events_of(modules.get(module_key))):
                return True
    if isinstance(tf_modules, Mapping):
        for tf_mod in tf_modules.values():
            if not isinstance(tf_mod, Mapping):
                continue
            for module_key in ("price_action", "smc"):
                if _scan(_events_of(tf_mod.get(module_key))):
                    return True
    return False


def _source_event_found(
    source_snapshot: Mapping[str, Any] | None,
    confirmation: Mapping[str, Any],
) -> bool:
    """The original event existed EXACTLY: re-run the trusted extraction
    (08-10 P2-3, fresh reviewer P2).

    ``_extract_structured_entry_confirmation`` is the single source of truth
    (design.md §5.3 step 6): the source snapshot IS the snapshot the event was
    extracted from, so re-extraction MUST reproduce the event field-for-field.
    A snapshot that merely shares the symbol / analysis time but has DRIFTED
    (event removed or edited, wrong snapshot id attached) fails closed —
    provenance is exact, never a weak ``analysis >= close`` heuristic, so a
    carried state can never resurrect an event the source no longer contains.
    """
    if not isinstance(source_snapshot, Mapping):
        return False
    direction = str(confirmation.get("direction") or "").lower()
    side = {"bullish": "LONG", "bearish": "SHORT"}.get(direction)
    if side is None:
        return False
    # The extractor's ``entry`` argument is unused (legacy) but mandatory.
    re_extracted = _extract_structured_entry_confirmation(
        source_snapshot, side, float(confirmation.get("price") or 0.0)
    )
    if not isinstance(re_extracted, Mapping):
        return False
    cand_close = _strict_positive_int_ms(re_extracted.get("candle_close_time"))
    try:
        cand_price = float(re_extracted.get("price"))
    except (TypeError, ValueError):
        return False
    want_close = _strict_positive_int_ms(confirmation.get("candle_close_time"))
    try:
        want_price = float(confirmation.get("price"))
    except (TypeError, ValueError):
        return False
    if (
        cand_close is None
        or want_close is None
        or not math.isfinite(cand_price)
        or not math.isfinite(want_price)
    ):
        return False
    return (
        str(re_extracted.get("event_type") or "") == str(confirmation.get("event_type") or "")
        and str(re_extracted.get("timeframe") or "") == str(confirmation.get("timeframe") or "")
        and str(re_extracted.get("direction") or "").lower() == direction
        and cand_close == want_close
        and math.isclose(cand_price, want_price, rel_tol=0.0, abs_tol=1e-9)
        and str(re_extracted.get("source") or "") == str(confirmation.get("source") or "")
        and str(re_extracted.get("symbol") or "") == str(confirmation.get("symbol") or "")
    )


@dataclass(frozen=True)
class LifecycleResolution:
    """design.md §5.1 internal lifecycle envelope (system-owned, never LLM)."""

    contract_version: int = 1
    status: str = "absent"
    origin: str | None = None
    confirmation: Mapping[str, Any] | None = None
    source_decision_id: int | None = None
    source_snapshot_id: int | None = None
    source_analysis_time: int | None = None
    validated_at: int = 0
    age_bars: int | None = None
    ttl_bars: int | None = None
    checks: Mapping[str, bool] = field(default_factory=dict)
    invalidation_reason: str | None = None


def _invalidated(
    reason: str,
    checks: Mapping[str, bool],
    *,
    origin: str | None,
    confirmation: Mapping[str, Any] | None,
    source_decision_id: int | None = None,
    source_snapshot_id: int | None = None,
    source_analysis_time: int | None = None,
) -> LifecycleResolution:
    """Fail-closed invalidated envelope with the given check snapshot."""
    return LifecycleResolution(
        status="invalidated",
        origin=origin,
        confirmation=confirmation,
        source_decision_id=source_decision_id,
        source_snapshot_id=source_snapshot_id,
        source_analysis_time=source_analysis_time,
        validated_at=_now_ms(),
        age_bars=None,
        ttl_bars=None,
        checks=dict(checks),
        invalidation_reason=reason,
    )


def _evaluate_candidate(
    *,
    confirmation: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    policy: Any,
    analysis_time: int,
    origin: str,
    source_event_found: bool,
    source_snapshot: Mapping[str, Any] | None,
    source_decision_id: int | None,
    source_snapshot_id: int | None,
) -> LifecycleResolution:
    """Pure per-candidate lifecycle evaluation (design §5.3 steps 7-10).

    All 7 envelope checks are evaluated; status falls out of the first failing
    fail-closed gate. ``origin`` distinguishes the just-observed current event
    (age 0, no persisted owner yet) from a carried history event.
    """
    side = str(plan.get("side") or "")
    tf = str(confirmation.get("timeframe") or "")
    symbol = str(confirmation.get("symbol") or "")
    snap_symbol = str(snapshot.get("symbol") or "")
    direction = str(confirmation.get("direction") or "").lower()
    event_close = _strict_positive_int_ms(confirmation.get("candle_close_time"))

    checks = {key: False for key in _ENVELOPE_CHECK_KEYS}
    checks["same_symbol"] = symbol == snap_symbol
    checks["same_side"] = _expected_direction(side) == direction
    checks["source_event_found"] = bool(source_event_found)

    bar_ms = _BAR_MS.get(tf)
    ttl_bars = (policy.confirmation_ttl_bars or {}).get(tf)
    hard_max_bars = (policy.confirmation_hard_max_bars or {}).get(tf)

    # Unknown timeframe or policy row: cannot even count bars (fail-closed).
    if event_close is None or bar_ms is None or ttl_bars is None or hard_max_bars is None:
        return _invalidated(
            "data_gap", checks, origin=origin, confirmation=confirmation,
            source_decision_id=source_decision_id,
            source_snapshot_id=source_snapshot_id,
        )

    age_bars = (analysis_time - event_close) // bar_ms
    if age_bars < 0:
        # future/unclosed event that slipped past the caller's filters
        return _invalidated(
            "data_gap", checks, origin=origin, confirmation=confirmation,
            source_decision_id=source_decision_id,
            source_snapshot_id=source_snapshot_id,
        )

    # Provability gates (design §5.4 data_gap).
    if not (checks["same_symbol"] and checks["same_side"] and checks["source_event_found"]):
        return _invalidated(
            "data_gap", checks, origin=origin, confirmation=confirmation,
            source_decision_id=source_decision_id,
            source_snapshot_id=source_snapshot_id,
        )

    # Closed-bar age counting with gap detection: the health row for the
    # event's timeframe must prove exactly ``age_bars`` complete bars closed
    # since the event (expected last close = event_close + age_bars*bar_ms).
    expected_last_close = event_close + age_bars * bar_ms
    health = _tf_health(snapshot, tf)
    checks["closed_bar_sequence_complete"] = bool(
        health is not None
        and health.get("ready") is True
        and health.get("last_close_time") is not None
        and _strict_positive_int_ms(health["last_close_time"]) == expected_last_close
    )
    if not checks["closed_bar_sequence_complete"]:
        return _invalidated(
            "data_gap", checks, origin=origin, confirmation=confirmation,
            source_decision_id=source_decision_id,
            source_snapshot_id=source_snapshot_id,
        )

    # Geometry: final entry/trigger relative tolerance (design §5.4).
    try:
        entry = float(plan.get("entry_price"))
        entry_finite = math.isfinite(entry)
    except (TypeError, ValueError):
        entry = float("nan")
        entry_finite = False
    try:
        event_price = float(confirmation.get("price"))
        event_finite = math.isfinite(event_price) and event_price > 0
    except (TypeError, ValueError):
        event_price = float("nan")
        event_finite = False
    checks["geometry_ok"] = bool(
        entry_finite
        and event_finite
        and abs(entry - event_price) / event_price * 100.0
        <= float(policy.max_entry_deviation_pct)
    )

    # Opposite structure: a later closed BOS/CHOCH opposing the plan (design
    # §5.4; intrabar wicks / forming candles never count).
    checks["opposite_structure_absent"] = not _later_opposing_structure(
        snapshot, side, event_close
    )

    # Price invalidation against current closed facts (design §5.4: a closed
    # candle crossing the structured condition; absence of a condition clears).
    level = _parse_invalidation_level(plan.get("invalid_condition"))
    last_close = _snapshot_last_close(snapshot, tf)
    if level is None:
        checks["price_invalidation_clear"] = True
    elif last_close is None:
        checks["price_invalidation_clear"] = False
    elif side == "SHORT":
        checks["price_invalidation_clear"] = last_close < level
    else:
        checks["price_invalidation_clear"] = last_close > level

    source_analysis_time = None
    if isinstance(source_snapshot, Mapping):
        source_analysis_time = _strict_positive_int_ms(
            source_snapshot.get("analysis_time_utc")
        )

    if age_bars > int(ttl_bars):
        status, invalidation_reason = "expired", None
    elif not checks["opposite_structure_absent"]:
        status, invalidation_reason = "invalidated", "opposite_structure"
    elif not checks["price_invalidation_clear"]:
        status, invalidation_reason = "invalidated", "price_invalidation"
    elif not checks["geometry_ok"]:
        status, invalidation_reason = "invalidated", "geometry_mismatch"
    else:
        status, invalidation_reason = "valid", None

    return LifecycleResolution(
        status=status,
        origin=origin,
        confirmation=confirmation,
        source_decision_id=source_decision_id,
        source_snapshot_id=source_snapshot_id,
        source_analysis_time=source_analysis_time,
        validated_at=_now_ms(),
        age_bars=age_bars,
        ttl_bars=int(ttl_bars),
        checks=checks,
        invalidation_reason=invalidation_reason,
    )


def resolve_trusted_entry_confirmation(
    repo: Any,
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    policy: Any,
) -> LifecycleResolution:
    """Deterministic lifecycle resolver (design §5.3).

    System-owned, accepts no LLM values. A current same-side event decides
    UNCONDITIONALLY (fail-closed): its resolution is returned with
    ``origin=current_snapshot`` even when it fails validation — a current event
    never falls through to the carried-history path, so a stale but
    geometrically-convenient older event cannot resurrect a state the newest
    market fact contradicts. Only when NO current event exists does the newest
    persisted same-symbol/same-direction event whose close lies in the
    hard-max bar window carry, if it passes every check. Returns a structured
    ``LifecycleResolution`` envelope; ``status == valid`` is the only state the
    pipeline may bind into a plan (``_bind_trusted_entry_confirmation``
    contract), and this resolver never writes.
    """
    symbol = str(snapshot.get("symbol") or "")
    analysis_time = _strict_positive_int_ms(snapshot.get("analysis_time_utc"))
    side = str(plan.get("side") or "")
    if not symbol or analysis_time is None or side not in ("LONG", "SHORT"):
        raise ValueError(
            "entry_confirmation lifecycle: snapshot must carry a symbol, a "
            "strict positive analysis_time_utc and a LONG/SHORT plan side "
            f"(got symbol={symbol!r} analysis={analysis_time!r} side={side!r})"
        )
    now_ms = _now_ms()

    # 1. Data quality is a precondition for ANY resolution (design §5.3 step 1).
    if not _snapshot_is_complete(snapshot):
        checks = {key: False for key in _ENVELOPE_CHECK_KEYS}
        checks["same_symbol"] = True
        checks["same_side"] = True
        return LifecycleResolution(
            status="invalidated",
            origin=None,
            confirmation=None,
            validated_at=now_ms,
            checks=checks,
            invalidation_reason="data_gap",
        )

    # 2-4. Current event wins (strict provenance; origin=current_snapshot).
    entry_price = plan.get("entry_price")
    try:
        entry_float = float(entry_price) if entry_price is not None else None
    except (TypeError, ValueError):
        entry_float = None
    current = _extract_structured_entry_confirmation(
        snapshot, side, entry_float or 0.0
    )
    if current is not None:
        return _evaluate_candidate(
            confirmation=current,
            snapshot=snapshot,
            plan=plan,
            policy=policy,
            analysis_time=analysis_time,
            origin="current_snapshot",
            source_event_found=True,
            source_snapshot=None,
            source_decision_id=None,
            source_snapshot_id=None,
        )

    # 5-10. Carried: newest persisted event within the hard-max window.
    window_ms = max(
        _BAR_MS[tf] * int((policy.confirmation_hard_max_bars or {}).get(tf, 0))
        for tf in _LIFECYCLE_TIMEFRAMES
    )
    since_ms = analysis_time - window_ms
    rows = repo.list_recent_entry_confirmation_events(
        symbol=symbol,
        direction=_expected_direction(side),
        since_ms=since_ms,
        limit=50,
    )
    candidates = [
        row
        for row in rows
        if _strict_positive_int_ms(row.get("event_close_time")) is not None
        and int(row["event_close_time"]) < analysis_time
        and str(row.get("timeframe")) in _BAR_MS
    ]
    if not candidates:
        return LifecycleResolution(
            status="absent",
            validated_at=now_ms,
            checks={key: False for key in _ENVELOPE_CHECK_KEYS},
        )

    # Deterministic order: newest close first, higher timeframe first on an
    # equal close, then fingerprint. The NEWEST VALID candidate decides
    # (design §5.3 step 10): an invalidated / expired / data-gap newest
    # candidate is SKIPPED and does not block an older valid event, but a
    # valid candidate is returned immediately and an older event never
    # resurrects a state the newest contradicts.
    candidates.sort(
        key=lambda row: (
            -int(row["event_close_time"]),
            _TIMEFRAME_PRIORITY.get(str(row.get("timeframe")), 9),
            str(row.get("event_fingerprint") or ""),
        )
    )
    source_cache: dict[int, Mapping[str, Any] | None] = {}
    first: LifecycleResolution | None = None
    for row in candidates:
        confirmation: Mapping[str, Any] = {
            "type": "closed_candle_confirmation",
            "timeframe": str(row["timeframe"]),
            "event_type": str(row["event_type"]),
            "direction": str(row["direction"]),
            "candle_close_time": int(row["event_close_time"]),
            "price": float(row["event_price"]),
            "source": str(row["source"]),
            "symbol": str(row["symbol"]),
        }
        snap_id = row.get("source_snapshot_id")
        if snap_id is not None and int(snap_id) not in source_cache:
            source_cache[int(snap_id)] = repo.get_market_snapshot_for_confirmation(
                int(snap_id)
            )
        source_snapshot = (
            source_cache.get(int(snap_id)) if snap_id is not None else None
        )
        result = _evaluate_candidate(
            confirmation=confirmation,
            snapshot=snapshot,
            plan=plan,
            policy=policy,
            analysis_time=analysis_time,
            origin="carried_forward",
            source_event_found=_source_event_found(source_snapshot, confirmation),
            source_snapshot=source_snapshot,
            source_decision_id=row.get("source_decision_id"),
            source_snapshot_id=int(snap_id) if snap_id is not None else None,
        )
        if result.status == "valid":
            return result
        if first is None:
            first = result
    return first if first is not None else LifecycleResolution(
        status="absent",
        validated_at=now_ms,
        checks={key: False for key in _ENVELOPE_CHECK_KEYS},
    )
