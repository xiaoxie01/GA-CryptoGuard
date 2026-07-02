"""State consistency diagnostics for CryptoGuard evolution system.

Detects:
- Orphan patches (strategy_patches with no matching strategy_version)
- Status mismatches (trigger/patch/version state inconsistencies)
- Stale shadows (candidates in shadow_testing >7 days with no new samples)
- Draft limbo (patches in draft >72 hours)
- Duplicate open trades (same order_id with multiple open paper_trades)
- Financial action missing mark price (paper_trade_logs with financial actions but no mark_price)
- Financial action stale price (paper_trade_logs with mark_price older than action time)
- Paper notification missing event time (alert_outbox paper events without event_time in payload)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.state_diagnostics")



def _profit_protection_cutoff(repo: CryptoGuardRepository) -> str | None:
    """Return the cutoff timestamp for profit-protection-era checks.

    Looks up the applied_at of the profit_protection_mark_price_contract_v1
    migration marker in _migration_state. Returns None when the marker
    doesn't exist, meaning the migration hasn't been applied and new-contract
    diagnostics should be skipped entirely.
    """
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state "
            "WHERE key = 'profit_protection_mark_price_contract_v1' "
            "LIMIT 1"
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        pass
    return None


def _btc9_contract_cutoff(repo: CryptoGuardRepository) -> str | None:
    """Section 七: Return the cutoff timestamp for BTC#9 regression-chain checks.

    Uses the independent btc9_trade_gate_contract_v1 marker (NOT the R4 marker).
    Returns None if the marker is absent, meaning BTC#9 diagnostics should be
    skipped (no contract yet).
    """
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state "
            "WHERE key = 'btc9_trade_gate_contract_v1' "
            "LIMIT 1"
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        pass
    return None


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
    issues.extend(_check_financial_action_missing_mark_price(repo))
    issues.extend(_check_financial_action_stale_price(repo))
    issues.extend(_check_paper_notification_missing_event_time(repo))
    # Section 九: Schema health is an integral part of state consistency
    issues.extend(_check_schema_health_as_issues(repo))
    # BTC#9 fix: 6 类新诊断
    issues.extend(_check_btc9_contract_marker_missing(repo))
    issues.extend(_check_fallback_llm_failed_created_paper_order(repo))
    issues.extend(_check_missing_entry_confirmation_paper_order(repo))
    issues.extend(_check_htf_support_reason_inconsistent(repo))
    issues.extend(_check_chop_regime_boosted(repo))
    issues.extend(_check_fill_without_ga_revalidation(repo))
    issues.extend(_check_invalid_condition_equals_stop_loss(repo))

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
        "financial_action_missing_mark_price": len([i for i in issues if i["type"] == "financial_action_missing_mark_price"]),
        "financial_action_stale_price": len([i for i in issues if i["type"] == "financial_action_stale_price"]),
        "paper_notification_missing_event_time": len([i for i in issues if i["type"] == "paper_notification_missing_event_time"]),
        "btc9_contract_marker_missing": len([i for i in issues if i["type"] == "btc9_contract_marker_missing"]),
    }

    # Section 八: Separate counts for error/warning/legacy_info
    error_issues = [i for i in issues if i.get("severity") == "error"]
    warning_issues = [i for i in issues if i.get("severity") == "warning"]
    legacy_issues = [i for i in issues if i.get("severity") == "legacy_info"]

    return {
        "ok": len(error_issues) == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
        "error_count": len(error_issues),
        "warning_count": len(warning_issues),
        "legacy_info_count": len(legacy_issues),
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


def _check_financial_action_missing_mark_price(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: paper_trade_logs with financial actions (breakeven stop adjust, profit protection,
    conflict exit) that are missing mark_price in their event_json.

    Financial actions that change stop-loss or close positions must record the mark_price
    used for the decision. Missing mark_price means the action was taken without a
    verifiable price reference.
    """
    issues: list[dict[str, Any]] = []
    financial_actions = ("stop_loss_adjustment", "profit_protection", "conflict_exit",
                         "strong_conflict_profit_protection")

    cutoff = _profit_protection_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, position_id, event_type, event_json, created_at
        FROM paper_trade_logs
        WHERE event_type IN (?, ?, ?, ?)
          AND created_at >= ?
          AND (event_json IS NULL
               OR json_extract(event_json, '$.mark_price') IS NULL)
        ORDER BY created_at DESC
        LIMIT 200
        """,
        (*financial_actions, cutoff),
    ).fetchall()

    for row in rows:
        issues.append({
            "type": "financial_action_missing_mark_price",
            "severity": "warning",
            "details": {
                "log_id": row["id"],
                "position_id": row["position_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
            },
            "suggested_action": (
                "Financial actions must include mark_price in event_json. "
                "Backfill mark_price from paper_positions or Binance API if available."
            ),
        })

    return issues


def _check_financial_action_stale_price(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: paper_trade_logs with financial actions where the mark_price timestamp
    (price_as_of) is more than 120 seconds older than the action's created_at.

    A stale price means the action was taken on outdated market data, which can
    lead to incorrect stop-loss placement or premature profit protection exits.
    """
    issues: list[dict[str, Any]] = []
    financial_actions = ("stop_loss_adjustment", "profit_protection", "conflict_exit",
                         "strong_conflict_profit_protection")

    cutoff = _profit_protection_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, position_id, event_type, event_json, created_at
        FROM paper_trade_logs
        WHERE event_type IN (?, ?, ?, ?)
          AND created_at >= ?
          AND event_json IS NOT NULL
          AND json_extract(event_json, '$.mark_price') IS NOT NULL
          AND json_extract(event_json, '$.price_as_of') IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 200
        """,
        (*financial_actions, cutoff),
    ).fetchall()

    for row in rows:
        try:
            import json
            event = json.loads(row["event_json"])
            price_as_of = event.get("price_as_of")
            created_at = row["created_at"]

            if price_as_of and created_at:
                price_dt = datetime.fromisoformat(str(price_as_of).replace("Z", "+00:00"))
                action_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                if price_dt.tzinfo is None:
                    price_dt = price_dt.replace(tzinfo=timezone.utc)
                if action_dt.tzinfo is None:
                    action_dt = action_dt.replace(tzinfo=timezone.utc)

                age_seconds = (action_dt - price_dt).total_seconds()
                if age_seconds > 120:
                    issues.append({
                        "type": "financial_action_stale_price",
                        "severity": "warning",
                        "details": {
                            "log_id": row["id"],
                            "position_id": row["position_id"],
                            "event_type": row["event_type"],
                            "price_age_seconds": round(age_seconds, 1),
                            "price_as_of": price_as_of,
                            "action_at": created_at,
                        },
                        "suggested_action": (
                            f"Price was {age_seconds:.0f}s old when action was taken. "
                            "Investigate mark_price fetch latency or cache staleness."
                        ),
                    })
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    return issues


def _check_paper_notification_missing_event_time(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Check: alert_outbox rows for paper trading events that are missing event_time
    (UTC+8) in their payload_json.

    All paper trading notifications must include an explicit UTC+8 event time
    via the shared format_event_time_cst formatter. Missing event_time means
    the notification was sent without a verifiable timestamp.

    Also checks fallback_text for UTC+8 time patterns — if the fallback text
    already contains a UTC+8 timestamp, the notification is not flagged.
    """
    import re
    issues: list[dict[str, Any]] = []
    paper_alert_types = (
        "paper_order_filled", "paper_order_expired", "stop_loss_adjustment",
        "stop_loss_hit", "take_profit_hit", "conflict_cancelled",
        "strong_conflict_profit_protection", "profit_protection",
        "paper_event_alert",
    )

    # UTC+8 time pattern: YYYY-MM-DD HH:MM:SS (UTC+8) or YYYY-MM-DD HH:MM (UTC+8)
    utc8_pattern = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})? \(UTC\+8\)')

    cutoff = _profit_protection_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, alert_type, payload_json, created_at
        FROM alert_outbox
        WHERE alert_type IN (?, ?, ?, ?, ?, ?, ?, ?, ?)
          AND status = 'sent'
          AND created_at >= ?
          AND (payload_json IS NULL
               OR json_extract(payload_json, '$.event_time') IS NULL)
        ORDER BY created_at DESC
        LIMIT 200
        """,
        (*paper_alert_types, cutoff),
    ).fetchall()

    for row in rows:
        # Check if fallback_text already contains a UTC+8 time
        payload_json = row["payload_json"]
        if payload_json:
            try:
                import json
                payload = json.loads(payload_json)
                fallback = payload.get("fallback_text", "")
                if fallback and utc8_pattern.search(str(fallback)):
                    continue  # UTC+8 time found in fallback text, not missing
            except (json.JSONDecodeError, TypeError):
                pass

        issues.append({
            "type": "paper_notification_missing_event_time",
            "severity": "info",
            "details": {
                "outbox_id": row["id"],
                "alert_type": row["alert_type"],
                "created_at": row["created_at"],
            },
            "suggested_action": (
                "Paper notifications must include event_time (UTC+8) in payload_json. "
                "Update notification handlers to use format_event_time_cst from notify/time_utils.py."
            ),
        })

    return issues


def _check_btc9_contract_marker_missing(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Section 九: Emit error when the btc9_trade_gate_contract_v1 marker is absent.

    The BTC#9 contract marker is independent from the R4 marker. When the
    schema and code for BTC#9 exist but the marker is missing, it means
    initialize_database() has not been run (or failed before the marker
    step). Diagnostics must not silently skip all BTC#9 checks and report
    healthy — they must emit an explicit error.
    """
    issues: list[dict[str, Any]] = []
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state "
            "WHERE key = 'btc9_trade_gate_contract_v1' "
            "LIMIT 1"
        ).fetchone()
        if not row or not row["applied_at"]:
            issues.append({
                "type": "btc9_contract_marker_missing",
                "severity": "error",
                "details": {
                    "marker_key": "btc9_trade_gate_contract_v1",
                    "issue": "marker_absent",
                },
                "suggested_action": (
                    "BTC#9 contract marker 缺失。运行 initialize_database() 部署 marker。"
                    "marker 缺失时所有 BTC#9 诊断被跳过，可能导致假绿。"
                ),
            })
    except Exception:
        # _migration_state table itself may not exist
        issues.append({
            "type": "btc9_contract_marker_missing",
            "severity": "error",
            "details": {
                "marker_key": "btc9_trade_gate_contract_v1",
                "issue": "migration_state_table_missing",
            },
            "suggested_action": (
                "_migration_state 表不存在或查询失败。"
                "运行 initialize_database() 创建表并写入 BTC#9 marker。"
            ),
        })
    return issues


