"""State consistency diagnostics for CryptoGuard evolution system.

Detects:
- Orphan patches (strategy_patches with no matching strategy_version)
- Status mismatches (trigger/patch/version state inconsistencies)
- Stale shadows (candidates in shadow_testing >7 days with no new samples)
- Draft limbo (patches in draft >72 hours)
- Duplicate open trades (same order_id with multiple open paper_trades)
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
            summary: {orphan_patches, status_mismatches, stale_shadows, draft_limbo, duplicate_patches}
        }
    """
    issues: list[dict[str, Any]] = []

    issues.extend(_check_orphan_patches(repo))
    issues.extend(_check_status_mismatches(repo))
    issues.extend(_check_stale_shadows(repo))
    issues.extend(_check_draft_limbo(repo))
    issues.extend(_check_duplicate_patches(repo))
    issues.extend(_check_duplicate_open_trades(repo))
    issues.extend(_check_candidate_queue_overflow(repo))
    issues.extend(_check_stalled_candidate(repo))
    issues.extend(_check_no_real_pnl_progress(repo))
    issues.extend(_check_strategy_name_mismatch(repo))
    issues.extend(_check_zero_quantity_virtual_trades(repo))
    issues.extend(_check_zero_risk_virtual_trades(repo))
    issues.extend(_check_three_table_status_mismatch(repo))
    issues.extend(_check_closed_vt_missing_real_pnl_eval(repo))
    issues.extend(_check_ambiguous_vt_missing_ambiguous_eval(repo))
    issues.extend(_check_ambiguous_eval_not_real_pnl(repo))
    issues.extend(_check_duplicate_vt_per_candidate_decision(repo))
    issues.extend(_check_closed_vt_still_processed(repo))
    issues.extend(_check_cursor_regression(repo))
    issues.extend(_check_illegal_status_transitions(repo))
    issues.extend(_check_active_eval_missing_ga_decision_id(repo))
    issues.extend(_check_paper_order_missing_active_eval(repo))
    issues.extend(_check_closed_trade_missing_active_real_pnl(repo))
    issues.extend(_check_shadow_candidate_legacy_only_samples(repo))

    summary = {
        "orphan_patches": len([i for i in issues if i["type"] == "orphan_patch"]),
        "status_mismatches": len([i for i in issues if i["type"] == "status_mismatch"]),
        "stale_shadows": len([i for i in issues if i["type"] == "stale_shadow"]),
        "draft_limbo": len([i for i in issues if i["type"] == "draft_limbo"]),
        "duplicate_patches": len([i for i in issues if i["type"] == "duplicate_patch"]),
        "duplicate_open_trades": len([i for i in issues if i["type"] == "duplicate_open_trade"]),
        "candidate_queue_overflow": len([i for i in issues if i["type"] == "candidate_queue_overflow"]),
        "stalled_candidate": len([i for i in issues if i["type"] == "stalled_candidate"]),
        "no_real_pnl_progress": len([i for i in issues if i["type"] == "no_real_pnl_progress"]),
        "strategy_name_mismatch": len([i for i in issues if i["type"] == "strategy_name_mismatch"]),
        "zero_quantity_vt": len([i for i in issues if i["type"] == "zero_quantity_virtual_trade"]),
        "zero_risk_vt": len([i for i in issues if i["type"] == "zero_risk_virtual_trade"]),
        "three_table_status_mismatch": len([i for i in issues if i["type"] == "three_table_status_mismatch"]),
        "closed_vt_missing_real_pnl": len([i for i in issues if i["type"] == "closed_vt_missing_real_pnl"]),
        "ambiguous_vt_missing_ambiguous_eval": len([i for i in issues if i["type"] == "ambiguous_vt_missing_ambiguous_eval"]),
        "ambiguous_eval_not_real_pnl": len([i for i in issues if i["type"] == "ambiguous_eval_not_real_pnl"]),
        "duplicate_vt_per_candidate_decision": len([i for i in issues if i["type"] == "duplicate_vt_per_candidate_decision"]),
        "closed_vt_still_processed": len([i for i in issues if i["type"] == "closed_vt_still_processed"]),
        "cursor_regression": len([i for i in issues if i["type"] == "cursor_regression"]),
        "illegal_status_transition": len([i for i in issues if i["type"] == "illegal_status_transition"]),
        "active_eval_missing_ga_decision_id": len([i for i in issues if i["type"] == "active_eval_missing_ga_decision_id"]),
        "paper_order_missing_active_eval": len([i for i in issues if i["type"] == "paper_order_missing_active_eval"]),
        "closed_trade_missing_active_real_pnl": len([i for i in issues if i["type"] == "closed_trade_missing_active_real_pnl"]),
        "shadow_candidate_legacy_only": len([i for i in issues if i["type"] == "shadow_candidate_legacy_only"]),
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
        WHERE sv.id IS NULL AND sp.status NOT IN ('duplicate', 'rejected')
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

    # Check: active patch with deprecated strategy_version
    active_patch_deprecated_version = repo.conn.execute(
        """
        SELECT sp.id as patch_id, sp.strategy_name, sp.candidate_version, sp.status as patch_status,
               sv.id as version_id, sv.status as version_status
        FROM strategy_patches sp
        JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        WHERE sp.status = 'active' AND sv.status = 'deprecated'
        """
    ).fetchall()

    for row in active_patch_deprecated_version:
        issues.append({
            "type": "status_mismatch",
            "severity": "error",
            "details": {
                "patch_id": row["patch_id"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "patch_status": row["patch_status"],
                "version_id": row["version_id"],
                "version_status": row["version_status"],
                "mismatch": "active_patch_but_deprecated_version",
            },
            "suggested_action": "Deprecate the patch or reactivate the version",
        })

    return issues


def _check_duplicate_patches(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find duplicate patches (same strategy_name + candidate_version) that are not already soft-cleaned."""
    issues: list[dict[str, Any]] = []

    duplicates = repo.conn.execute(
        """
        SELECT strategy_name, candidate_version, COUNT(*) as count,
               GROUP_CONCAT(id) as patch_ids, GROUP_CONCAT(status) as statuses
        FROM strategy_patches
        WHERE status NOT IN ('duplicate', 'rejected', 'deprecated')
        GROUP BY strategy_name, candidate_version
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for row in duplicates:
        issues.append({
            "type": "duplicate_patch",
            "severity": "error",
            "details": {
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "duplicate_count": row["count"],
                "patch_ids": row["patch_ids"],
                "statuses": row["statuses"],
            },
            "suggested_action": "Mark older duplicates as duplicate/rejected, keep the latest",
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


def _check_duplicate_open_trades(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find orders that have multiple open paper_trades (distorts equity/PnL)."""
    issues: list[dict[str, Any]] = []

    duplicates = repo.conn.execute(
        """
        SELECT order_id, symbol, COUNT(*) as open_count,
               GROUP_CONCAT(id) as trade_ids,
               GROUP_CONCAT(entry_price) as entry_prices,
               GROUP_CONCAT(quantity) as quantities,
               GROUP_CONCAT(created_at) as created_ats
        FROM paper_trades
        WHERE closed_at IS NULL
        GROUP BY order_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for row in duplicates:
        issues.append({
            "type": "duplicate_open_trade",
            "severity": "error",
            "details": {
                "order_id": row["order_id"],
                "symbol": row["symbol"],
                "open_count": row["open_count"],
                "trade_ids": row["trade_ids"],
                "entry_prices": row["entry_prices"],
                "quantities": row["quantities"],
                "created_ats": row["created_ats"],
            },
            "suggested_action": "Close all but the oldest open trade for this order; verify equity correction",
        })

    return issues


def _check_candidate_queue_overflow(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find strategy_names with more than 5 shadow_testing candidates."""
    issues: list[dict[str, Any]] = []

    overflow = repo.conn.execute(
        """
        SELECT strategy_name, COUNT(*) as cnt, GROUP_CONCAT(version) as versions
        FROM strategy_versions
        WHERE status = 'shadow_testing'
        GROUP BY strategy_name
        HAVING COUNT(*) > 5
        """
    ).fetchall()

    for row in overflow:
        issues.append({
            "type": "candidate_queue_overflow",
            "severity": "warning",
            "details": {
                "strategy_name": row["strategy_name"],
                "candidate_count": row["cnt"],
                "versions": row["versions"],
            },
            "suggested_action": "Run _enforce_candidate_cap() to reject excess candidates",
        })

    return issues


def _check_stalled_candidate(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find candidates stuck in 'candidate' status >48 hours without transitioning to shadow_testing."""
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=48)

    stalled = repo.conn.execute(
        """
        SELECT sv.id, sv.strategy_name, sv.version, sv.created_at
        FROM strategy_versions sv
        WHERE sv.status = 'candidate'
        """
    ).fetchall()

    for row in stalled:
        created_at_str = row["created_at"]
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < threshold:
                issues.append({
                    "type": "stalled_candidate",
                    "severity": "warning",
                    "details": {
                        "strategy_name": row["strategy_name"],
                        "version": row["version"],
                        "created_at": created_at_str,
                        "hours_stalled": int((now - created_at).total_seconds() / 3600),
                    },
                    "suggested_action": "Check backtest gate status; candidate may need manual promotion or rejection",
                })
        except (ValueError, TypeError):
            continue

    return issues


def _check_no_real_pnl_progress(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find shadow_testing candidates with no new real PnL samples in 7 days."""
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=7)

    candidates = repo.conn.execute(
        """
        SELECT sv.strategy_name, sv.version, sv.created_at,
               (SELECT MAX(se.created_at) FROM strategy_evaluations se
                WHERE se.strategy_name=sv.strategy_name AND se.strategy_version=sv.version
                  AND se.is_shadow=1 AND se.outcome_source='real_pnl' AND se.pnl_r IS NOT NULL) as last_real_pnl_at
        FROM strategy_versions sv
        WHERE sv.status = 'shadow_testing'
        """
    ).fetchall()

    for row in candidates:
        last_at = row["last_real_pnl_at"]
        if not last_at:
            created_at_str = row["created_at"]
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if created_at < threshold:
                        issues.append({
                            "type": "no_real_pnl_progress",
                            "severity": "warning",
                            "details": {
                                "strategy_name": row["strategy_name"],
                                "version": row["version"],
                                "last_real_pnl_at": None,
                                "days_since_creation": (now - created_at).days,
                            },
                            "suggested_action": "Candidate has zero real PnL samples after 7+ days; consider rejection",
                        })
                except (ValueError, TypeError):
                    continue
            continue

        try:
            last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if last_dt < threshold:
                issues.append({
                    "type": "no_real_pnl_progress",
                    "severity": "warning",
                    "details": {
                        "strategy_name": row["strategy_name"],
                        "version": row["version"],
                        "last_real_pnl_at": last_at,
                        "days_since_last_real_pnl": (now - last_dt).days,
                    },
                    "suggested_action": "No real PnL progress in 7+ days; candidate may be stale",
                })
        except (ValueError, TypeError):
            continue

    return issues


def _check_strategy_name_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find strategy_patches where strategy_name differs from the associated trigger's strategy_name."""
    issues: list[dict[str, Any]] = []

    mismatches = repo.conn.execute(
        """
        SELECT sp.id as patch_id, sp.strategy_name as patch_strategy,
               sp.candidate_version, et.id as trigger_id, et.strategy_name as trigger_strategy
        FROM strategy_patches sp
        JOIN evolution_triggers et ON sp.trigger_id = et.id
        WHERE sp.strategy_name != et.strategy_name
          AND sp.status NOT IN ('rejected', 'duplicate')
        """
    ).fetchall()

    for row in mismatches:
        issues.append({
            "type": "strategy_name_mismatch",
            "severity": "error",
            "details": {
                "patch_id": row["patch_id"],
                "patch_strategy_name": row["patch_strategy"],
                "candidate_version": row["candidate_version"],
                "trigger_id": row["trigger_id"],
                "trigger_strategy_name": row["trigger_strategy"],
            },
            "suggested_action": "Fix strategy_name in patch to match trigger, or reject the patch",
        })

    return issues


def _check_zero_quantity_virtual_trades(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find open/pending shadow_virtual_trades with quantity <= 0."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT id, symbol, strategy_name, candidate_version, status, quantity, initial_risk_usdt
        FROM shadow_virtual_trades
        WHERE status IN ('open', 'pending_entry') AND COALESCE(quantity, 0) <= 0
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "zero_quantity_virtual_trade",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "status": row["status"],
                "quantity": row["quantity"],
                "initial_risk_usdt": row["initial_risk_usdt"],
            },
            "suggested_action": "Fix risk sizing in _create_virtual_trade_for_candidate; quantity must be > 0",
        })

    return issues


def _check_zero_risk_virtual_trades(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find open/pending shadow_virtual_trades with initial_risk_usdt <= 0."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT id, symbol, strategy_name, candidate_version, status, quantity, initial_risk_usdt
        FROM shadow_virtual_trades
        WHERE status IN ('open', 'pending_entry') AND COALESCE(initial_risk_usdt, 0) <= 0
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "zero_risk_virtual_trade",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "status": row["status"],
                "quantity": row["quantity"],
                "initial_risk_usdt": row["initial_risk_usdt"],
            },
            "suggested_action": "Fix risk sizing; initial_risk_usdt must be > 0 for real R computation",
        })

    return issues


def _check_three_table_status_mismatch(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find trigger/patch/version rows where statuses are inconsistent.

    Covers: patch='draft' but version='candidate', version='shadow_testing' but patch='candidate',
    version='active' but trigger='pending'.
    """
    issues: list[dict[str, Any]] = []

    # patch='draft' but version IN ('candidate', 'shadow_testing', 'active')
    draft_patch_active_version = repo.conn.execute(
        """
        SELECT sp.id as patch_id, sp.strategy_name, sp.candidate_version,
               sp.status as patch_status, sv.status as version_status
        FROM strategy_patches sp
        JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        WHERE sp.status = 'draft' AND sv.status IN ('candidate', 'shadow_testing', 'active')
        """
    ).fetchall()

    for row in draft_patch_active_version:
        issues.append({
            "type": "three_table_status_mismatch",
            "severity": "error",
            "details": {
                "patch_id": row["patch_id"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "patch_status": row["patch_status"],
                "version_status": row["version_status"],
                "mismatch": "draft_patch_with_active_version",
            },
            "suggested_action": "Sync patch status to match version, or reject the patch",
        })

    # version='shadow_testing' but patch='candidate' (not synced after backtest)
    version_shadow_patch_candidate = repo.conn.execute(
        """
        SELECT sp.id as patch_id, sp.strategy_name, sp.candidate_version,
               sp.status as patch_status, sv.status as version_status
        FROM strategy_patches sp
        JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        WHERE sv.status = 'shadow_testing' AND sp.status = 'candidate'
        """
    ).fetchall()

    for row in version_shadow_patch_candidate:
        issues.append({
            "type": "three_table_status_mismatch",
            "severity": "warning",
            "details": {
                "patch_id": row["patch_id"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "patch_status": row["patch_status"],
                "version_status": row["version_status"],
                "mismatch": "version_shadow_testing_but_patch_candidate",
            },
            "suggested_action": "Sync patch status to shadow_testing",
        })

    # version='active' but trigger='pending'
    version_active_trigger_pending = repo.conn.execute(
        """
        SELECT sv.id as version_id, sv.strategy_name, sv.version, sv.status as version_status,
               et.id as trigger_id, et.status as trigger_status
        FROM strategy_versions sv
        JOIN strategy_patches sp ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        JOIN evolution_triggers et ON sp.trigger_id = et.id
        WHERE sv.status = 'active' AND et.status = 'pending'
        """
    ).fetchall()

    for row in version_active_trigger_pending:
        issues.append({
            "type": "three_table_status_mismatch",
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


def _check_closed_vt_missing_real_pnl_eval(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Find closed shadow_virtual_trades with no corresponding real_pnl evaluation.

    Must match by shadow_virtual_trade_id = svt.id (exact match).
    Also verifies pnl_r consistency between VT and evaluation.
    """
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT svt.id, svt.symbol, svt.strategy_name, svt.candidate_version,
               svt.ga_decision_id, svt.pnl_r, svt.close_reason
        FROM shadow_virtual_trades svt
        WHERE svt.status = 'closed'
          AND NOT EXISTS (
              SELECT 1 FROM strategy_evaluations se
              WHERE se.shadow_virtual_trade_id = svt.id
                AND se.outcome_source = 'real_pnl'
                AND se.is_shadow = 1
                AND se.pnl_r IS NOT NULL
          )
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "closed_vt_missing_real_pnl",
            "severity": "warning",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "ga_decision_id": row["ga_decision_id"],
                "pnl_r": row["pnl_r"],
                "close_reason": row["close_reason"],
            },
            "suggested_action": "Backfill strategy_evaluations with real_pnl from closed virtual trade",
        })

    # Also check: closed VT has evaluation but pnl_r differs (or is NULL on either side)
    pnl_mismatches = repo.conn.execute(
        """
        SELECT svt.id, svt.pnl_r AS vt_pnl_r, se.pnl_r AS eval_pnl_r,
               svt.symbol, svt.strategy_name, svt.candidate_version
        FROM shadow_virtual_trades svt
        JOIN strategy_evaluations se ON se.shadow_virtual_trade_id = svt.id
            AND se.outcome_source = 'real_pnl' AND se.is_shadow = 1
        WHERE svt.status = 'closed'
          AND svt.pnl_r IS NOT NULL
          AND (
              se.pnl_r IS NULL
              OR ABS(svt.pnl_r - se.pnl_r) > 0.0001
          )
        """
    ).fetchall()

    # Also check: closed VT has evaluation but VT pnl_r IS NULL (shouldn't happen)
    vt_null_mismatches = repo.conn.execute(
        """
        SELECT svt.id, svt.pnl_r AS vt_pnl_r, se.pnl_r AS eval_pnl_r,
               svt.symbol, svt.strategy_name, svt.candidate_version
        FROM shadow_virtual_trades svt
        JOIN strategy_evaluations se ON se.shadow_virtual_trade_id = svt.id
            AND se.outcome_source = 'real_pnl' AND se.is_shadow = 1
        WHERE svt.status = 'closed'
          AND svt.pnl_r IS NULL
        """
    ).fetchall()

    for row in pnl_mismatches:
        issues.append({
            "type": "closed_vt_missing_real_pnl",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "vt_pnl_r": row["vt_pnl_r"],
                "eval_pnl_r": row["eval_pnl_r"],
                "mismatch": "pnl_r_inconsistency",
            },
            "suggested_action": "Sync evaluation pnl_r to match closed virtual trade",
        })

    for row in vt_null_mismatches:
        issues.append({
            "type": "closed_vt_missing_real_pnl",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "vt_pnl_r": row["vt_pnl_r"],
                "eval_pnl_r": row["eval_pnl_r"],
                "mismatch": "vt_pnl_r_null_but_has_evaluation",
            },
            "suggested_action": "Backfill VT pnl_r from close price or mark as corrupted",
        })

    return issues


