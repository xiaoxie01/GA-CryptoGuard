from __future__ import annotations

import json
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository, _decode_json, _json_dumps_payload


def evaluate_evolution_triggers(repo: CryptoGuardRepository, *, snapshot: dict[str, Any] | None = None, report_only: bool = False) -> dict[str, Any]:
    snapshot = snapshot or {}

    # report_only must be checked FIRST — before any write operations
    if report_only:
        existing_triggers = repo.conn.execute(
            "SELECT * FROM evolution_triggers WHERE status IN ('pending','shadow_testing','review_required') ORDER BY latest_triggered_at DESC LIMIT 20"
        ).fetchall()
        return {
            "ok": True,
            "triggered": False,
            "actions": [],
            "cleaned_stale": {"skipped": True, "reason": "report_only"},
            "cleaned_duplicates": {"skipped": True, "reason": "report_only"},
            "report_only": True,
            "existing_triggers": [dict(r) for r in existing_triggers],
        }

    actions: list[dict[str, Any]] = []

    # Cleanup stale candidates before creating new ones
    cleaned = _cleanup_stale_candidates(repo)

    # P1: 软清理历史重复补丁
    duplicates_cleaned = repo.mark_duplicate_patches_rejected()

    stop_loss_trigger = _consecutive_stop_losses(repo)
    if stop_loss_trigger:
        actions.append(_record_trigger_and_candidate(repo, stop_loss_trigger))

    # Only fire daily_loss_threshold if consecutive_stop_losses didn't fire
    # (consecutive is more specific and higher priority)
    if not stop_loss_trigger:
        daily_loss_trigger = _daily_loss_threshold(repo)
        if daily_loss_trigger:
            actions.append(_record_trigger_and_candidate(repo, daily_loss_trigger))

    drawdown = float(snapshot.get("drawdown_percent") or 0) / 100.0
    if drawdown <= -0.10:
        actions.append(
            _record_trigger_and_candidate(
                repo,
                {
                    "trigger_type": "account_drawdown",
                    "trigger_value": abs(drawdown),
                    "threshold_value": 0.10,
                    "related_trade_ids": [int(t["id"]) for t in repo.recent_closed_trades(limit=20)],
                    "reason": "模拟盘总资金回撤超过 10%",
                },
            )
        )
    return {"ok": True, "triggered": bool(actions), "actions": actions, "cleaned_stale": cleaned, "cleaned_duplicates": duplicates_cleaned}


