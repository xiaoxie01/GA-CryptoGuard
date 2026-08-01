from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from plugins.crypto_guard.config.loader import PLUGIN_ROOT


SCHEMA_DIR = PLUGIN_ROOT / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    with (SCHEMA_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_json(name: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        jsonschema.validate(payload, load_schema(name))
        return True, None
    except Exception as exc:
        return False, str(exc)


def validate_json_detail(name: str, payload: dict[str, Any]) -> tuple[bool, str, str]:
    """07-31 P1-4: validate and return BOTH a compact and a full error form.

    Returns ``(ok, compact, full)``:

    - ``ok``: schema passes.
    - ``compact``: ``"/".join(absolute_path) + ": " + message`` (fallback
      ``<root>`` when the path is empty) — a single-line field-path + type
      description that fits the Feishu recent-failure ``llm_error[:100]``
      display slice. NEVER carries the multi-line jsonschema traceback.
    - ``full``: the complete ``str(exc)`` jsonschema traceback (with
      ``Failed validating ...`` lines) preserved for the ``llm_error_detail``
      audit field.
    """
    try:
        jsonschema.validate(payload, load_schema(name))
        return True, "", ""
    except Exception as exc:
        path = exc.absolute_path if isinstance(exc, jsonschema.ValidationError) else ()
        loc = "/".join(str(p) for p in path) or "<root>"
        message = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
        compact = f"{loc}: {message}"
        return False, compact, str(exc)


def no_edge_decision(
    symbol: str,
    reason: str,
    *,
    analysis_time_utc: int,
    timeframe_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Phase G (07-05): ``analysis_time_utc`` is keyword-only and required so
    # callers cannot silently drop the snapshot-authoritative analysis time.
    # The schema requires ``analysis_time_utc: integer, minimum=1`` — passing
    # 0 or a wall-clock fallback would re-introduce the original defect
    # (schema-invalid no_edge fallback crashing the chain on the second
    # validate_json call). Callers must source this from snapshot.
    if not isinstance(analysis_time_utc, int) or isinstance(analysis_time_utc, bool):
        raise TypeError(
            "no_edge_decision requires a strict positive integer analysis_time_utc"
        )
    if analysis_time_utc <= 0:
        raise ValueError(
            "no_edge_decision requires analysis_time_utc > 0; got "
            f"{analysis_time_utc}. The caller must pass snapshot-authoritative "
            "time; wall-clock fallbacks are forbidden."
        )
    # R1-3 (07-03 final review): include the structured fields required by
    # the tightened ga_decision.schema.json so the no_edge fallback is
    # schema-valid. When timeframe_context is supplied (snapshot-derived),
    # use it verbatim; otherwise fall back to unknown/closed=False markers
    # for each required TF so schema validation still passes.
    if timeframe_context is not None:
        tf_ctx = dict(timeframe_context)
    else:
        tf_ctx = {
            tf: {"bias": "unknown", "structure": "unknown", "closed": False, "close_time": 0}
            for tf in ("1d", "4h", "1h", "15m")
        }
    return {
        "symbol": symbol,
        "decision": "no_edge",
        "signal_grade": "D",
        "market_bias": "neutral",
        "trend_stage": "unknown",
        "confidence": 0.0,
        "summary": f"当前输出未通过校验，降级为 no_edge：{reason}",
        "evidence": [],
        "counter_evidence": [reason],
        "risk_notes": ["不构成实盘建议，仅用于模拟盘与策略研究。"],
        "has_trade_plan": False,
        "trade_plan": None,
        "opportunity_watch": None,
        "suggested_actions": ["ignore"],
        "analysis_time_utc": int(analysis_time_utc),
        "timeframe_context": tf_ctx,
        "alignment": "unknown",
        "htf_conflict": False,
        "market_reason_codes": ["schema_validation_failed"],
    }


# Phase B (07-09): alias normalization for entry_trigger_confirmation.type.
# The schema enum only allows "closed_candle_confirmation", but the LLM
# frequently emits semantic trigger-style aliases ("price_rejection",
# "pullback_rejection", "breakout_retest", "reclaim_confirmation") that
# would otherwise hard-fail schema validation. As long as the rest of the
# confirmation object is complete and internally consistent (correct
# symbol, non-future candle_close_time, schema-allowed source/event_type),
# the alias is semantically equivalent to closed_candle_confirmation and
# is repaired here. The original alias is preserved in audit_notes for
# traceability. Anything that cannot be repaired is returned as None with
# a reason list - the caller treats that as a hard schema failure.
_ENTRY_TRIGGER_CONFIRMATION_ALIASES = frozenset(
    {
        "price_rejection",
        "pullback_rejection",
        "breakout_retest",
        "reclaim_confirmation",
        "closed_candle_confirmation",
    }
)
_ENTRY_TRIGGER_CONFIRMATION_REQUIRED_FIELDS = (
    "type",
    "timeframe",
    "event_type",
    "direction",
    "candle_close_time",
    "price",
    "source",
    "symbol",
)
_ENTRY_TRIGGER_CONFIRMATION_ALLOWED_TIMEFRAMES = frozenset(
    {"1m", "5m", "15m", "1h", "4h"}
)
_ENTRY_TRIGGER_CONFIRMATION_ALLOWED_EVENT_TYPES = frozenset(
    {"BOS", "CHOCH", "RECLAIM", "BREAKOUT_RETEST"}
)
_ENTRY_TRIGGER_CONFIRMATION_ALLOWED_DIRECTIONS = frozenset(
    {"bullish", "bearish"}
)
_ENTRY_TRIGGER_CONFIRMATION_ALLOWED_SOURCES = frozenset(
    {"price_action", "smc", "deterministic_rule"}
)


def normalize_entry_trigger_confirmation(
    confirmation: Any,
    *,
    decision_symbol: str,
    analysis_time_utc: int,
) -> tuple[dict[str, Any] | None, list[str], bool]:
    """Normalize alias ``entry_trigger_confirmation.type`` to the schema enum.

    Returns ``(normalized_confirmation_or_None, audit_notes, changed_flag)``.
    """
    if not isinstance(confirmation, dict):
        return None, ["confirmation_not_dict"], False

    audit_notes: list[str] = []
    raw_type = confirmation.get("type")
    if not isinstance(raw_type, str):
        return None, ["confirmation_type_missing_or_not_string"], False

    original_alias: str | None = None
    if raw_type == "closed_candle_confirmation":
        normalized_type = raw_type
    elif raw_type in _ENTRY_TRIGGER_CONFIRMATION_ALIASES:
        original_alias = raw_type
        normalized_type = "closed_candle_confirmation"
    else:
        return None, [f"confirmation_type_unknown:{raw_type}"], False

    missing = [
        f for f in _ENTRY_TRIGGER_CONFIRMATION_REQUIRED_FIELDS if f not in confirmation
    ]
    if missing:
        return None, [f"confirmation_missing_field:{f}" for f in missing], False

    tf_value = confirmation.get("timeframe")
    if not isinstance(tf_value, str) or tf_value not in _ENTRY_TRIGGER_CONFIRMATION_ALLOWED_TIMEFRAMES:
        return None, [f"confirmation_timeframe_invalid:{tf_value!r}"], False

    event_type_value = confirmation.get("event_type")
    if not isinstance(event_type_value, str) or event_type_value not in _ENTRY_TRIGGER_CONFIRMATION_ALLOWED_EVENT_TYPES:
        return None, [f"confirmation_event_type_invalid:{event_type_value!r}"], False

    direction_value = confirmation.get("direction")
    if not isinstance(direction_value, str) or direction_value not in _ENTRY_TRIGGER_CONFIRMATION_ALLOWED_DIRECTIONS:
        return None, [f"confirmation_direction_invalid:{direction_value!r}"], False

    source_value = confirmation.get("source")
    if not isinstance(source_value, str) or source_value not in _ENTRY_TRIGGER_CONFIRMATION_ALLOWED_SOURCES:
        return None, [f"confirmation_source_invalid:{source_value!r}"], False

    symbol_value = confirmation.get("symbol")
    if not isinstance(symbol_value, str) or symbol_value != decision_symbol:
        return None, [f"confirmation_symbol_mismatch:{symbol_value!r}"], False

    close_time_value = confirmation.get("candle_close_time")
    if isinstance(close_time_value, bool) or not isinstance(close_time_value, int) or close_time_value <= 0:
        return None, ["confirmation_candle_close_time_invalid"], False
    analysis_time_int = int(analysis_time_utc)
    if close_time_value > analysis_time_int:
        return None, ["confirmation_candle_close_time_in_future"], False

    price_value = confirmation.get("price")
    if isinstance(price_value, bool) or not isinstance(price_value, (int, float)) or price_value <= 0:
        return None, ["confirmation_price_invalid"], False

    # Construct the normalized confirmation from ONLY the schema-declared
    # fields. ``ga_decision.schema.json`` marks ``entry_trigger_confirmation``
    # as ``additionalProperties: false`` (07-09 R4 recommended #1), so any
    # extra field the LLM emits alongside the alias (e.g. ``confirmation_style``,
    # ``note``, ``entry_trigger_type``) would cause re-validation to fail and
    # the repair SOP would silently fall through to hard-fail deterministic
    # fallback — defeating the entire 07-09 task. By reconstructing the dict
    # from the required-fields tuple, extras are stripped before re-validation.
    normalized = {k: confirmation[k] for k in _ENTRY_TRIGGER_CONFIRMATION_REQUIRED_FIELDS}
    normalized["type"] = normalized_type
    if original_alias is not None:
        audit_notes.append(original_alias)
        audit_notes.append(
            f"entry_trigger_confirmation_alias_repaired:{original_alias}->closed_candle_confirmation"
        )
        return normalized, audit_notes, True
    return normalized, audit_notes, False


# Phase-2 D (07-27): ``suggested_actions`` schema-alias repair. The JSON
# Schema (ga_decision.schema.json:72-74) declares ``suggested_actions`` as a
# FLAT array of strings from a 5-value enum:
#   create_paper_order | create_opportunity_watch | add_to_watchlist |
#   ignore | monitor_only
# The schema is NOT loosened here. The observed production defect is that the
# LLM sometimes emits ``decision``-enum values
# (``wait_for_breakout``/``wait_for_reclaim``/``avoid_chop``) inside
# ``suggested_actions``, or a nested-array variant
# (``[['monitor_only'], ['wait_for_breakout']]``). Those are schema-invalid
# (not in the 5-value enum). Rather than blindly filtering the raw list
# against the enum (which would keep ``monitor_only`` and drop the decision-
# enum values, ignoring the LLM's INTENT — e.g. ``wait_for_breakout`` should
# map to ``add_to_watchlist``, not be silently dropped), the canonical list
# is REBUILT from the decision semantics. The rebuild mapping (verbatim from
# the authoritative review):
#   executable plan (has_trade_plan and trade_plan, or decision==
#     trade_plan_available) -> create_paper_order
#   opportunity watch (non-empty dict opportunity_watch) ->
#     create_opportunity_watch
#   wait_for_* (decision in {wait_for_pullback, wait_for_breakout,
#     wait_for_reclaim}) -> add_to_watchlist
#   no_edge / avoid_chop -> ignore
#   other / fallback -> monitor_only
# The order matters: executable plan is checked first (most specific), then
# opportunity watch, then wait_for_*, then no_edge/avoid_chop, then the
# monitor_only fallback. Empty ``opportunity_watch={}`` is NOT a real watch
# (P2-1 07-27 final review) — it falls through so wait_for_* / ignore /
# monitor_only can still win. The raw list is NOT the source of truth — the
# decision semantics are. ``monitor_only`` is always a valid fallback so the
# function always returns a non-None list.
_SUGGESTED_ACTIONS_CANONICAL = frozenset(
    {
        "create_paper_order",
        "create_opportunity_watch",
        "add_to_watchlist",
        "ignore",
        "monitor_only",
    }
)
_SUGGESTED_ACTIONS_WAIT_FOR_DECISIONS = frozenset(
    {
        "wait_for_pullback",
        "wait_for_breakout",
        "wait_for_reclaim",
    }
)
_SUGGESTED_ACTIONS_IGNORE_DECISIONS = frozenset(
    {
        "no_edge",
        "avoid_chop",
    }
)


def normalize_suggested_actions(
    raw: Any,
    *,
    decision: Any,
    has_trade_plan: Any,
    trade_plan: Any,
    opportunity_watch: Any,
) -> tuple[list[str] | None, list[str], bool]:
    """Normalize ``suggested_actions`` to a schema-valid canonical list.

    Returns ``(canonical_list_or_None, audit_notes, changed_flag)`` mirroring
    ``normalize_entry_trigger_confirmation``. The canonical list is REBUILT
    from the decision semantics (not filtered from the raw list) so the LLM's
    INTENT is preserved even when the raw list carries illegal decision-enum
    values. ``monitor_only`` is always a valid fallback so the function always
    returns a non-None list.
    """
    # --- REBUILD the canonical list from decision semantics ---
    # The decision semantics (has_trade_plan / trade_plan / opportunity_watch
    # / decision) are the source of truth. The raw list is only used to detect
    # whether a repair is needed (changed flag) — it is NOT filtered against
    # the enum (filtering would silently drop the LLM's intent, e.g.
    # ``wait_for_breakout`` should map to ``add_to_watchlist``, not be
    # silently dropped).
    canonical: list[str]
    if has_trade_plan and trade_plan:
        canonical = ["create_paper_order"]
    # P2-1 (07-27 final review): only a non-empty dict is a real opportunity
    # watch. ``opportunity_watch={}`` is schema-legal but carries no usable
    # watch payload — treating ``is not None`` as truth over-fired
    # ``create_opportunity_watch`` and skipped the more accurate wait_for_* ->
    # ``add_to_watchlist`` branch. ``None`` / missing / ``{}`` all fall through.
    elif isinstance(opportunity_watch, dict) and opportunity_watch:
        canonical = ["create_opportunity_watch"]
    elif isinstance(decision, str) and decision in _SUGGESTED_ACTIONS_WAIT_FOR_DECISIONS:
        canonical = ["add_to_watchlist"]
    elif isinstance(decision, str) and decision in _SUGGESTED_ACTIONS_IGNORE_DECISIONS:
        canonical = ["ignore"]
    else:
        canonical = ["monitor_only"]

    # --- Determine whether a repair is needed (changed flag) ---
    # ``changed=True`` when the raw value was NOT already the exact canonical
    # list (either schema-invalid, a different list, nested-array, or a
    # non-list). ``changed=False`` only when the raw was already the canonical
    # list (already valid + canonical — no repair).
    raw_is_list = isinstance(raw, list)
    raw_is_flat_valid = (
        raw_is_list
        and all(isinstance(x, str) and x in _SUGGESTED_ACTIONS_CANONICAL for x in raw)
        and not any(isinstance(x, list) for x in raw)
    )
    if raw_is_flat_valid and raw == canonical:
        changed = False
    else:
        changed = True

    # --- Build audit notes ONLY when a repair actually happened ---
    # (mirrors ``normalize_entry_trigger_confirmation`` which returns an empty
    # notes list when the confirmation is already canonical).
    audit_notes: list[str] = []
    if changed:
        if not raw_is_list:
            audit_notes.append("suggested_actions_not_list")
        else:
            # Detect a nested-array variant (items that are themselves lists).
            for _item in raw:
                if isinstance(_item, list):
                    audit_notes.append("suggested_actions_nested_array_flattened")
                    break
        audit_notes.append(f"suggested_actions_rebuilt_from_decision:{decision}")

    return canonical, audit_notes, changed