def _check_ambiguous_vt_missing_ambiguous_eval(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: closed shadow virtual trades with ambiguous close_reason must have
    strategy_evaluations with outcome_source='ambiguous_path', not 'real_pnl'."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT svt.id, svt.symbol, svt.strategy_name, svt.candidate_version,
               svt.close_reason, svt.ga_decision_id,
               se.id AS eval_id, se.outcome_source AS eval_outcome_source
        FROM shadow_virtual_trades svt
        LEFT JOIN strategy_evaluations se
          ON se.strategy_name = svt.strategy_name
         AND se.strategy_version = svt.candidate_version
         AND se.ga_decision_id = svt.ga_decision_id
         AND se.is_shadow = 1
        WHERE svt.status = 'closed'
          AND svt.close_reason IN ('ambiguous_path', 'activation_ambiguous_path')
        """
    ).fetchall()

    for row in rows:
        if row["eval_id"] is None:
            issues.append({
                "type": "ambiguous_vt_missing_ambiguous_eval",
                "severity": "error",
                "details": {
                    "virtual_trade_id": row["id"],
                    "symbol": row["symbol"],
                    "strategy_name": row["strategy_name"],
                    "candidate_version": row["candidate_version"],
                    "close_reason": row["close_reason"],
                    "ga_decision_id": row["ga_decision_id"],
                    "issue": "no_evaluation_found",
                },
                "suggested_action": "Backfill strategy_evaluation with outcome_source='ambiguous_path'",
            })
        elif row["eval_outcome_source"] != "ambiguous_path":
            issues.append({
                "type": "ambiguous_vt_missing_ambiguous_eval",
                "severity": "error",
                "details": {
                    "virtual_trade_id": row["id"],
                    "symbol": row["symbol"],
                    "strategy_name": row["strategy_name"],
                    "candidate_version": row["candidate_version"],
                    "close_reason": row["close_reason"],
                    "ga_decision_id": row["ga_decision_id"],
                    "actual_outcome_source": row["eval_outcome_source"],
                    "issue": "wrong_outcome_source",
                },
                "suggested_action": "Change strategy_evaluation.outcome_source to 'ambiguous_path'",
            })

    return issues


def _check_ambiguous_eval_not_real_pnl(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: strategy_evaluations with outcome_source='ambiguous_path' must NOT be counted
    as real_pnl in stats or used for candidate promotion decisions."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT se.id AS eval_id, se.strategy_name, se.strategy_version,
               se.ga_decision_id, se.outcome_source, se.pnl_r,
               svt.id AS vt_id, svt.status AS vt_status, svt.close_reason
        FROM strategy_evaluations se
        JOIN shadow_virtual_trades svt
          ON se.shadow_virtual_trade_id = svt.id
        WHERE se.outcome_source = 'ambiguous_path'
          AND se.is_shadow = 1
        """
    ).fetchall()

    for row in rows:
        if row["close_reason"] not in ("ambiguous_path", "activation_ambiguous_path"):
            issues.append({
                "type": "ambiguous_eval_not_real_pnl",
                "severity": "warning",
                "details": {
                    "eval_id": row["eval_id"],
                    "strategy_name": row["strategy_name"],
                    "strategy_version": row["strategy_version"],
                    "ga_decision_id": row["ga_decision_id"],
                    "vt_id": row["vt_id"],
                    "vt_close_reason": row["close_reason"],
                    "issue": "eval_ambiguous_but_vt_close_reason_mismatch",
                },
                "suggested_action": "Align VT close_reason with evaluation outcome_source",
            })

    return issues