def _cleanup_stale_candidates(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Reject candidates that have been shadow_testing for > N days without enough samples."""
    from datetime import datetime, timezone, timedelta
    from plugins.crypto_guard.config.loader import load_config

    config = load_config()
    stale_cfg = (config.trading_mode.get("evolution") or {}).get("stale_cleanup") or {}
    max_days = int(stale_cfg.get("max_days", 7))

    # Get backtest-status-aware thresholds from online_shadow config
    online_shadow_cfg = (config.trading_mode.get("evolution") or {}).get("online_shadow") or {}
    min_samples_after_backtest = int(online_shadow_cfg.get("min_samples_after_backtest", 5))
    min_samples_without_backtest = int(online_shadow_cfg.get("min_samples_without_backtest", 30))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()
    stale = repo.conn.execute(
        "SELECT id, strategy_name, version FROM strategy_versions WHERE status='shadow_testing' AND created_at < %s::timestamptz",
        (cutoff,),
    ).fetchall()

    rejected = 0
    for row in stale:
        version = row["version"]
        strategy_name = row["strategy_name"]

        # Check backtest status to determine correct threshold
        backtest_row = repo.conn.execute(
            "SELECT backtest_result_json FROM strategy_patches WHERE strategy_name=%s AND candidate_version=%s AND backtest_result_json IS NOT NULL",
            (strategy_name, version),
        ).fetchone()

        effective_min_samples = min_samples_without_backtest  # Default: conservative
        if backtest_row and backtest_row["backtest_result_json"]:
            try:
                backtest = json.loads(backtest_row["backtest_result_json"]) if isinstance(backtest_row["backtest_result_json"], str) else backtest_row["backtest_result_json"]
                if backtest.get("passed") and not backtest.get("skipped"):
                    effective_min_samples = min_samples_after_backtest
                elif backtest.get("gate_disabled"):
                    effective_min_samples = min_samples_after_backtest
            except Exception:
                pass

        # Check if enough shadow evaluations exist
        count = repo.conn.execute(
            "SELECT COUNT(*) AS cnt FROM strategy_evaluations WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE",
            (strategy_name, version),
        ).fetchone()
        sample_count = int(count["cnt"]) if count else 0

        if sample_count < effective_min_samples:
            # Not enough samples after max_days - reject
            with repo.conn.transaction():
                repo.conn.execute(
                    "UPDATE strategy_versions SET status='rejected', change_reason=%s WHERE id=%s",
                    (f"shadow_testing 超过 {max_days} 天且样本不足 ({sample_count}/{effective_min_samples})，自动拒绝", int(row["id"])),
                )
                # Sync strategy_patches status
                repo.conn.execute(
                    "UPDATE strategy_patches SET status='rejected' WHERE candidate_version=%s AND status NOT IN ('rejected', 'duplicate')",
                    (version,),
                )
                # Also update the trigger - use trigger_id column from strategy_patches
                repo.conn.execute(
                    "UPDATE evolution_triggers SET status='rejected' WHERE id IN (SELECT trigger_id FROM strategy_patches WHERE candidate_version=%s AND trigger_id IS NOT NULL) AND status='shadow_testing'",
                    (version,),
                )
            rejected += 1

    return {"rejected_stale": rejected}


def _consecutive_stop_losses(repo: CryptoGuardRepository) -> dict[str, Any] | None:
    trades = repo.recent_closed_trades(limit=3)
    if len(trades) < 3:
        return None
    if all(str(t.get("close_reason")) == "stop_loss" for t in trades):
        return {
            "trigger_type": "consecutive_stop_losses",
            "trigger_value": 3,
            "threshold_value": 3,
            "related_trade_ids": [int(t["id"]) for t in trades],
            "symbol": trades[0].get("symbol") if len({t.get("symbol") for t in trades}) == 1 else None,
            "reason": "连续 3 次模拟盘止损",
        }
    return None


def _daily_loss_threshold(repo: CryptoGuardRepository) -> dict[str, Any] | None:
    """Trigger when 3+ stop losses occur in a single day."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = repo.conn.execute(
        "SELECT COUNT(*) AS cnt FROM paper_trades WHERE close_reason='stop_loss' AND COALESCE(closed_at, NOW())::date=%s::date",
        (today,),
    ).fetchone()
    count = int(row["cnt"]) if row else 0
    if count < 3:
        return None
    rows = repo.conn.execute(
        "SELECT id FROM paper_trades WHERE close_reason='stop_loss' AND COALESCE(closed_at, NOW())::date=%s::date",
        (today,),
    ).fetchall()
    trade_ids = [int(r["id"]) for r in rows]
    return {
        "trigger_type": "daily_loss_threshold",
        "trigger_value": count,
        "threshold_value": 3,
        "related_trade_ids": trade_ids,
        "reason": f"单日 {count} 笔止损，触发复盘与自进化评估",
    }


