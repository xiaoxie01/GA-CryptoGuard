"""P0-3 (08-02): structured opportunity-watch condition normalization.

Production baseline (audit-confirmed 2026-08-02): 158/190 decisions carried
an ``opportunity_watch`` whose ``conditions`` were bare strings (e.g.
"15M 收盘突破上沿或跌破下沿") and ``invalid_condition`` was a text blob
or the pseudo-kind ``risk_rejected``. The opportunity watcher
(``scheduler/opportunity_watcher.py:_condition_hit``) only understands
structured condition objects (type/kind + side + level), so every text
condition silently waited forever — the watch never triggered and the alert
never enqueued. 92 decisions declared ``create_opportunity_watch`` but the
``opportunity_watches`` table stayed empty.

P0-3 PRD contract (verbatim): ga_decision schema MUST forbid bare-string
``opportunity_watch.conditions``; conditions must be the objects the watcher
actually supports; ``invalid_condition`` must also be structured; no
regex/LLM free-text translator to fabricate conditions; if the LLM gives no
valid structure, build deterministically from ``candidate_trade_plan``
(pullback/breakout/reclaim + stop invalidation); if unbuildable, fail-closed
(no auto watch) and emit structured diagnostics.

This module is the SINGLE shared normalizer used by all three decision
paths so the schema, the deterministic SOP (ga_judge), the risk gate
(risk_engine) and the LLM adapter (llm_agent_judge) cannot drift apart:
  - ``ga_judge._build_sop_watch`` (deterministic SOP watch builders)
  - ``risk_engine.default_watch_from_decision``
  - ``llm_agent_judge._normalize_llm_decision`` watch block
  - ``llm_agent_judge._try_repair_opportunity_watch`` (schema-repair chain)
"""

from __future__ import annotations

from typing import Any

# The watcher kinds ``opportunity_watcher._condition_hit`` actually supports
# (price/close level crossings, pullback, breakout, reclaim).
# ga_decision.schema.json and this module MUST stay in lockstep.
#
# 08-02 Codex P0 (terminal-review round 2): ``cvd_confirmation`` is REMOVED.
# ``opportunity_watcher._condition_hit`` only compared the condition's own
# persisted ``flow_confirmation`` string (never the real analysis-time
# order-flow), so a LONG cvd watch fired immediately on a static match and
# every other value never fired. No real analysis-time-aligned order-flow read
# exists, so the kind is dropped (conservative): it is no longer supported, the
# normalizer drops it and rebuilds price conditions from the trade plan, and an
# existing cvd watch is flagged untriggerable by the diagnostic — the persisted
# ``flow_confirmation`` can never masquerade as live CVD.
SUPPORTED_WATCH_CONDITION_KINDS = frozenset({
    "price_below", "close_below", "price_above", "close_above",
    "pullback", "breakout", "reclaim",
})

# Structured watch envelope keys (the watcher reads these).
_WATCH_KEYS = frozenset({
    "needed", "direction", "reason", "conditions", "invalid_condition",
    "expires_minutes",
})

# Keys a single structured condition object may carry. The normalizer drops
# anything else so a condition can never smuggle a free-text blob through.
# 08-02 P2-2 (fresh reviewer): ``direction``/``symbol`` are REMOVED — they are
# schema-forbidden inside a condition (ga_decision.schema.json condition items
# are ``additionalProperties: false``); they only live at the watch-envelope
# (direction) / decision (symbol) level.
_CONDITION_KEYS = frozenset({
    "type", "kind", "side", "timeframe", "level", "price",
    "tolerance_pct",
})

# Codex P1-3: the EXACT key set ga_decision.schema.json allows inside a
# condition item / invalid_condition (``additionalProperties: false``). This is
# STRICTER than ``_CONDITION_KEYS``: ``kind`` (the LLM-friendly alias the
# normalizer canonicalizes to ``type``) is NOT schema-allowable, so a
# kind-carrying watch must not short-circuit the repair. ``is_structured_watch``
# (the schema-repair short-circuit) must equal schema-valid + watcher-valid,
# so it gates on this set.
_SCHEMA_CONDITION_KEYS = frozenset({
    "type", "side", "timeframe", "level", "price",
    "tolerance_pct",
})