def _check_duplicate_vt_per_candidate_decision(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: one (strategy_name, candidate_version, ga_decision_id) must have ≤1 shadow_virtual_trade."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT strategy_name, candidate_version, ga_decision_id, COUNT(*) AS cnt,
               GROUP_CONCAT(id) AS vt_ids,
               GROUP_CONCAT(status) AS statuses
        FROM shadow_virtual_trades
        WHERE ga_decision_id IS NOT NULL
          AND COALESCE(status, '') != 'duplicate'
        GROUP BY strategy_name, candidate_version, ga_decision_id
        HAVING cnt > 1
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "duplicate_vt_per_candidate_decision",
            "severity": "error",
            "details": {
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "ga_decision_id": row["ga_decision_id"],
                "count": row["cnt"],
                "vt_ids": row["vt_ids"],
                "statuses": row["statuses"],
            },
            "suggested_action": (
                "Keep the VT with the most advanced status (closed > open > pending_entry) "
                "and delete or merge the others"
            ),
        })

    return issues


def _check_closed_vt_still_processed(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: closed/expired shadow virtual trades should not have a pending cursor.
    Once a VT is closed or expired, last_processed_candle_time should be cleared."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT id, symbol, strategy_name, candidate_version, status,
               closed_at, last_processed_candle_time, created_at
        FROM shadow_virtual_trades
        WHERE status IN ('closed', 'expired')
          AND last_processed_candle_time IS NOT NULL
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "closed_vt_still_processed",
            "severity": "warning",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "status": row["status"],
                "closed_at": row["closed_at"],
                "last_processed_candle_time": row["last_processed_candle_time"],
                "issue": "cursor_still_set_on_closed_vt",
            },
            "suggested_action": "Clear last_processed_candle_time for closed/expired VTs",
        })

    return issues