def _check_fallback_llm_failed_created_paper_order(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: LLM failed/disabled 的 GA decision 不应关联到非取消的 paper_order。

    Section 八: Uses BTC#9 marker cutoff in SQL WHERE clause.
    - Pre-marker data: severity=legacy_info (historical, not actionable)
    - Post-marker data: severity=error (contract violation)
    - Uses aggregate COUNT to detect truncation by LIMIT.
    """
    import json
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    # Aggregate count to detect truncation by LIMIT
    total_count_row = repo.conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM paper_orders po
        JOIN ga_decisions gd ON po.ga_decision_id = gd.id
        WHERE po.status NOT IN ('revalidator_cancelled', 'watch_cancelled',
                                'expired', 'risk_off_cancelled', 'cancelled')
          AND datetime(replace(replace(gd.analysis_time_utc, 'T', ' '), 'Z', '')) >= datetime(?)
        """,
        (cutoff,),
    ).fetchone()
    total_matching = int(total_count_row["cnt"]) if total_count_row else 0

    rows = repo.conn.execute(
        """
        SELECT po.id AS order_id, po.symbol, po.side, po.status, po.ga_decision_id,
               gd.raw_decision_json, gd.analysis_time_utc
        FROM paper_orders po
        JOIN ga_decisions gd ON po.ga_decision_id = gd.id
        WHERE po.status NOT IN ('revalidator_cancelled', 'watch_cancelled',
                                'expired', 'risk_off_cancelled', 'cancelled')
          AND datetime(replace(replace(gd.analysis_time_utc, 'T', ' '), 'Z', '')) >= datetime(?)
        ORDER BY po.id DESC
        LIMIT 500
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw_decision_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        llm_status = str(raw.get("llm_status") or "ok").lower()
        if llm_status not in {"failed", "disabled"}:
            continue
        issues.append({
            "type": "fallback_llm_failed_created_paper_order",
            "severity": "error",
            "details": {
                "order_id": row["order_id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "status": row["status"],
                "ga_decision_id": row["ga_decision_id"],
                "llm_status": llm_status,
            },
            "suggested_action": (
                "LLM failed/disabled 的 GA decision 必须降级为 opportunity_watch，"
                "不应创建模拟盘订单。检查 risk_engine.apply_risk_to_decision 的 fallback 降级逻辑。"
            ),
        })
    # If aggregate count exceeds LIMIT, emit a truncation warning so diagnostics
    # do not falsely declare all-healthy due to LIMIT truncation.
    if total_matching > 500:
        issues.append({
            "type": "fallback_llm_failed_created_paper_order",
            "severity": "warning",
            "details": {
                "truncated": True,
                "total_matching": total_matching,
                "limit": 500,
            },
            "suggested_action": (
                "诊断因 LIMIT 500 截断，可能遗漏部分违规订单。"
                "请扩大分页或使用 aggregate COUNT 进行全库审计。"
            ),
        })
    return issues


def _check_missing_entry_confirmation_paper_order(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: 缺少 entry_trigger_confirmation 的 trade_plan 不应创建非取消订单。

    Section 八: Uses BTC#9 marker cutoff in SQL WHERE clause.
    R3-E: Provenance-aware — calls the same _validate_entry_confirmation used
    by the execution gate, passing snapshot/module_analysis_results loaded
    from the persisted snapshot_id. A structurally valid but fabricated
    confirmation is now reported when no matching real event exists.

    Reports by reason category:
    - missing confirmation
    - malformed confirmation
    - confirmation event not found
    - event not closed
    - future event
    - field mismatch

    Uses aggregate COUNT to detect truncation by LIMIT. Emits
    diagnostic_truncated warning when total > 500.
    """
    import json
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    # Aggregate count to detect truncation by LIMIT
    total_count_row = repo.conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM paper_orders po
        JOIN ga_decisions gd ON po.ga_decision_id = gd.id
        WHERE po.status NOT IN ('revalidator_cancelled', 'watch_cancelled',
                                'expired', 'risk_off_cancelled', 'cancelled')
          AND datetime(replace(replace(gd.analysis_time_utc, 'T', ' '), 'Z', '')) >= datetime(?)
        """,
        (cutoff,),
    ).fetchone()
    total_matching = int(total_count_row["cnt"]) if total_count_row else 0

    rows = repo.conn.execute(
        """
        SELECT po.id AS order_id, po.symbol, po.side, po.status, po.ga_decision_id,
               gd.trade_plan_json, gd.analysis_time_utc, gd.analysis_time,
               gd.snapshot_id
        FROM paper_orders po
        JOIN ga_decisions gd ON po.ga_decision_id = gd.id
        WHERE po.status NOT IN ('revalidator_cancelled', 'watch_cancelled',
                                'expired', 'risk_off_cancelled', 'cancelled')
          AND datetime(replace(replace(gd.analysis_time_utc, 'T', ' '), 'Z', '')) >= datetime(?)
        ORDER BY po.id DESC
        LIMIT 500
        """,
        (cutoff,),
    ).fetchall()
    from plugins.crypto_guard.risk.risk_engine import _validate_entry_confirmation
    for row in rows:
        plan_json = row["trade_plan_json"] or "{}"
        try:
            plan = json.loads(plan_json)
        except (json.JSONDecodeError, TypeError):
            continue
        ec = plan.get("entry_trigger_confirmation")
        side = str(row["side"] or "").upper()
        analysis_time_ms = int(row["analysis_time"] or 0)

        # R3-E: Load persisted module_analysis_results by snapshot_id for
        # provenance-aware validation — same path as the execution gate.
        snapshot_id = row["snapshot_id"]
        module_analysis_results: dict[str, Any] | None = None
        provenance_load_error: str | None = None
        if snapshot_id:
            try:
                mod_rows = repo.conn.execute(
                    "SELECT module, result_json FROM module_analysis_results "
                    "WHERE snapshot_id=?",
                    (int(snapshot_id),),
                ).fetchall()
                if mod_rows:
                    module_analysis_results = {}
                    for mr in mod_rows:
                        mod_key = mr["module"]
                        try:
                            module_analysis_results[mod_key] = json.loads(mr["result_json"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            module_analysis_results[mod_key] = {}
                else:
                    # snapshot_id present but no module_analysis_results rows
                    provenance_load_error = "no_module_analysis_results_for_snapshot"
            except Exception as e:
                provenance_load_error = f"module_analysis_results_query_failed: {e}"
        else:
            provenance_load_error = "missing_snapshot_id"

        # Use the unified provenance-aware validator: when
        # module_analysis_results is available, the confirmation must
        # match a real persisted event. Fabricated objects are rejected.
        # R4-D3: pass repo so the validator can also verify deterministic_rule
        # sources against persisted events.
        valid, reason = _validate_entry_confirmation(
            ec, side, analysis_time_ms,
            module_analysis_results=module_analysis_results,
            repo=repo,
        )
        if not valid:
            # Categorize by reason for diagnostic clarity
            reason_str = str(reason or "")
            if "provenance_unavailable" in reason_str:
                category = "provenance_unavailable"
            elif "必须是对象" in reason_str or "不是字符串" in reason_str:
                category = "malformed_confirmation"
            elif "not_found" in reason_str or "not_found_in_real_events" in reason_str:
                category = "confirmation_event_not_found"
            elif "future" in reason_str or "晚于" in reason_str:
                category = "future_event"
            elif "不匹配" in reason_str or "mismatch" in reason_str:
                category = "field_mismatch"
            elif ec is None:
                category = "missing_confirmation"
            else:
                category = "malformed_confirmation"

            issues.append({
                "type": "missing_entry_confirmation_paper_order",
                "severity": "error",
                "details": {
                    "order_id": row["order_id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "status": row["status"],
                    "entry_trigger_confirmation": ec,
                    "validation_reason": reason,
                    "category": category,
                    "snapshot_id": snapshot_id,
                    "provenance_load_error": provenance_load_error,
                },
                "suggested_action": (
                    "缺少有效入场确认（entry_trigger_confirmation 必须为结构化对象且通过"
                    "_validate_entry_confirmation provenance-aware 验证）禁止直接开仓。"
                    f"category={category}。"
                    "检查 risk_engine.validate_trade_plan 的 require_entry_confirmation_for_paper_order 门禁。"
                ),
            })
    # If aggregate count exceeds LIMIT, emit a diagnostic_truncated warning
    # so diagnostics do not falsely declare all-healthy due to LIMIT truncation.
    if total_matching > 500:
        issues.append({
            "type": "diagnostic_truncated",
            "severity": "warning",
            "details": {
                "check": "missing_entry_confirmation_paper_order",
                "truncated": True,
                "total_matching": total_matching,
                "limit": 500,
            },
            "suggested_action": (
                "诊断因 LIMIT 500 截断，可能遗漏部分违规订单。"
                "请扩大分页或使用 aggregate COUNT 进行全库审计。"
            ),
        })
    return issues


def _check_htf_support_reason_inconsistent(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: risk_check.htf_support.ok=True 时 reason 不得包含「不支持」。"""
    import json
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, symbol, risk_check_json, analysis_time_utc
        FROM ga_decisions
        WHERE risk_check_json IS NOT NULL
          AND datetime(analysis_time_utc) >= datetime(?)
        ORDER BY id DESC
        LIMIT 500
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        try:
            risk_check = json.loads(row["risk_check_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        htf = (risk_check.get("metrics") or {}).get("htf_support") or {}
        ok = bool(htf.get("ok"))
        reason = str(htf.get("reason") or "")
        if ok and "不支持" in reason:
            issues.append({
                "type": "htf_support_reason_inconsistent",
                "severity": "error",
                "details": {
                    "ga_decision_id": row["id"],
                    "symbol": row["symbol"],
                    "htf_ok": ok,
                    "htf_reason": reason,
                },
                "suggested_action": (
                    "htf_support.ok=True 时 reason 必须为正面/空文本。"
                    "检查 risk_engine._htf_support 的 reason 赋值逻辑。"
                ),
            })
    return issues


def _check_chop_regime_boosted(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: market_phase=chop/transition/unknown 时不应有正向 confidence_adjustment。

    Section 八: Detects abnormal boosts for all non-aligned phases:
    - chop: sideways, no directional boost
    - transition: uncertain, no directional boost
    - unknown: regime unclear, no directional boost
    """
    import json
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT id, symbol, market_regime_gate_json, analysis_time_utc
        FROM ga_decisions
        WHERE market_regime_gate_json IS NOT NULL
          AND datetime(analysis_time_utc) >= datetime(?)
        ORDER BY id DESC
        LIMIT 500
        """,
        (cutoff,),
    ).fetchall()
    # Phases that should never receive a positive confidence_adjustment
    abnormal_phases = {"chop", "transition", "unknown"}
    for row in rows:
        raw = row["market_regime_gate_json"] or "{}"
        # Safely parse JSON; default to {} on any failure or non-dict result
        try:
            regime_gate = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(regime_gate, dict):
            continue
        adjustments = regime_gate.get("adjustments") or {}
        if not isinstance(adjustments, dict):
            continue
        market_phase = str(adjustments.get("market_phase") or "normal").lower()
        conf_adj = float(adjustments.get("confidence_adjustment") or 0)
        if market_phase in abnormal_phases and conf_adj > 0:
            issues.append({
                "type": "chop_regime_boosted",
                "severity": "error",
                "details": {
                    "ga_decision_id": row["id"],
                    "symbol": row["symbol"],
                    "market_phase": market_phase,
                    "confidence_adjustment": conf_adj,
                },
                "suggested_action": (
                    f"market_phase={market_phase} 不应提供 confidence boost。"
                    "检查 risk_engine.apply_regime_gate aligned 分支的抑制逻辑。"
                ),
            })
    return issues


def _check_fill_without_ga_revalidation(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: 事后审计 — limit_range_touch 成交时刻最新 GA 已转冲突。"""
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT po.id AS order_id, po.symbol, po.side, po.filled_at, po.ga_decision_id
        FROM paper_orders po
        WHERE po.fill_method = 'limit_range_touch'
          AND po.filled_at IS NOT NULL
          AND datetime(replace(replace(po.filled_at, 'T', ' '), 'Z', '')) >= datetime(?)
        ORDER BY po.id DESC
        LIMIT 200
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        filled_at_norm = str(row["filled_at"]).replace("T", " ").replace("Z", "")
        ga = repo.conn.execute(
            "SELECT id, market_bias, signal_grade FROM ga_decisions "
            "WHERE symbol=? AND datetime(replace(replace(analysis_time_utc, 'T', ' '), 'Z', '')) <= datetime(?) "
            "ORDER BY analysis_time DESC LIMIT 1",
            (row["symbol"], filled_at_norm),
        ).fetchone()
        if not ga:
            continue
        bias = str(ga["market_bias"] or "neutral").lower()
        grade = str(ga["signal_grade"] or "D").upper()
        side = str(row["side"] or "").upper()
        conflict = (
            (side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"})
            or (side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"})
        )
        if conflict:
            issues.append({
                "type": "fill_without_ga_revalidation",
                "severity": "error",
                "details": {
                    "order_id": row["order_id"],
                    "symbol": row["symbol"],
                    "side": side,
                    "filled_at": row["filled_at"],
                    "conflict_ga_decision_id": ga["id"],
                    "conflict_bias": bias,
                    "conflict_grade": grade,
                },
                "suggested_action": (
                    "fill 前必须复核最新 GA，方向冲突时取消订单。"
                    "检查 paper_broker._revalidate_pending_before_fill 的调用。"
                ),
            })
    return issues


def _check_invalid_condition_equals_stop_loss(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """BTC#9 fix: trade_plan.invalid_condition 解析价不应等于 stop_loss。"""
    import json
    import re
    issues: list[dict[str, Any]] = []
    cutoff = _btc9_contract_cutoff(repo)
    if cutoff is None:
        return issues
    rows = repo.conn.execute(
        """
        SELECT po.id AS order_id, po.symbol, po.side, po.status, po.ga_decision_id,
               gd.trade_plan_json
        FROM paper_orders po
        JOIN ga_decisions gd ON po.ga_decision_id = gd.id
        WHERE gd.trade_plan_json IS NOT NULL
          AND datetime(replace(replace(gd.analysis_time_utc, 'T', ' '), 'Z', '')) >= datetime(?)
        ORDER BY po.id DESC
        LIMIT 500
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        try:
            plan = json.loads(row["trade_plan_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        stop = plan.get("stop_loss")
        invalid_cond = plan.get("invalid_condition")
        if stop is None or not isinstance(invalid_cond, str):
            continue
        try:
            stop_f = float(stop)
        except (TypeError, ValueError):
            continue
        # BTC#9 fix: invalid_condition 文本可能包含非价格数字（如 "15m"），
        # 取最后一个数字作为失效价
        all_matches = re.findall(r"[-+]?\d+(?:\.\d+)?", invalid_cond)
        if not all_matches:
            continue
        try:
            invalid_price = float(all_matches[-1])
        except (TypeError, ValueError):
            continue
        if abs(invalid_price - stop_f) < 1e-9:
            issues.append({
                "type": "invalid_condition_equals_stop_loss",
                "severity": "warning",
                "details": {
                    "order_id": row["order_id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "stop_loss": stop_f,
                    "invalid_condition": invalid_cond,
                    "parsed_invalid_price": invalid_price,
                },
                "suggested_action": (
                    "invalid_condition 价与 stop_loss 同价，缺少失效缓冲层。"
                    "检查 ga_judge._build_trade_plan 的 invalid_condition_price 计算。"
                ),
            })
    return issues


def _check_schema_health_as_issues(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Section 九: Delegates to check_schema_health(conn=repo.conn) and
    converts any failures into state_consistency issues.

    Schema health is an integral part of state consistency — missing columns,
    missing indexes, or incorrect CHECK constraints are state consistency
    errors.
    """
    from plugins.crypto_guard.storage.migrations import check_schema_health

    try:
        health = check_schema_health(conn=repo.conn)
    except Exception as e:
        return [{
            "type": "schema_health_check_failed",
            "severity": "error",
            "details": {"exception": str(e)},
            "suggested_action": "check_schema_health() raised an exception. Check the database connection and schema integrity.",
        }]

    issues: list[dict[str, Any]] = []

    if not health.get("ok"):
        for col in (health.get("missing_columns") or []):
            issues.append({
                "type": "schema_health_missing_column",
                "severity": "error",
                "details": {"column": col},
                "suggested_action": "Run initialize_database() to apply migrations.",
            })

        for idx in (health.get("missing_indexes") or []):
            issues.append({
                "type": "schema_health_missing_index",
                "severity": "error",
                "details": {"index": idx},
                "suggested_action": "Run initialize_database() to rebuild indexes.",
            })

        for con in (health.get("constraint_errors") or []):
            issues.append({
                "type": "schema_health_constraint_error",
                "severity": "error",
                "details": {"constraint": con},
                "suggested_action": "Check migrations for missed schema updates.",
            })

    return issues