_SUPPORTED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h", "4h"})
_SIDES = frozenset({"LONG", "SHORT"})

_DEFAULT_EXPIRES_MINUTES = 240
_DEFAULT_TIMEFRAME = "15m"


def is_structured_condition(cond: Any) -> bool:
    """True when ``cond`` is a dict with a watcher-supported kind and a
    usable trigger field (a positive number ``level``/``price``).

    08-02 Codex P0 (terminal-review round 2): ``cvd_confirmation`` is no longer
    a supported kind (see ``SUPPORTED_WATCH_CONDITION_KINDS``), so the old
    non-empty-string ``flow_confirmation``/``value`` trigger branch is removed
    — a cvd kind now fails the ``kind not in SUPPORTED`` check below and is
    dropped by the normalizer (rebuilt as price conditions from the trade
    plan). A persisted ``flow_confirmation`` can never stand in for live CVD.
    """
    if not isinstance(cond, dict):
        return False
    kind = str(cond.get("type") or cond.get("kind") or "").lower()
    if kind not in SUPPORTED_WATCH_CONDITION_KINDS:
        return False
    level = cond.get("level")
    if level is None:
        level = cond.get("price")
    return isinstance(level, (int, float)) and not isinstance(level, bool) and float(level) > 0


def is_structured_invalid_condition(cond: Any) -> bool:
    """True when ``cond`` is None (no invalidation) or a structured,
    watcher-supported condition dict."""
    if cond is None:
        return True
    return is_structured_condition(cond)


def _is_schema_condition(cond: Any, direction: str) -> bool:
    """Codex P1-3: strict per-condition schema + watcher checks.

    This is the STRICT predicate used ONLY by the schema-repair short-circuit
    (``is_structured_watch``). Unlike ``is_structured_condition`` (which stays
    PERMISSIVE about extra keys so the normalizer's keep-loop can clean them),
    it requires the condition to be exactly what ga_decision.schema.json and
    the watcher need:

      - every key inside the schema's condition property set
        (``additionalProperties: false`` — rejects ``note``, ``kind``, ...);
      - ``side`` equals the watch ``direction`` (``opportunity_watcher.
        _condition_hit`` reads ``condition.side`` FIRST and falls back to the
        watch direction, so a mismatched side evaluates the OPPOSITE
        direction);
      - ``timeframe`` is one of the schema enum (1m/5m/15m/1h/4h) — an
        illegal value would make ``_watch_timeframe`` query a bogus timeframe;
      - numeric ``tolerance_pct`` is a non-negative number (schema
        ``number, minimum 0``);
      - ``level``/``price`` are non-negative numbers (schema ``number,
        minimum 0``). ``is_structured_condition`` only checks the FIRST usable
        field, so a condition carrying a valid ``level`` PLUS a garbage
        ``price`` (string / negative / bool) slipped through the pre-fix
        strict predicate and short-circuited the repair (Codex P2-2).

    ``flow_confirmation``/``value`` are NO LONGER schema keys (08-02 R2 P2-2):
    ga_decision.schema.json removed them from the condition property set and
    tightened ``anyOf`` to level/price only, so a persisted
    ``flow_confirmation`` can never be a watch trigger field again. Any
    condition carrying them fails the key-set check below.
    """
    if not isinstance(cond, dict):
        return False
    if not _SCHEMA_CONDITION_KEYS.issuperset(cond.keys()):
        return False
    if str(cond.get("side") or "").upper() != direction:
        return False
    if "timeframe" in cond and str(cond["timeframe"]) not in _SUPPORTED_TIMEFRAMES:
        return False
    if "tolerance_pct" in cond:
        tol = cond["tolerance_pct"]
        if not isinstance(tol, (int, float)) or isinstance(tol, bool) or float(tol) < 0:
            return False
    for key in ("level", "price"):
        if key in cond:
            v = cond[key]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or float(v) < 0:
                return False
    return True