def _check_cursor_regression(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: last_processed_candle_time must not regress (go backward) for the same VT."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT id, symbol, strategy_name, candidate_version, status,
               last_processed_candle_time, created_at, updated_at
        FROM shadow_virtual_trades
        WHERE last_processed_candle_time IS NOT NULL
          AND created_at IS NOT NULL
          AND last_processed_candle_time < created_at
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "cursor_regression",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "last_processed_candle_time": row["last_processed_candle_time"],
                "created_at": row["created_at"],
                "issue": "cursor_before_created_at",
            },
            "suggested_action": "Correct cursor to at least created_at timestamp",
        })

    return issues


def _check_illegal_status_transitions(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: shadow virtual trades must have legal status values.
    Legal: pending_entry, open, closed, expired, cancelled, duplicate."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT id, symbol, strategy_name, candidate_version, status, created_at
        FROM shadow_virtual_trades
        WHERE status NOT IN ('pending_entry', 'open', 'closed', 'expired', 'cancelled', 'duplicate')
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "illegal_status_transition",
            "severity": "error",
            "details": {
                "virtual_trade_id": row["id"],
                "symbol": row["symbol"],
                "strategy_name": row["strategy_name"],
                "candidate_version": row["candidate_version"],
                "current_status": row["status"],
                "issue": "unknown_status_value",
            },
            "suggested_action": "Correct status to a legal value",
        })

    return issues


