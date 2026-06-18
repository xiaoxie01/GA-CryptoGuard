"""Account-level feedback rules dry-run evaluator.

Loads feedback_rules.yaml from the account_risk skill directory and matches
backfilled evolution_trigger feedback entries against `when` conditions.
Outputs matches with `would_apply` action and explanatory details, but does
NOT execute any strategy or risk changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.account_feedback_rules_dry_run")

ACCOUNT_RISK_RULES_PATH = Path(__file__).parent.parent / "skills" / "account_risk" / "feedback_rules.yaml"


def evaluate_account_feedback_rules_dry_run(
    repo: CryptoGuardRepository,
    *,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Evaluate account-level feedback rules against backfilled evolution_trigger entries.

    Returns:
        {
            ok: bool,
            matches: [{rule_when, rule_action, description, event_count, ...}],
            summary: {total_matches, unique_event_count, by_pattern, by_action},
            rules_loaded: int,
            events_checked: int,
        }
    """
    # Schema health guard
    schema = check_schema_health()
    if not schema["ok"]:
        return {
            "ok": False,
            "error": "schema_unhealthy",
            "missing_columns": schema["missing_columns"],
        }

    # Load account-level rules
    rules = _load_account_rules()
    if not rules:
        return {
            "ok": True,
            "matches": [],
            "summary": {"total_matches": 0, "unique_event_count": 0, "by_pattern": {}, "by_action": {}},
            "rules_loaded": 0,
            "events_checked": 0,
        }

    # Apply lookback filter
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")

    # Query structured evolution_trigger feedback
    rows = repo.conn.execute(
        """
        SELECT id, skill_name, pattern_type, finding, suggested_adjustment_json, created_at
        FROM skill_feedback_memory
        WHERE source_type = 'evolution_trigger'
          AND pattern_type IS NOT NULL AND pattern_type != ''
          AND datetime(created_at) >= datetime(?)
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()

    # Group by pattern_type
    events_by_pattern: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pt = row["pattern_type"]
        events_by_pattern.setdefault(pt, []).append(dict(row))

    # For each rule, find matching events and enrich with context
    matches: list[dict[str, Any]] = []
    for rule in rules:
        when = rule["when"]
        matching_events = events_by_pattern.get(when, [])
        if not matching_events:
            continue

        # Collect all patch_ids
        patch_ids: list[int] = []
        for ev in matching_events:
            adj = ev.get("suggested_adjustment_json")
            if adj:
                try:
                    adj_data = json.loads(adj) if isinstance(adj, str) else adj
                    pid = adj_data.get("candidate_patch_id")
                    if pid is not None:
                        patch_ids.append(int(pid))
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

        # Infer symbols/sides from all related trades
        inferred_symbols, inferred_sides = _infer_affected_context(repo, patch_ids)

        matches.append({
            "rule_when": when,
            "rule_action": rule["action"],
            "description": rule.get("description", ""),
            "event_count": len(matching_events),
            "patch_count": len(set(patch_ids)),
            "sample_patch_ids": list(set(patch_ids))[:10],
            "inferred_symbols": inferred_symbols,
            "inferred_sides": inferred_sides,
            "would_apply": True,
            "params": rule.get("params", {}),
        })

    # Aggregate
    by_pattern: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for m in matches:
        by_pattern[m["rule_when"]] = by_pattern.get(m["rule_when"], 0) + m["event_count"]
        by_action[m["rule_action"]] = by_action.get(m["rule_action"], 0) + m["event_count"]

    unique_event_count = sum(len(events) for events in events_by_pattern.values())

    return {
        "ok": True,
        "matches": matches,
        "summary": {
            "total_matches": sum(m["event_count"] for m in matches),
            "unique_event_count": unique_event_count,
            "by_pattern": by_pattern,
            "by_action": by_action,
        },
        "rules_loaded": len(rules),
        "events_checked": len(rows),
    }


def _load_account_rules() -> list[dict[str, Any]]:
    """Load feedback_rules.yaml from account_risk skill."""
    if not ACCOUNT_RISK_RULES_PATH.exists():
        LOGGER.warning("Account risk rules not found: %s", ACCOUNT_RISK_RULES_PATH)
        return []

    try:
        with open(ACCOUNT_RISK_RULES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "feedback_rules" not in data:
            return []

        rules = data["feedback_rules"]
        if not isinstance(rules, list):
            return []

        parsed: list[dict[str, Any]] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = rule.get("when")
            action = rule.get("action")
            if when and action:
                entry: dict[str, Any] = {
                    "when": str(when),
                    "action": str(action),
                }
                if rule.get("description"):
                    entry["description"] = str(rule["description"])
                if rule.get("params") and isinstance(rule["params"], dict):
                    entry["params"] = rule["params"]
                parsed.append(entry)

        return parsed

    except Exception as exc:
        LOGGER.warning("Failed to load account risk rules: %s", exc)
        return []


def _infer_affected_context(
    repo: CryptoGuardRepository,
    patch_ids: list[int],
) -> tuple[list[str], list[str]]:
    """Infer affected symbols and sides from related trades via evolution_triggers."""
    if not patch_ids:
        return [], []

    symbols: set[str] = set()
    sides: set[str] = set()

    # Process in batches to avoid SQLite variable limit
    batch_size = 200
    for i in range(0, len(patch_ids), batch_size):
        batch = patch_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = repo.conn.execute(
            f"""
            SELECT DISTINCT et.related_trade_ids
            FROM strategy_patches sp
            JOIN evolution_triggers et ON et.id = sp.trigger_id
            WHERE sp.id IN ({placeholders})
              AND et.related_trade_ids IS NOT NULL
              AND et.related_trade_ids != '[]'
            """,
            batch,
        ).fetchall()

        trade_ids: list[int] = []
        for row in rows:
            try:
                ids = json.loads(row["related_trade_ids"])
                trade_ids.extend(int(tid) for tid in ids if tid is not None)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        if not trade_ids:
            continue

        # Get symbols/sides from paper_trades
        for j in range(0, len(trade_ids), batch_size):
            trade_batch = trade_ids[j:j + batch_size]
            trade_placeholders = ",".join("?" for _ in trade_batch)
            trade_rows = repo.conn.execute(
                f"""
                SELECT DISTINCT symbol, side
                FROM paper_trades
                WHERE id IN ({trade_placeholders})
                """,
                trade_batch,
            ).fetchall()

            for tr in trade_rows:
                if tr["symbol"]:
                    symbols.add(tr["symbol"])
                if tr["side"]:
                    sides.add(tr["side"])

    return sorted(symbols), sorted(sides)
