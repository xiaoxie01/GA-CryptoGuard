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