def _record_trigger_and_candidate(repo: CryptoGuardRepository, trigger: dict[str, Any]) -> dict[str, Any]:
    # P0: 止住重复创建 — 已有同类型未完成 trigger 时复用，不创建新 patch
    existing = repo.conn.execute(
        "SELECT id FROM evolution_triggers WHERE trigger_type=%s AND status IN ('pending','shadow_testing') AND COALESCE(symbol,'')=COALESCE(%s, '') ORDER BY id DESC LIMIT 1",
        (trigger["trigger_type"], trigger.get("symbol")),
    ).fetchone()

    if existing:
        existing_id = int(existing["id"])
        # Update latest_related_trade_ids to reflect the LATEST triggering trades,
        # and record latest_triggered_at. Preserve original_related_trade_ids.
        new_related_trade_ids = json.dumps(trigger.get("related_trade_ids") or [])
        from datetime import datetime, timezone as tz

        # Check if original_related_trade_ids exists; if not, backfill from current related_trade_ids
        existing_row = repo.conn.execute(
            "SELECT original_related_trade_ids, related_trade_ids FROM evolution_triggers WHERE id=%s",
            (existing_id,),
        ).fetchone()
        with repo.conn.transaction():
            if existing_row and not existing_row["original_related_trade_ids"]:
                # First reuse: freeze original from the existing related_trade_ids
                original = existing_row["related_trade_ids"]
                repo.conn.execute(
                    "UPDATE evolution_triggers SET original_related_trade_ids=%s, latest_related_trade_ids=%s, related_trade_ids=%s, latest_triggered_at=%s WHERE id=%s",
                    (original, new_related_trade_ids, new_related_trade_ids, datetime.now(tz.utc).isoformat(), existing_id),
                )
            else:
                repo.conn.execute(
                    "UPDATE evolution_triggers SET latest_related_trade_ids=%s, related_trade_ids=%s, latest_triggered_at=%s WHERE id=%s",
                    (new_related_trade_ids, new_related_trade_ids, datetime.now(tz.utc).isoformat(), existing_id),
                )
            # Also sync latest_trigger_value if the new trigger has it
            if trigger.get("trigger_value") is not None:
                repo.conn.execute(
                    "UPDATE evolution_triggers SET latest_trigger_value=%s WHERE id=%s",
                    (float(trigger["trigger_value"]), existing_id),
                )
        # Find the associated patch for this trigger
        existing_patch = repo.conn.execute(
            "SELECT id, candidate_version FROM strategy_patches WHERE trigger_id=%s ORDER BY id DESC LIMIT 1",
            (existing_id,),
        ).fetchone()
        return {
            "trigger_id": existing_id,
            "patch_id": int(existing_patch["id"]) if existing_patch else None,
            "status": "existing_trigger_reused",
            "candidate_version": existing_patch["candidate_version"] if existing_patch else None,
            "trigger": trigger,
        }

    # Detect actual strategy used in related trades
    strategy_name, from_version = _detect_strategy_from_trades(repo, trigger.get("related_trade_ids") or [])

    # Wrap trigger creation + patch creation + version creation + cap enforcement in a single transaction
    from plugins.crypto_guard.strategy.shadow_testing import _enforce_candidate_cap
    with repo.conn.transaction():
        trigger_id = repo.create_evolution_trigger(
            trigger_type=trigger["trigger_type"],
            trigger_value=float(trigger["trigger_value"]),
            threshold_value=float(trigger["threshold_value"]),
            related_trade_ids=trigger.get("related_trade_ids") or [],
            strategy_name=strategy_name,
            symbol=trigger.get("symbol"),
            evolution_allowed=True,
            status="shadow_testing",
        )
        candidate_version = f"v2-trigger-{trigger_id}"
        patch = {
            "strategy_name": strategy_name,
            "from_version": from_version,
            "candidate_version": candidate_version,
            "change_reason": trigger.get("reason", "自进化触发器创建 candidate patch"),
            "patch": {
                "status": "candidate",
                "paper_order_permission": "shadow_testing_only",
                "risk_controls": ["require_structure_momentum_alignment", "pause_after_trigger"],
                "trigger": trigger,
            },
        }
        patch_id = repo.save_strategy_patch_candidate(patch, evidence={"trigger": trigger, "trigger_id": trigger_id}, trigger_id=trigger_id, status="candidate")
        repo.save_strategy_version(
            strategy_name=patch["strategy_name"],
            version=patch["candidate_version"],
            status="candidate",
            config=patch["patch"],
            change_reason=patch["change_reason"],
        )
        # Enforce candidate cap AFTER creation, inside same transaction
        _enforce_candidate_cap(repo, strategy_name, max_candidates=5)

    # Run backtest gate immediately after candidate creation
    from plugins.crypto_guard.strategy.shadow_testing import run_backtest_gate
    try:
        backtest_result = run_backtest_gate(
            repo,
            strategy_name=strategy_name,
            candidate_version=candidate_version,
        )
    except Exception as exc:
        backtest_result = {
            "ok": False,
            "passed": False,
            "reason": "backtest_exception",
            "error": str(exc),
        }

    # If backtest truly fails (not skipped, not gate_disabled), reject the candidate immediately
    backtest_failed = (
        not backtest_result.get("passed")
        and not backtest_result.get("skipped")
        and not backtest_result.get("gate_disabled")
    )
    if backtest_failed:
        with repo.conn.transaction():
            repo.conn.execute(
                "UPDATE strategy_versions SET status='rejected', change_reason=%s WHERE strategy_name=%s AND version=%s",
                (f"回测门禁未通过：{backtest_result.get('reason', 'unknown')}", strategy_name, candidate_version),
            )
            repo.conn.execute(
                "UPDATE evolution_triggers SET status='rejected' WHERE id=%s",
                (trigger_id,),
            )
            # Save backtest result and update patch status
            repo.conn.execute(
                "UPDATE strategy_patches SET backtest_result_json=%s, status='rejected' WHERE id=%s",
                (_json_dumps_payload(backtest_result), patch_id),
            )
        return {
            "trigger_id": trigger_id,
            "patch_id": patch_id,
            "status": "rejected",
            "reason": "backtest_gate_failed",
            "candidate_version": candidate_version,
            "backtest_result": backtest_result,
            "trigger": trigger,
        }

    # Backtest passed or skipped - transition to shadow_testing and continue
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE strategy_versions SET status='shadow_testing' WHERE strategy_name=%s AND version=%s AND status='candidate'",
            (strategy_name, candidate_version),
        )
        repo.conn.execute(
            "UPDATE strategy_patches SET backtest_result_json=%s, status='shadow_testing' WHERE id=%s",
            (_json_dumps_payload(backtest_result), patch_id),
        )

    for skill in ("price_action", "momentum", "trend_stage", "smc_orderflow", "chanlun"):
        repo.save_skill_feedback_memory(
            skill_name=skill,
            feedback_type="evolution_trigger",
            source_type="evolution_trigger",
            source_id=trigger_id,
            finding=trigger.get("reason", "模拟盘触发自进化，需要复盘该 Skill 权重。"),
            suggested_adjustment={"candidate_patch_id": patch_id, "shadow_testing": True},
        )
    # Push notification for new trigger
    try:
        repo.enqueue_job(
            "evolution_trigger_alert",
            4,
            "paper_worker",
            f"system:evolution:new:{trigger_id}",
            {
                "trigger_type": trigger["trigger_type"],
                "trigger_id": trigger_id,
                "patch_id": patch_id,
                "reason": trigger.get("reason", ""),
                "related_trade_ids": trigger.get("related_trade_ids") or [],
                "trigger_value": trigger.get("trigger_value"),
                "threshold_value": trigger.get("threshold_value"),
            },
        )
    except Exception:
        pass
    return {"trigger_id": trigger_id, "patch_id": patch_id, "status": "shadow_testing", "trigger": trigger}