def _check_active_eval_missing_ga_decision_id(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: active evaluations (is_shadow=0) with NULL ga_decision_id.

    Excludes legacy/duplicate/invalidated outcome_sources — those are pre-backfill
    artifacts that will never receive real_pnl. Only flags evaluations that were
    created through the current pipeline but are missing ga_decision_id (outcome_source
    IS NULL or 'pending_outcome').
    """
    issues: list[dict[str, Any]] = []

    # New-pipeline evals: outcome_source IS NULL or 'pending_outcome' but no ga_decision_id
    new_pipeline = repo.conn.execute(
        """
        SELECT COUNT(*) AS cnt, MIN(created_at) AS earliest, MAX(created_at) AS latest
        FROM strategy_evaluations
        WHERE is_shadow=0
          AND ga_decision_id IS NULL
          AND (outcome_source IS NULL OR outcome_source='pending_outcome')
        """
    ).fetchone()

    if new_pipeline and new_pipeline["cnt"] > 0:
        issues.append({
            "type": "active_eval_missing_ga_decision_id",
            "severity": "error",
            "details": {
                "count": new_pipeline["cnt"],
                "earliest": new_pipeline["earliest"],
                "latest": new_pipeline["latest"],
                "category": "new_pipeline",
            },
            "suggested_action": (
                "Investigate active evaluations with NULL ga_decision_id; "
                "backfill ga_decision_id linkage from paper_orders if available"
            ),
        })

    # Legacy evals: outcome_source IN ('legacy_fuzzy','duplicate','invalidated') — info only
    legacy = repo.conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM strategy_evaluations
        WHERE is_shadow=0
          AND ga_decision_id IS NULL
          AND outcome_source IN ('legacy_fuzzy', 'duplicate', 'invalidated')
        """
    ).fetchone()

    if legacy and legacy["cnt"] > 0:
        issues.append({
            "type": "active_eval_missing_ga_decision_id",
            "severity": "info",
            "details": {
                "count": legacy["cnt"],
                "category": "legacy_artifact",
            },
            "suggested_action": (
                "These are pre-backfill legacy evaluations. They do not block "
                "the active PnL loop but represent historical data gaps."
            ),
        })

    return issues


