"""State consistency diagnostics for CryptoGuard evolution system.

Detects:
- Orphan patches (strategy_patches with no matching strategy_version)
- Status mismatches (trigger/patch/version state inconsistencies)
- Stale shadows (candidates in shadow_testing >7 days with no new samples)
- Draft limbo (patches in draft >72 hours)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.state_diagnostics")


def diagnose_state_consistency(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Run all state consistency checks.

    Returns:
        {
            ok: bool,
            issues: [{type, severity, details, suggested_action}],
            summary: {orphan_patches, status_mismatches, stale_shadows, draft_limbo}
        }
    """
    issues: list[dict[str, Any]] = []

    issues.extend(_check_orphan_patches(repo))
    issues.extend(_check_status_mismatches(repo))
    issues.extend(_check_stale_shadows(repo))
    issues.extend(_check_draft_limbo(repo))

    summary = {
        "orphan_patches": len([i for i in issues if i["type"] == "orphan_patch"]),
        "status_mismatches": len([i for i in issues if i["type"] == "status_mismatch"]),
        "stale_shadows": len([i for i in issues if i["type"] == "stale_shadow"]),
        "draft_limbo": len([i for i in issues if i["type"] == "draft_limbo"]),
    }

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
    }


def _check_orphan_patches(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find strategy_patches with no matching strategy_version."""
    issues: list[dict[str, Any]] = []

    orphans = repo.conn.execute(
        """
        SELECT sp.id, sp.strategy_name, sp.candidate_version, sp.status, sp.created_at
        FROM strategy_patches sp
        LEFT JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        WHERE sv.id IS NULL
        """
    ).fetchall()

    for row in orphans:
        issues.append({
            "type": "orphan_patch",
            "severity": "warning",
            "details": {
                "patch_id": row["id"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "patch_status": row["status"],
                "created_at": row["created_at"],
            },
            "suggested_action": "Delete orphan patch or create matching strategy_version",
        })

    return issues


def _check_status_mismatches(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find trigger/patch/version state inconsistencies."""
    issues: list[dict[str, Any]] = []

    # Check: evolution_triggers 'pending' but corresponding patch is 'rejected'
    mismatches = repo.conn.execute(
        """
        SELECT et.id as trigger_id, et.trigger_type, et.status as trigger_status,
               sp.id as patch_id, sp.candidate_version, sp.status as patch_status
        FROM evolution_triggers et
        JOIN strategy_patches sp ON sp.trigger_id = et.id
        WHERE et.status = 'pending' AND sp.status = 'rejected'
        """
    ).fetchall()

    for row in mismatches:
        issues.append({
            "type": "status_mismatch",
            "severity": "error",
            "details": {
                "trigger_id": row["trigger_id"],
                "trigger_type": row["trigger_type"],
                "trigger_status": row["trigger_status"],
                "patch_id": row["patch_id"],
                "candidate_version": row["candidate_version"],
                "patch_status": row["patch_status"],
                "mismatch": "trigger_pending_but_patch_rejected",
            },
            "suggested_action": "Reject trigger or reset patch status",
        })

    # Check: strategy_version 'active' but trigger still 'pending' (should be resolved)
    active_pending = repo.conn.execute(
        """
        SELECT sv.id as version_id, sv.strategy_name, sv.version, sv.status as version_status,
               et.id as trigger_id, et.status as trigger_status
        FROM strategy_versions sv
        JOIN strategy_patches sp ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        JOIN evolution_triggers et ON sp.trigger_id = et.id
        WHERE sv.status = 'active' AND et.status = 'pending'
        """
    ).fetchall()

    for row in active_pending:
        issues.append({
            "type": "status_mismatch",
            "severity": "warning",
            "details": {
                "version_id": row["version_id"],
                "strategy_name": row["strategy_name"],
                "version": row["version"],
                "version_status": row["version_status"],
                "trigger_id": row["trigger_id"],
                "trigger_status": row["trigger_status"],
                "mismatch": "version_active_but_trigger_pending",
            },
            "suggested_action": "Mark trigger as resolved",
        })

    return issues


def _check_stale_shadows(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find candidates in shadow_testing >7 days with no new samples."""
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=7)

    # Get all shadow_testing candidates (using created_at since updated_at doesn't exist)
    candidates = repo.conn.execute(
        "SELECT strategy_name, version, created_at FROM strategy_versions WHERE status = 'shadow_testing'"
    ).fetchall()

    for row in candidates:
        created_at_str = row["created_at"]
        if not created_at_str:
            continue

        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at < stale_threshold:
                # Check if there are new samples since last update
                latest_eval = repo.conn.execute(
                    "SELECT MAX(created_at) as latest FROM strategy_evaluations WHERE strategy_name=? AND strategy_version=? AND is_shadow=1",
                    (row["strategy_name"], row["version"]),
                ).fetchone()

                latest_eval_at = latest_eval["latest"] if latest_eval else None
                if not latest_eval_at or datetime.fromisoformat(latest_eval_at.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) < stale_threshold:
                    issues.append({
                        "type": "stale_shadow",
                        "severity": "warning",
                        "details": {
                            "strategy_name": row["strategy_name"],
                            "candidate_version": row["version"],
                            "created_at": created_at_str,
                            "days_stale": (now - created_at).days,
                        },
                        "suggested_action": "Reject stale candidate or investigate why no new samples",
                    })
        except (ValueError, TypeError):
            continue

    return issues


def _check_draft_limbo(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find patches in draft status >72 hours (human approval timeout)."""
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    limbo_threshold = now - timedelta(hours=72)

    drafts = repo.conn.execute(
        "SELECT id, strategy_name, candidate_version, created_at FROM strategy_patches WHERE status = 'draft'"
    ).fetchall()

    for row in drafts:
        created_at_str = row["created_at"]
        if not created_at_str:
            continue

        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at < limbo_threshold:
                issues.append({
                    "type": "draft_limbo",
                    "severity": "warning",
                    "details": {
                        "patch_id": row["id"],
                        "strategy_name": row["strategy_name"],
                        "candidate_version": row["candidate_version"],
                        "created_at": created_at_str,
                        "hours_in_draft": int((now - created_at).total_seconds() / 3600),
                    },
                    "suggested_action": "Approve, reject, or escalate draft patch",
                })
        except (ValueError, TypeError):
            continue

    return issues