def _detect_strategy_from_trades(repo: CryptoGuardRepository, trade_ids: list[int]) -> tuple[str, str]:
    """Detect which strategy was used in the related trades.

    Returns (strategy_name, from_version). Falls back to defaults if detection fails.
    """
    default_name = "smc_pullback_long"
    default_version = "1.0"

    if not trade_ids:
        active = repo.active_strategy_version(default_name)
        return default_name, active.get("version", default_version) if active else default_version

    # Look up strategy info from GA decisions linked to these trades
    placeholders = ",".join("%s" for _ in trade_ids[:10])
    rows = repo.conn.execute(
        f"""
        SELECT DISTINCT po.ga_decision_id FROM paper_trades pt
        JOIN paper_orders po ON po.id = pt.order_id
        WHERE pt.id IN ({placeholders})
        """,
        trade_ids[:10],
    ).fetchall()

    for row in rows:
        ga_id = row["ga_decision_id"]
        if not ga_id:
            continue
        ga = repo.conn.execute(
            "SELECT raw_decision_json FROM ga_decisions WHERE id=%s",
            (int(ga_id),),
        ).fetchone()
        if ga:
            raw = _decode_json(ga["raw_decision_json"], {})
            name = raw.get("strategy_name") if isinstance(raw, dict) else None
            if name:
                version = raw.get("strategy_version", default_version)
                return str(name), str(version)

    # Fallback: use active version
    active = repo.active_strategy_version(default_name)
    return default_name, active.get("version", default_version) if active else default_version
