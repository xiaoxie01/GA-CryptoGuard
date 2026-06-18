from __future__ import annotations

import json
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def latest_decision_summaries(repo: CryptoGuardRepository, *, limit: int = 80) -> list[dict[str, Any]]:
    rows = repo.latest_ga_decisions_by_symbol(limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "ga_decision_id": row["id"],
                "symbol": row["symbol"],
                "analysis_time": row["analysis_time"],
                "decision": row["decision"],
                "legacy_decision": _raw(row).get("legacy_decision"),
                "signal_grade": row["signal_grade"],
                "confidence": row["confidence"],
                "market_bias": row["market_bias"],
                "trend_stage": row["trend_stage"],
                "final_summary": row["final_summary"],
                "risk_check": _safe_json(row.get("risk_check_json"), {}),
                "feishu_actions": _safe_json(row.get("feishu_actions_json"), []),
            }
        )
    return out


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    return _safe_json(row.get("raw_decision_json"), {})


def _safe_json(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw or json.dumps(default))
    except Exception:
        return default