def is_structured_watch(watch: Any) -> bool:
    """True when the watch already satisfies the structured watcher contract:
    a dict with a LONG/SHORT direction, every condition structured, and a
    structured-or-None invalid_condition. Used by the schema-repair chain to
    skip cosmetic rewrites of already-valid watches.

    08-02 P0-3: ``is_structured_watch`` is the schema-repair short-circuit, so
    "structured" must mean SCHEMA-valid (ga_decision.schema.json requires
    ``side`` on every condition item AND on the invalid_condition object). A
    condition/invalid that is usable by the watcher but missing ``side`` is
    NOT schema-valid and must NOT short-circuit the repair — the repair chain
    rebuilds it via ``normalize_opportunity_watch`` (which injects the
    resolved direction as ``side``).

    08-02 P2-2 (fresh reviewer): SCHEMA-valid also forbids ``direction`` /
    ``symbol`` inside a condition (``additionalProperties: false``), so a
    condition that smuggles either must NOT short-circuit — the repair chain
    drops the keys via ``_clean_condition``.

    Codex P1-3 (terminal review): the short-circuit must equal SCHEMA-valid +
    watcher-valid, so it ALSO rejects (each via ``_is_schema_condition``):
    any envelope key outside the schema property set (``note``, ...), any
    condition/invalid key outside the schema condition property set, an
    illegal ``timeframe``, a non-numeric/negative ``tolerance_pct``, and a
    condition/invalid ``side`` that differs from ``watch.direction`` (the
    watcher evaluates the condition's own side first — a mismatch flips the
    direction). 08-02 R2 P2-2: ``value``/``flow_confirmation`` were REMOVED
    from the schema condition key set entirely (they were dead string-trigger
    fields), so a condition carrying either fails the key-set check regardless
    of type — no separate type check is needed. The repair chain
    (``normalize_opportunity_watch`` + ``_clean_condition``) then CLEANS or
    fail-closes instead of persisting a schema-invalid watch.

    Codex P2-3 (terminal-review rework): ``expires_minutes`` is schema
    ``["integer", "null"]``, so a string/float/bool value must NOT
    short-circuit the repair (pre-fix it did -> a schema-invalid watch was
    persisted). The repair path coerces garbage to ``_DEFAULT_EXPIRES_MINUTES``.

    Fresh-reviewer round 2 P2: the envelope key ``invalid_condition`` is
    schema-REQUIRED (ga_decision.schema.json:64) even though its VALUE may be
    null (``["object","null"]`` = "no invalidation"). An absent key must not
    short-circuit the repair via ``is_structured_invalid_condition(None)``
    (pre-fix it did -> a schema-invalid watch could persist raw).
    """
    if not isinstance(watch, dict):
        return False
    # Envelope ``additionalProperties: false``: no key outside the schema set.
    if not _WATCH_KEYS.issuperset(watch.keys()):
        return False
    # Codex P1 (terminal-review round 2): ``needed`` must be True for a
    # MATERIALIZABLE structured watch — the worker auto-materialize and manual
    # button gates all short-circuit on ``is_structured_watch``, so a
    # ``needed`` absent / False / non-bool must fall through to the repair
    # chain (which never materializes a non-True-needed watch). This is the
    # single gate that keeps the "needed must be True" contract consistent
    # across schema, normalizer, repository write, and both materialization
    # paths.
    if watch.get("needed") is not True:
        return False
    # Codex P1: ``reason`` is schema ``["string", "null"]`` — an absent key,
    # None, or a str is fine; any other type (int/float/bool/list/dict) must
    # fall through to the repair so a schema-invalid reason is never persisted.
    reason = watch.get("reason")
    if reason is not None and not isinstance(reason, str):
        return False
    # ``invalid_condition`` is schema-required (round-2 P2): the KEY must be
    # present even when its value is null ("no invalidation"). An absent key
    # falls through to the repair, which always emits it.
    if "invalid_condition" not in watch:
        return False
    # ``expires_minutes`` is schema ``["integer", "null", minimum 1]`` (P2-3 +
    # Codex P1). Python's bool is an int subclass, so it must be excluded
    # explicitly; a 0/negative value is not a positive integer and must not
    # short-circuit the repair (which coerces it to the default 240).
    expires = watch.get("expires_minutes")
    if expires is not None and (not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0):
        return False
    direction = str(watch.get("direction") or "").upper()
    if direction not in _SIDES:
        return False
    conds = watch.get("conditions")
    if not isinstance(conds, list) or not conds:
        return False
    for cond in conds:
        if not is_structured_condition(cond):
            return False
        if not _is_schema_condition(cond, direction):
            return False
    invalid = watch.get("invalid_condition")
    if isinstance(invalid, dict):
        if not is_structured_condition(invalid):
            return False
        if not _is_schema_condition(invalid, direction):
            return False
    return is_structured_invalid_condition(invalid)


