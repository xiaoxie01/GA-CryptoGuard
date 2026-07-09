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
