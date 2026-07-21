from __future__ import annotations

import json
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def latest_decision_summaries(repo: CryptoGuardRepository, *, limit: int = 80) -> list[dict[str, Any]]:
    rows = repo.latest_ga_decisions_by_symbol(limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        # Phase C (07-03): prefer rendered_summary (canonical) over
        # final_summary for downstream consumers. rendered_summary is the
        # deterministic canonical text; final_summary is kept as a fallback.
        summary = row.get("rendered_summary") or row["final_summary"]
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
                "final_summary": summary,
                "rendered_summary": row.get("rendered_summary"),
                "raw_llm_summary": _raw(row).get("raw_llm_summary"),
                "risk_check": _safe_json(row.get("risk_check_json"), {}),
                "feishu_actions": _safe_json(row.get("feishu_actions_json"), []),
            }
        )
    return out


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    return _safe_json(row.get("raw_decision_json"), {})


def _safe_json(raw: Any, default: Any) -> Any:
    # PG cutover: psycopg returns JSONB columns already decoded to a Python
    # dict/list (NOT a str). The legacy ``json.loads(raw)`` raised
    # ``TypeError: the JSON object must be str, ... not dict`` on that shape,
    # and the broad ``except`` silently fell back to ``default`` — losing the
    # real nested data (``raw_llm_summary`` audit text, ``risk_check``,
    # ``feishu_actions``). Accept the decoded shape directly; only parse the
    # str/bytes shape (SQLite/legacy TEXT columns). Mirrors
    # storage.repository._decode_json.
    if raw is None:
        return default
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    try:
        return json.loads(raw or json.dumps(default))
    except Exception:
        return default
