"""Pending order lifecycle management: TTL expiry and direction-conflict cancellation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.pending_order_manager")

# TTL per entry_type (from trade_plan.entry_type)
TTL_CONFIG: dict[str, timedelta] = {
    "limit": timedelta(hours=8),
    "trigger": timedelta(hours=4),
    "market": timedelta(minutes=10),
}

DEFAULT_TTL = timedelta(hours=8)


def ttl_for_entry_type(entry_type: str | None) -> timedelta:
    """Return the TTL for a given entry_type."""
    key = str(entry_type or "").lower()
    return TTL_CONFIG.get(key, DEFAULT_TTL)


def compute_expires_at(entry_type: str | None, created_at: datetime | None = None) -> str:
    """Compute the UTC ISO expiration time for an order."""
    base = created_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    ttl = ttl_for_entry_type(entry_type)
    return (base + ttl).isoformat()


def expire_pending_orders(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Scan and expire pending orders that have exceeded their TTL.

    Uses expires_at field when available, falls back to created_at + TTL.
    """
    now = datetime.now(timezone.utc)

    pending_orders = repo.conn.execute(
        "SELECT id, symbol, side, order_type, created_at, expires_at, signal_id FROM paper_orders WHERE status='pending'"
    ).fetchall()

    expired: list[dict[str, Any]] = []
    for order in pending_orders:
        order = dict(order)
        expires_at_str = order.get("expires_at")

        if expires_at_str:
            # Use stored expires_at
            try:
                expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if now > expires_at:
                    expires_at_utc8 = expires_at.astimezone(timezone(timedelta(hours=8)))
                    reason = f"挂单已超过有效期（到期时间：{expires_at_utc8.strftime('%m-%d %H:%M')} UTC+8）"
                    _expire_order(repo, order, now, reason)
                    expired.append(order)
            except (ValueError, TypeError):
                pass  # Skip malformed expires_at
        else:
            # Fallback: compute from created_at + TTL
            created_at_str = order.get("created_at")
            if not created_at_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                ttl = ttl_for_entry_type(order.get("order_type"))
                if now - created_at > ttl:
                    hours = int(ttl.total_seconds() // 3600)
                    reason = f"挂单已超过{hours}小时有效期"
                    _expire_order(repo, order, now, reason)
                    expired.append(order)
            except (ValueError, TypeError):
                continue

    repo.conn.commit()
    result: dict[str, Any] = {"ok": True, "expired_count": len(expired), "expired_orders": expired}
    if expired:
        LOGGER.info("expire_pending_orders result: expired %d orders", len(expired))
    return result


def _expire_order(repo: CryptoGuardRepository, order: dict[str, Any], now: datetime, reason: str) -> None:
    now_iso = now.isoformat()
    repo.conn.execute(
        "UPDATE paper_orders SET status='expired', cancelled_at=?, cancel_reason=? WHERE id=?",
        (now_iso, reason, order["id"]),
    )
    order["cancel_reason"] = reason
    order["status"] = "expired"
    LOGGER.info(
        "expired pending order id=%s symbol=%s side=%s reason=%s",
        order["id"], order["symbol"], order["side"], reason,
    )


def cancel_conflict_pending_orders(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Cancel pending orders whose side conflicts with the latest GA decision bias."""
    now = datetime.now(timezone.utc)

    pending_orders = repo.conn.execute(
        "SELECT id, symbol, side, signal_id, expires_at FROM paper_orders WHERE status='pending'"
    ).fetchall()

    cancelled: list[dict[str, Any]] = []
    for order in pending_orders:
        order = dict(order)
        symbol = order["symbol"]
        side = str(order["side"] or "").upper()

        # Get the latest GA decision for this symbol
        latest_decision = repo.conn.execute(
            "SELECT id, market_bias, signal_grade FROM ga_decisions WHERE symbol=? ORDER BY analysis_time_utc DESC LIMIT 1",
            (symbol,),
        ).fetchone()

        if not latest_decision:
            continue

        bias = str(latest_decision["market_bias"] or "neutral").lower()
        grade = str(latest_decision["signal_grade"] or "D").upper()
        ga_decision_id = int(latest_decision["id"])
        side_cn = {"LONG": "做多", "SHORT": "做空"}.get(side, side)
        bias_cn = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "mixed": "混杂"}.get(bias, bias)

        # Conflict: SHORT pending but bullish with strong grade, or LONG pending but bearish
        conflict = False
        if side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"}:
            conflict = True
        elif side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"}:
            conflict = True

        if conflict:
            now_iso = now.isoformat()
            reason = f"方向冲突：{side_cn} vs {bias_cn}（{grade}级）"
            repo.conn.execute(
                "UPDATE paper_orders SET status='conflict_cancelled', cancelled_at=?, cancel_reason=?, invalidated_by_ga_decision_id=? WHERE id=?",
                (now_iso, reason, ga_decision_id, order["id"]),
            )
            cancelled.append(order)
            LOGGER.info(
                "conflict cancelled pending order id=%s symbol=%s side=%s bias=%s grade=%s ga_decision_id=%s",
                order["id"], symbol, side, bias, grade, ga_decision_id,
            )
        elif bias in ("neutral", "mixed"):
            # Neutral/mixed bias: mark for recheck, don't cancel
            repo.conn.execute(
                "UPDATE paper_orders SET status='needs_recheck' WHERE id=? AND status='pending'",
                (order["id"],),
            )
            LOGGER.info(
                "marked needs_recheck: pending order id=%s symbol=%s side=%s bias=%s",
                order["id"], symbol, side, bias,
            )

    repo.conn.commit()
    result: dict[str, Any] = {"ok": True, "cancelled_count": len(cancelled), "cancelled_orders": cancelled}
    if cancelled:
        LOGGER.info("cancel_conflict_pending_orders result: cancelled %d orders", len(cancelled))
    return result


def cleanup_stale_pending(repo: CryptoGuardRepository, max_age_hours: int = 24) -> dict[str, Any]:
    """One-shot cleanup: expire ALL pending orders older than max_age_hours.

    Uses Python datetime parsing instead of SQL string comparison to avoid
    format mismatches between ISO and SQLite CURRENT_TIMESTAMP.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    stale = repo.conn.execute(
        "SELECT id, symbol, side, created_at FROM paper_orders WHERE status='pending'",
    ).fetchall()

    to_expire: list[dict[str, Any]] = []
    for row in stale:
        created_at_str = row["created_at"]
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                to_expire.append(dict(row))
        except (ValueError, TypeError):
            continue

    if not to_expire:
        return {"ok": True, "cleaned": 0}

    now_iso = now.isoformat()
    reason = f"手动清理：挂单滞留超过{max_age_hours}小时"
    for order in to_expire:
        repo.conn.execute(
            "UPDATE paper_orders SET status='expired', cancelled_at=?, cancel_reason=? WHERE id=? AND status='pending'",
            (now_iso, reason, order["id"]),
        )
    repo.conn.commit()

    LOGGER.info("cleanup_stale_pending: cleaned %d orders older than %dh", len(to_expire), max_age_hours)
    return {"ok": True, "cleaned": len(to_expire), "orders": to_expire}


def notify_order_cancelled(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    reason: str,
    send_message: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Send or enqueue a notification for a cancelled/expired pending order.

    Uses resolve_report_target() for receive_id and send_markdown_alert()
    for consistent delivery via alert_outbox.
    """
    from plugins.crypto_guard.notify.alert_delivery import send_markdown_alert
    from plugins.crypto_guard.notify.hourly_report import resolve_report_target

    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(order.get("side") or "").upper(), order.get("side") or "-")
    status = str(order.get("status") or "").lower()
    alert_type = "paper_order_expired" if status == "expired" else "conflict_cancelled"

    lines = [
        "**模拟盘挂单已取消**",
        "",
        f"- 产品：{order.get('symbol', '-')}",
        f"- 方向：{side_cn}",
        f"- 订单：#{order.get('id', '-')}",
        f"- 原因：{reason}",
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)

    target = resolve_report_target(repo)
    if not target:
        LOGGER.warning("notify_order_cancelled: no receive_id available, skipping notification for order %s", order.get("id"))
        return {"ok": True, "sent": False, "queued": False, "reason": "no_target"}

    sent = send_markdown_alert(
        repo,
        send_message,
        receive_id=target["receive_id"],
        receive_id_type=target.get("receive_id_type", "chat_id"),
        text=text,
        alert_type=alert_type,
        symbol=order.get("symbol"),
        priority=3,
    )
    return {"ok": True, "sent": bool(sent.get("sent")), "queued": bool(sent.get("queued")), "text": text}


def force_risk_off_pending_revalidation(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Force-convert all pending orders to watch when account is in hard_risk_off or daily_loss_pause.

    Under pause conditions, no new paper orders are allowed — only opportunity_watch / monitor_only.
    """
    from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard

    guard = AccountRiskGuard(repo)
    account_risk = guard.check(symbol="", side="")
    if not account_risk.get("pause_active"):
        return {"ok": True, "converted_count": 0, "converted_orders": [], "pause_active": False}

    now = datetime.now(timezone.utc)
    pause_reason = account_risk.get("pause_reason", "账户暂停开仓")

    pending_orders = repo.conn.execute(
        "SELECT id, symbol, side, signal_id, ga_decision_id FROM paper_orders WHERE status IN ('pending', 'needs_recheck')"
    ).fetchall()

    converted: list[dict[str, Any]] = []
    for row in pending_orders:
        order = dict(row)
        now_iso = now.isoformat()
        reason = f"账户风控暂停（{pause_reason}）"
        repo.conn.execute(
            "UPDATE paper_orders SET status='risk_off_cancelled', cancelled_at=?, cancel_reason=? WHERE id=?",
            (now_iso, reason, order["id"]),
        )
        # Create opportunity_watch entry so the signal isn't lost
        _create_watch_from_risk_off(repo, order, reason, now)
        converted.append(order)
        LOGGER.info(
            "risk_off cancelled pending order id=%s symbol=%s side=%s",
            order["id"], order["symbol"], order["side"],
        )

    repo.conn.commit()

    LOGGER.info("force_risk_off_pending_revalidation: converted %d orders", len(converted))
    return {
        "ok": True,
        "converted_count": len(converted),
        "converted_orders": converted,
        "pause_active": True,
        "pause_reason": pause_reason,
    }


def _create_watch_from_risk_off(repo: CryptoGuardRepository, order: dict[str, Any], reason: str, now: datetime) -> None:
    """Create an opportunity_watch entry from a risk_off cancelled order."""
    now_iso = now.isoformat()
    try:
        repo.conn.execute(
            """
            INSERT INTO opportunity_watches(symbol, direction, ga_decision_id, watch_reason, watch_condition_json, status, created_at)
            VALUES (?, ?, ?, ?, '{}', 'active', ?)
            """,
            (order.get("symbol", ""), order.get("side", ""), order.get("ga_decision_id"),
             f"账户风控暂停：{reason}", now_iso),
        )
    except Exception as e:
        LOGGER.debug("_create_watch_from_risk_off failed: %s", e)


def run_pending_order_management(
    repo: CryptoGuardRepository,
    send_message: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run all pending order lifecycle checks: TTL expiry + conflict cancellation + risk_off revalidation.

    Called periodically by the scheduler (every 60 minutes).
    """
    expire_result = expire_pending_orders(repo)
    conflict_result = cancel_conflict_pending_orders(repo)
    risk_off_result = force_risk_off_pending_revalidation(repo)

    # Send notifications for expired/cancelled orders
    for order in expire_result.get("expired_orders", []):
        notify_order_cancelled(repo, order, order.get("cancel_reason", "挂单已超过有效期"), send_message=send_message)
    for order in conflict_result.get("cancelled_orders", []):
        notify_order_cancelled(repo, order, order.get("cancel_reason", "方向冲突取消"), send_message=send_message)
    for order in risk_off_result.get("converted_orders", []):
        notify_order_cancelled(repo, order, order.get("cancel_reason", "账户风控暂停"), send_message=send_message)

    return {
        "ok": True,
        "expire": expire_result,
        "conflict": conflict_result,
        "risk_off": risk_off_result,
    }