def _check_paper_order_missing_active_eval(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: paper_orders with ga_decision_id that SHOULD have an active evaluation
    but don't. Only checks orders that can produce meaningful outcomes:
    open, pending, needs_recheck, closed (with valid non-duplicate_cleanup trade)."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT po.id AS order_id, po.ga_decision_id, po.symbol, po.status
        FROM paper_orders po
        WHERE po.ga_decision_id IS NOT NULL
          AND po.status IN ('open', 'pending', 'needs_recheck')
          AND NOT EXISTS (
              SELECT 1 FROM strategy_evaluations se
              WHERE se.ga_decision_id=po.ga_decision_id AND se.is_shadow=0
          )
        LIMIT 50
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "paper_order_missing_active_eval",
            "severity": "error",
            "details": {
                "order_id": row["order_id"],
                "ga_decision_id": row["ga_decision_id"],
                "symbol": row["symbol"],
                "order_status": row["status"],
            },
            "suggested_action": (
                "Investigate paper_orders missing an active evaluation; "
                "create the evaluation via the normal trade pipeline if applicable"
            ),
        })

    return issues


def _check_closed_trade_missing_active_real_pnl(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: closed paper_trades with pnl_r should have an active evaluation
    with outcome_source='real_pnl' and matching paper_trade_id.
    Excludes close_reason='duplicate_cleanup'."""
    issues: list[dict[str, Any]] = []

    rows = repo.conn.execute(
        """
        SELECT pt.id AS trade_id, pt.order_id, pt.pnl_r, pt.close_reason,
               po.ga_decision_id
        FROM paper_trades pt
        JOIN paper_orders po ON po.id=pt.order_id
        WHERE pt.closed_at IS NOT NULL
          AND pt.pnl_r IS NOT NULL
          AND po.ga_decision_id IS NOT NULL
          AND (pt.close_reason IS NULL OR pt.close_reason != 'duplicate_cleanup')
          AND NOT EXISTS (
              SELECT 1 FROM strategy_evaluations se
              WHERE se.ga_decision_id=po.ga_decision_id
                AND se.is_shadow=0
                AND se.outcome_source='real_pnl'
                AND se.paper_trade_id=pt.id
          )
        LIMIT 50
        """
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "closed_trade_missing_active_real_pnl",
            "severity": "error",
            "details": {
                "trade_id": row["trade_id"],
                "order_id": row["order_id"],
                "ga_decision_id": row["ga_decision_id"],
                "pnl_r": row["pnl_r"],
                "close_reason": row["close_reason"],
            },
            "suggested_action": (
                "Investigate closed trades missing a real_pnl active evaluation; "
                "backfill the evaluation via the normal trade pipeline if applicable"
            ),
        })

    return issues


def _check_shadow_candidate_legacy_only_samples(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: shadow candidates whose evaluations are all legacy/duplicate/pseudo
    with no real_pnl or executed_virtual_trade samples.

    Only flags candidates created more than 24 hours ago to avoid false positives
    on freshly created candidates that haven't had time to accumulate samples.
    """
    issues: list[dict[str, Any]] = []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    candidates = repo.conn.execute(
        """
        SELECT sv.strategy_name, sv.version, sv.status, sv.created_at,
               (SELECT COUNT(*) FROM strategy_evaluations se
                WHERE se.strategy_name=sv.strategy_name
                  AND se.strategy_version=sv.version
                  AND se.is_shadow=1) AS total_evals,
               (SELECT COUNT(*) FROM strategy_evaluations se
                WHERE se.strategy_name=sv.strategy_name
                  AND se.strategy_version=sv.version
                  AND se.is_shadow=1
                  AND se.outcome_source IN ('real_pnl', 'executed_virtual_trade')) AS real_samples,
               (SELECT COUNT(*) FROM strategy_evaluations se
                WHERE se.strategy_name=sv.strategy_name
                  AND se.strategy_version=sv.version
                  AND se.is_shadow=1
                  AND se.outcome_source IN ('legacy_fuzzy', 'duplicate', 'invalidated')) AS legacy_samples
        FROM strategy_versions sv
        WHERE sv.status IN ('candidate', 'shadow_testing')
          AND sv.created_at < ?
        ORDER BY sv.created_at DESC
        """,
        (cutoff,),
    ).fetchall()

    for row in candidates:
        total = int(row["total_evals"] or 0)
        real = int(row["real_samples"] or 0)
        legacy = int(row["legacy_samples"] or 0)

        if total > 0 and real == 0 and legacy >= total * 0.5:
            issues.append({
                "type": "shadow_candidate_legacy_only",
                "severity": "warning",
                "details": {
                    "strategy_name": row["strategy_name"],
                    "version": row["version"],
                    "status": row["status"],
                    "total_evals": total,
                    "real_samples": real,
                    "legacy_samples": legacy,
                    "created_at": row["created_at"],
                },
                "suggested_action": (
                    "Candidate has no real_pnl or executed_virtual_trade samples "
                    "and is >24h old. Consider soft-rejecting and creating a fresh "
                    "candidate from the next trade review cycle, or wait for new "
                    "samples if the service was recently restarted."
                ),
            })

    return issues