def build_conditions_from_trade_plan(trade_plan: Any) -> list[dict[str, Any]]:
    """Deterministically derive watch conditions from a trade plan.

    Never fabricates from free text: only the plan's structured numbers and
    confirmation events are used. Returns ``[]`` when no usable side/level
    exists (caller fail-closes).

    Mapping:
      - trigger entry   -> ``breakout`` through the trigger price
      - RECLAIM / BREAKOUT_RETEST confirmation event -> ``reclaim`` of the level
      - limit / market / fallback -> ``pullback`` toward the entry level
    """
    if not isinstance(trade_plan, dict):
        return []
    side = str(trade_plan.get("side") or "").upper()
    if side not in _SIDES:
        return []
    entry_type = str(trade_plan.get("entry_type") or "limit").lower()
    confirmation = trade_plan.get("entry_trigger_confirmation")
    event_type = (
        str((confirmation or {}).get("event_type") or "").upper()
        if isinstance(confirmation, dict) else ""
    )
    timeframe = _timeframe_from(confirmation, trade_plan)
    level = _positive_float(trade_plan.get("entry_price"))
    if level is None:
        level = _positive_float(trade_plan.get("trigger_price"))
    if level is None:
        return []
    if entry_type == "trigger":
        return [{"type": "breakout", "side": side, "level": level, "timeframe": timeframe}]
    if event_type in {"RECLAIM", "BREAKOUT_RETEST"}:
        return [{"type": "reclaim", "side": side, "level": level, "timeframe": timeframe}]
    return [{"type": "pullback", "side": side, "level": level, "timeframe": timeframe}]


def build_invalid_condition_from_trade_plan(trade_plan: Any) -> dict[str, Any] | None:
    """Deterministically derive the stop-invalidation condition from a plan.

    LONG  -> close_below stop_loss
    SHORT -> close_above stop_loss
    Returns None when the plan has no usable side/stop.
    """
    if not isinstance(trade_plan, dict):
        return None
    side = str(trade_plan.get("side") or "").upper()
    if side not in _SIDES:
        return None
    stop = _positive_float(trade_plan.get("stop_loss"))
    if stop is None:
        return None
    confirmation = trade_plan.get("entry_trigger_confirmation")
    timeframe = _timeframe_from(confirmation, trade_plan)
    kind = "close_below" if side == "LONG" else "close_above"
    return {"type": kind, "side": side, "level": stop, "timeframe": timeframe}


