"""Feedback TTL/Decay management.

States: fresh (0-30 days) → decayed (30-90 days) → archived (>90 days)

Protection:
- Feedback referenced by active strategy_patches never archived
- Recent 30 days: full weight in context builder
- 30-90 days: decayed weight (0.5x)
- >90 days: archived, excluded from context unless referenced
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.feedback_ttl")

# TTL thresholds
FRESH_DAYS = 30
DECAYED_DAYS = 90


def apply_feedback_ttl(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Apply TTL/decay to skill_feedback_memory entries.

    Returns:
        {
            ok: bool,
            transitions: {fresh_to_decayed, decayed_to_archived, protected},
            summary: {fresh, decayed, archived, total},
        }
    """
    # Check schema health first
    schema = check_schema_health()
    if not schema["ok"]:
        return {
            "ok": False,
            "error": "schema_unhealthy",
            "missing_columns": schema["missing_columns"],
        }

    now = datetime.now(timezone.utc)
    fresh_cutoff = (now - timedelta(days=FRESH_DAYS)).isoformat().replace("+00:00", "Z")
    decayed_cutoff = (now - timedelta(days=DECAYED_DAYS)).isoformat().replace("+00:00", "Z")

    # Get feedback IDs referenced by active strategy_patches
    protected_ids = _get_protected_feedback_ids(repo)

    # Count current state
    current_counts = _count_feedback_by_status(repo)

    # Transition fresh → decayed (30-90 days, not protected)
    fresh_to_decayed = repo.conn.execute(
        """
        UPDATE skill_feedback_memory
        SET status = 'decayed', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'fresh' AND datetime(created_at) < datetime(?) AND datetime(created_at) >= datetime(?)
        AND id NOT IN (SELECT value FROM json_each(?))
        """,
        (fresh_cutoff, decayed_cutoff, json.dumps(protected_ids)),
    ).rowcount

    # Transition decayed → archived (>90 days, not protected)
    decayed_to_archived = repo.conn.execute(
        """
        UPDATE skill_feedback_memory
        SET status = 'archived', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'decayed' AND datetime(created_at) < datetime(?)
        AND id NOT IN (SELECT value FROM json_each(?))
        """,
        (decayed_cutoff, json.dumps(protected_ids)),
    ).rowcount

    # Also archive entries still in 'candidate' or 'active' status that are >90 days old
    # (entries that were never transitioned)
    stale_to_archived = repo.conn.execute(
        """
        UPDATE skill_feedback_memory
        SET status = 'archived', updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('candidate', 'active') AND datetime(created_at) < datetime(?)
        AND id NOT IN (SELECT value FROM json_each(?))
        """,
        (decayed_cutoff, json.dumps(protected_ids)),
    ).rowcount

    repo.conn.commit()

    # Count new state
    new_counts = _count_feedback_by_status(repo)

    LOGGER.info(
        "Feedback TTL applied: fresh_to_decayed=%d, decayed_to_archived=%d, stale_to_archived=%d, protected=%d",
        fresh_to_decayed, decayed_to_archived, stale_to_archived, len(protected_ids),
    )

    return {
        "ok": True,
        "transitions": {
            "fresh_to_decayed": fresh_to_decayed,
            "decayed_to_archived": decayed_to_archived,
            "stale_to_archived": stale_to_archived,
            "protected": len(protected_ids),
        },
        "summary": new_counts,
        "previous_summary": current_counts,
    }


def get_feedback_with_ttl_weight(
    repo: CryptoGuardRepository,
    *,
    limit: int = 100,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Get feedback entries with TTL weight for context builder.

    Weight mapping:
    - fresh (0-30 days): 1.0
    - decayed (30-90 days): 0.5
    - archived (>90 days): 0.0 (excluded unless include_archived=True)
    """
    status_filter = ""
    if not include_archived:
        status_filter = "AND status != 'archived'"

    rows = repo.conn.execute(
        f"""
        SELECT id, skill_name, pattern_type, finding, status, created_at,
               CASE
                   WHEN status = 'fresh' THEN 1.0
                   WHEN status = 'decayed' THEN 0.5
                   WHEN status = 'archived' THEN 0.0
                   ELSE 1.0
               END as ttl_weight
        FROM skill_feedback_memory
        WHERE 1=1 {status_filter}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [dict(row) for row in rows]


def _get_protected_feedback_ids(repo: CryptoGuardRepository) -> list[int]:
    """Get feedback IDs referenced by active strategy_patches.

    These feedback entries should never be archived.
    """
    # Get all active/candidate patches
    active_patches = repo.conn.execute(
        """
        SELECT id, evidence_json, patch_json
        FROM strategy_patches
        WHERE status IN ('active', 'candidate', 'draft')
        """
    ).fetchall()

    protected_ids: set[int] = set()
    for patch in active_patches:
        # Parse both evidence_json and patch_json
        for json_field in ("evidence_json", "patch_json"):
            json_str = patch[json_field]
            if not json_str:
                continue
            try:
                import json
                data = json.loads(json_str) if isinstance(json_str, str) else json_str
                if isinstance(data, dict):
                    # Look for feedback_ids (list)
                    feedback_refs = data.get("feedback_ids")
                    if isinstance(feedback_refs, list):
                        for fid in feedback_refs:
                            if fid is not None:
                                try:
                                    protected_ids.add(int(fid))
                                except (ValueError, TypeError):
                                    pass

                    # Look for feedback_id (single)
                    feedback_id = data.get("feedback_id")
                    if feedback_id is not None:
                        try:
                            protected_ids.add(int(feedback_id))
                        except (ValueError, TypeError):
                            pass

                    # Look for source_feedback_ids (list)
                    source_refs = data.get("source_feedback_ids")
                    if isinstance(source_refs, list):
                        for fid in source_refs:
                            if fid is not None:
                                try:
                                    protected_ids.add(int(fid))
                                except (ValueError, TypeError):
                                    pass
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    return list(protected_ids)


def _count_feedback_by_status(repo: CryptoGuardRepository) -> dict[str, int]:
    """Count feedback entries by status."""
    rows = repo.conn.execute(
        """
        SELECT status, COUNT(*) as count
        FROM skill_feedback_memory
        GROUP BY status
        """
    ).fetchall()

    counts = {row["status"]: row["count"] for row in rows}
    counts["total"] = sum(counts.values())
    return counts
