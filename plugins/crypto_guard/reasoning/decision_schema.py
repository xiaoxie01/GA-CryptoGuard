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


def no_edge_decision(symbol: str, reason: str) -> dict[str, Any]:
    # R1-3 (07-03 final review): include the structured fields required by
    # the tightened ga_decision.schema.json so the no_edge fallback is
    # schema-valid. All TFs are marked unknown/closed=False because the
    # decision was produced without a valid snapshot.
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
        "timeframe_context": tf_ctx,
        "alignment": "unknown",
        "htf_conflict": False,
        "market_reason_codes": ["schema_validation_failed"],
    }