def normalize_opportunity_watch(
    watch: Any,
    trade_plan: Any = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize an ``opportunity_watch`` to the structured watcher contract.

    Returns ``(watch_or_None, diagnostics)``:
      - a schema-valid watch dict when any usable structure exists;
      - ``(None, diagnostics)`` on fail-closed (no direction, or no structured
        condition and no buildable plan).

    Rules (PRD):
      1. Structured LLM conditions are kept and canonicalized (kind->type,
         unknown keys dropped); text conditions are DROPPED with a diagnostic
         — never translated from free text.
      2. If no structured condition survives, build deterministically from
         ``trade_plan`` (pullback/breakout/reclaim).
      3. ``invalid_condition``: a structured dict is kept; a text blob is
         dropped and rebuilt from the plan's stop loss.
      4. If unbuildable or direction is missing, fail-closed to ``None`` and
         emit a structured diagnostic.
    """
    diagnostics: list[str] = []
    plan = trade_plan if isinstance(trade_plan, dict) and trade_plan else None
    plan_side = str((plan or {}).get("side") or "").upper()
    has_input = plan is not None or isinstance(watch, dict)

    if watch is not None and not isinstance(watch, dict):
        diagnostics.append("opportunity_watch 非对象，已丢弃。")
        watch = None

    structured_conditions: list[dict[str, Any]] = []
    if isinstance(watch, dict):
        for cond in (watch.get("conditions") or []):
            if is_structured_condition(cond):
                cleaned = _clean_condition(cond)
                # Fresh-reviewer round 3 P2 (defense-in-depth): the keep-loop
                # re-validates the CLEANED condition so a cleaning that orphans
                # every trigger field can never persist an empty, schema-invalid
                # shell — drop it (and rebuild/fail-closed below) instead.
                if is_structured_condition(cleaned):
                    structured_conditions.append(cleaned)
                else:
                    diagnostics.append(
                        f"机会监控条件清理后失去触发字段，已丢弃：{_snip(cond)}"
                    )
            else:
                diagnostics.append(
                    f"机会监控条件无法结构化，已丢弃：{_snip(cond)}"
                )
        direction = _normalize_direction(watch.get("direction"), plan_side)
    else:
        direction = plan_side

    # 2) No structured condition survived -> deterministic build from the
    # plan (pullback/breakout/reclaim). Never fabricate from free text.
    if not structured_conditions:
        structured_conditions = build_conditions_from_trade_plan(plan)
        if structured_conditions:
            diagnostics.append("机会监控条件由交易计划确定性构建（pullback/breakout/reclaim）。")
        elif has_input:
            diagnostics.append("机会监控条件无法结构化且无可用交易计划，fail-closed：不自动创建监控。")

    if direction not in _SIDES and structured_conditions:
        # A condition may carry its own side; fall back to it.
        cond_side = str(structured_conditions[0].get("side") or "").upper()
        if cond_side in _SIDES:
            direction = cond_side
    if direction not in _SIDES:
        if has_input or structured_conditions:
            diagnostics.append("机会监控方向缺失或不可用，fail-closed：不自动创建监控。")
        return None, diagnostics
    if not structured_conditions:
        if has_input:
            diagnostics.append("无可用结构化机会监控条件，fail-closed：不自动创建监控。")
        return None, diagnostics

    # ga_decision.schema.json requires ``side`` on every condition item AND on
    # the invalid_condition object. The watcher itself falls back to the watch
    # direction, so injecting the resolved direction as ``side`` is a pure
    # schema-conformance fix — no behavior change, no fabrication. Without it,
    # a condition/invalid that the watcher could evaluate (structured level,
    # no side) would fail the tightened schema.
    # Codex P1-3: the alignment is UNCONDITIONAL — a condition whose side
    # DIFFERS from the resolved direction must be aligned too, because
    # ``opportunity_watcher._condition_hit`` reads ``condition.side`` FIRST
    # (falling back to the watch direction), so a mismatched side would make
    # the watcher evaluate the OPPOSITE direction.
    for cond in structured_conditions:
        cond["side"] = direction

    # 3) invalid_condition: structured dict kept; text blob dropped and
    # rebuilt from the plan's stop loss.
    invalid: dict[str, Any] | None = None
    raw_invalid = (watch or {}).get("invalid_condition") if isinstance(watch, dict) else None
    if is_structured_invalid_condition(raw_invalid):
        if isinstance(raw_invalid, dict):
            invalid = _clean_condition(raw_invalid)
            # Fresh-reviewer round 3 P2 (defense-in-depth): never persist an
            # empty-shell invalid_condition — if cleaning orphaned every trigger
            # field, rebuild from the plan / fail-closed to None instead.
            if not is_structured_condition(invalid):
                invalid = build_invalid_condition_from_trade_plan(plan)
                diagnostics.append(
                    "invalid_condition 清理后失去触发字段，已从交易计划重建。"
                )
    else:
        invalid = build_invalid_condition_from_trade_plan(plan)
        diagnostics.append(
            f"invalid_condition 无法结构化，已从交易计划重建：{_snip(raw_invalid)}"
        )
    if isinstance(invalid, dict):
        invalid["side"] = direction

    expires = (watch or {}).get("expires_minutes") if isinstance(watch, dict) else None
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        expires = _DEFAULT_EXPIRES_MINUTES

    # Codex P1: ``reason`` is schema ``["string", "null"]``. None -> the default
    # string (kept for backward compat); str -> kept as-is; any OTHER type
    # (int/float/bool/list/dict) is repaired to the default — never coerced with
    # ``str(...)`` which would fabricate a bogus reason from a non-string.
    raw_reason = watch.get("reason") if isinstance(watch, dict) else None
    if raw_reason is None:
        reason = "等待结构确认"
    elif isinstance(raw_reason, str):
        reason = raw_reason
    else:
        reason = "等待结构确认"

    # 08-02 R2 review Finding 2 (brand-new reviewer): preserve an explicit
    # ``needed=False`` across the repair chain. ``needed`` is LLM intent —
    # "no opportunity watch wanted". The P1-1 materialization gate requires
    # ``needed is True`` (is_structured_watch), so the repair chain must NOT
    # erase a deliberate False into True: pre-fix it did, which made the gate
    # vacuous on the production LLM path (every watch that reached repair came
    # out needed=True and materialized despite the model's no-watch intent).
    # A False is kept so the normalized watch still fails is_structured_watch
    # and neither the auto gate nor the manual button materializes it. Absent /
    # None / non-bool still default to True (a rebuilt watch IS wanted).
    raw_needed = watch.get("needed") if isinstance(watch, dict) else None
    needed = False if raw_needed is False else True
    return {
        "needed": needed,
        "direction": direction,
        "reason": reason,
        "conditions": structured_conditions,
        "invalid_condition": invalid,
        "expires_minutes": expires,
    }, diagnostics


def _clean_condition(cond: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize a structured condition: kind->type, known keys only.

    08-02 P2-2 (fresh reviewer): ``direction``/``symbol`` are NOT preserved —
    ga_decision.schema.json forbids them inside a condition
    (``additionalProperties: false``); they only live at the watch-envelope
    (direction) / decision (symbol) level. Preserving them previously let a
    schema-invalid watch through whenever the repair short-circuit passed.

    Codex P1-3: schema-INVALID VALUES are repaired (dropped), never preserved —
    an illegal ``timeframe`` would fail the schema enum AND make
    ``opportunity_watcher._watch_timeframe`` evaluate a bogus timeframe (so
    the watcher falls back to its default 15m); a non-numeric/negative
    ``tolerance_pct`` would break the pullback tolerance arithmetic. The output
    is therefore always schema-valid.

    Codex P2-2 (terminal-review rework): a non-numeric/negative ``level``/
    ``price`` is DROPPED too (schema ``number, minimum 0``) — the keep-loop
    only lets a condition through with at least one usable trigger field, so
    dropping a garbage SIBLING never orphans the condition's ``anyOf``.

    08-02 R2 P2-2 (fresh reviewer): ``flow_confirmation``/``value`` are NO
    LONGER preserved — they were dead string-trigger fields for the removed
    cvd_confirmation kind. ga_decision.schema.json forbids them inside a
    condition (``additionalProperties: false`` + tightened anyOf), so a
    persisted ``flow_confirmation`` can never be a trigger field again.
    """
    out: dict[str, Any] = {}
    kind = str(cond.get("type") or cond.get("kind") or "").lower()
    out["type"] = kind
    for key in ("side", "timeframe", "level", "price",
                "tolerance_pct"):
        if key not in cond:
            continue
        val = cond[key]
        if key == "timeframe":
            if str(val) in _SUPPORTED_TIMEFRAMES:
                out[key] = val
            continue
        if key == "tolerance_pct":
            if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) >= 0:
                out[key] = val
            continue
        if key in ("level", "price"):
            if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) >= 0:
                out[key] = val
            continue
        out[key] = val
    return out


def _normalize_direction(value: Any, plan_side: str = "") -> str | None:
    if value in ("LONG", "SHORT", None):
        return value
    text = str(value).strip().lower()
    if text in {"long", "buy", "bull", "bullish", "up", "多", "做多", "看多"}:
        return "LONG"
    if text in {"short", "sell", "bear", "bearish", "down", "空", "做空", "看空"}:
        return "SHORT"
    if plan_side in _SIDES:
        return plan_side
    return None


def _timeframe_from(confirmation: Any, trade_plan: Any) -> str:
    tf = (confirmation or {}).get("timeframe") if isinstance(confirmation, dict) else None
    if tf in _SUPPORTED_TIMEFRAMES:
        return str(tf)
    return _DEFAULT_TIMEFRAME


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _snip(value: Any, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "SUPPORTED_WATCH_CONDITION_KINDS",
    "is_structured_condition",
    "is_structured_invalid_condition",
    "is_structured_watch",
    "build_conditions_from_trade_plan",
    "build_invalid_condition_from_trade_plan",
    "normalize_opportunity_watch",
]
