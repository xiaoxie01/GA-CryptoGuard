"""Pending order lifecycle management: TTL expiry and direction-conflict cancellation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.pending_order_manager")

# Default TTL per order type
TTL_CONFIG: dict[str, timedelta] = {
    "limit_pullback": timedelta(hours=8),
    "breakout": timedelta(hours=4),
    "retest": timedelta(hours=4),
    "swing": timedelta(hours=24),
}

DEFAULT_TTL = timedelta(hours=8)


def _ttl_for_order(order: dict[str, Any]) -> timedelta:
    """Return the TTL for a given order based on its order_type."""
    order_type = str(order.get("order_type") or "").lower()
    if order_type in TTL_CONFIG:
        return TTL_CONFIG[order_type]
    return DEFAULT_TTL


def expire_pending_orders(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Scan and expire pending orders that have exceeded their TTL."""
    now = datetime.now(timezone.utc)

    pending_orders = repo.conn.execute(
        "SELECT id, symbol, side, order_type, created_at, signal_id FROM paper_orders WHERE status='pending'"
    ).fetchall()

    expired: list[dict[str, Any]] = []
    for order in pending_orders:
        order = dict(order)
        created_at_str = order.get("created_at")
        if not created_at_str:
            continue
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        ttl = _ttl_for_order(order)
        if now - created_at > ttl:
            now_iso = now.isoformat()
            reason = f"TTL expired ({ttl})"
            repo.conn.execute(
                "UPDATE paper_orders SET status='expired', cancelled_at=?, cancel_reason=? WHERE id=?",
                (now_iso, reason, order["id"]),
            )
            expired.append(order)
            LOGGER.info(
                "expired pending order id=%s symbol=%s side=%s age=%s",
                order["id"], order["symbol"], order["side"], now - created_at,
            )

    repo.conn.commit()
    result: dict[str, Any] = {"ok": True, "expired_count": len(expired), "expired_orders": expired}
    if expired:
        LOGGER.info("expire_pending_orders result: expired %d orders", len(expired))
    return result


def cancel_conflict_pending_orders(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Cancel pending orders whose side conflicts with the latest GA decision bias."""
    now = datetime.now(timezone.utc)

    pending_orders = repo.conn.execute(
        "SELECT id, symbol, side, signal_id FROM paper_orders WHERE status='pending'"
    ).fetchall()

    cancelled: list[dict[str, Any]] = []
    for order in pending_orders:
        order = dict(order)
        symbol = order["symbol"]
        side = str(order["side"] or "").upper()

        # Get the latest GA decision for this symbol
        latest_decision = repo.conn.execute(
            "SELECT market_bias, signal_grade FROM ga_decisions WHERE symbol=? ORDER BY analysis_time_utc DESC LIMIT 1",
            (symbol,),
        ).fetchone()

        if not latest_decision:
            continue

        bias = str(latest_decision["market_bias"] or "neutral").lower()
        grade = str(latest_decision["signal_grade"] or "D").upper()

        # Conflict: SHORT pending but bullish with strong grade, or LONG pending but bearish
        conflict = False
        if side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"}:
            conflict = True
        elif side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"}:
            conflict = True

        if conflict:
            now_iso = now.isoformat()
            reason = f"Direction conflict: {side} vs {bias} ({grade})"
            repo.conn.execute(
                "UPDATE paper_orders SET status='conflict_cancelled', cancelled_at=?, cancel_reason=? WHERE id=?",
                (now_iso, reason, order["id"]),
            )
            cancelled.append(order)
            LOGGER.info(
                "conflict cancelled pending order id=%s symbol=%s side=%s bias=%s grade=%s",
                order["id"], symbol, side, bias, grade,
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

    This is intended for immediate cleanup of accumulated stale pending orders.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=max_age_hours)).isoformat()

    stale = repo.conn.execute(
        "SELECT id, symbol, side, created_at FROM paper_orders WHERE status='pending' AND created_at < ?",
        (cutoff,),
    ).fetchall()

    if not stale:
        return {"ok": True, "cleaned": 0}

    repo.conn.execute(
        "UPDATE paper_orders SET status='expired', cancelled_at=?, cancel_reason=? WHERE status='pending' AND created_at < ?",
        (now.isoformat(), f"Manual cleanup: TTL expired (>{max_age_hours}h)", cutoff),
    )
    repo.conn.commit()

    LOGGER.info("cleanup_stale_pending: cleaned %d orders older than %dh", len(stale), max_age_hours)
    return {"ok": True, "cleaned": len(stale), "orders": [dict(r) for r in stale]}


def notify_order_cancelled(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    reason: str,
    send_message: Any = None,
    receive_id: str | None = None,
    receive_id_type: str = "chat_id",
) -> dict[str, Any]:
    """Enqueue an alert for a cancelled/expired pending order.

    If send_message and receive_id are provided, sends immediately.
    Otherwise, enqueues to alert_outbox for later delivery.
    """
    from plugins.crypto_guard.notify.alert_delivery import send_markdown_alert

    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(order.get("side") or "").upper(), order.get("side") or "-")
    alert_type = "paper_order_expired" if "expired" in reason.lower() else "conflict_cancelled"

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

    if send_message and receive_id:
        sent = send_markdown_alert(
            repo, send_message,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            text=text,
            alert_type=alert_type,
            symbol=order.get("symbol"),
            priority=3,
        )
        return {"ok": True, "sent": bool(sent.get("sent")), "text": text}

    # Enqueue to outbox for background delivery
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=order.get("symbol"),
        priority=3,
        payload={
            "msg_type": "text",
            "content": text,
        },
    )
    return {"ok": True, "queued": True, "alert_id": alert_id, "text": text}


def run_pending_order_management(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Run all pending order lifecycle checks: TTL expiry + conflict cancellation.

    Called periodically by the scheduler (every 60 minutes).
    """
    expire_result = expire_pending_orders(repo)
    conflict_result = cancel_conflict_pending_orders(repo)

    # Send notifications for expired/cancelled orders
    for order in expire_result.get("expired_orders", []):
        notify_order_cancelled(repo, order, f"TTL expired ({_ttl_for_order(order)})")
    for order in conflict_result.get("cancelled_orders", []):
        notify_order_cancelled(repo, order, order.get("cancel_reason", "Direction conflict"))

    return {
        "ok": True,
        "expire": expire_result,
        "conflict": conflict_result,
    }
