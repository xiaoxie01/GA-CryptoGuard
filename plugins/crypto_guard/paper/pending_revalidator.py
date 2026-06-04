"""Pending order revalidator: multi-dimensional review of pending and needs_recheck orders.

Conservative rules:
1. Conflict cancel — side conflicts with strong GA bias (already in pending_order_manager, but re-checks here for needs_recheck)
2. Late trend stage — convert to watch
3. Price deviation too large — convert to watch
4. needs_recheck timeout — convert to watch after max hours

Run hourly by the scheduler alongside pending_order_management.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.pending_revalidator")

# Maximum time an order can stay in needs_recheck before being converted to watch
NEEDS_RECHECK_MAX_HOURS = 4

# Price deviation thresholds (% from entry/trigger price)
PRICE_DEVIATION_WATCH_PCT = 3.0  # convert to watch if price moved > 3% from entry
PRICE_DEVIATION_CANCEL_PCT = 6.0  # cancel if price moved > 6% from entry

# Trend stages considered "late" — risky for pending entries
# Note: "transition" is excluded — it's neutral, not a reason to block
LATE_TREND_STAGES = {"late", "exhausted"}


def revalidate_pending_orders(
    repo: CryptoGuardRepository,
    send_message: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Scan pending and needs_recheck orders and apply multi-dimensional review.

    Returns summary of actions taken.
    """
    now = datetime.now(timezone.utc)

    # Get all pending and needs_recheck orders
    orders = repo.conn.execute(
        """
        SELECT id, symbol, side, order_type, entry_price, trigger_price, stop_loss,
               signal_id, ga_decision_id, status, created_at, expires_at
        FROM paper_orders
        WHERE status IN ('pending', 'needs_recheck')
        """
    ).fetchall()

    actions: list[dict[str, Any]] = []
    for row in orders:
        order = dict(row)
        action = _review_order(repo, order, now)
        if action["action"] != "keep":
            _apply_action(repo, order, action, now)
            actions.append({"order_id": order["id"], "symbol": order["symbol"], "side": order["side"], **action})
            LOGGER.info(
                "revalidator: order %d %s/%s -> %s (reason: %s)",
                order["id"], order["symbol"], order["side"], action["action"], action.get("reason"),
            )

    repo.conn.commit()

    # Send notifications for converted/cancelled orders
    if send_message:
        for act in actions:
            if act["action"] in ("cancel", "convert_to_watch"):
                _notify_action(repo, act, send_message)

    return {
        "ok": True,
        "reviewed_count": len(orders),
        "actions_count": len(actions),
        "actions": actions,
    }


def _review_order(repo: CryptoGuardRepository, order: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Apply conservative review rules to a single order.

    Rules (in priority order — highest first):
    1. Conflict with strong GA bias → cancel (most dangerous, cancel immediately)
    2. Late trend stage → convert_to_watch
    3. Large price deviation → convert_to_watch or cancel
    4. needs_recheck timeout → convert_to_watch (least urgent)
    """
    order_id = order["id"]
    symbol = order["symbol"]
    side = str(order["side"] or "").upper()
    status = str(order["status"] or "").lower()

    # Get latest GA decision for context (needed by rules 1-3)
    ga_decision = _latest_ga_decision(repo, symbol)

    # Rule 1: Conflict with strong GA bias → cancel immediately
    if ga_decision:
        bias = str(ga_decision.get("market_bias") or "neutral").lower()
        grade = str(ga_decision.get("signal_grade") or "D").upper()
        conflict = False
        if side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"}:
            conflict = True
        elif side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"}:
            conflict = True
        if conflict:
            side_cn = {"LONG": "做多", "SHORT": "做空"}.get(side, side)
            bias_cn = {"bullish": "偏多", "bearish": "偏空"}.get(bias, bias)
            ctx = _ga_context(ga_decision)
            return {
                "action": "cancel",
                "reason": f"方向冲突：{side_cn} vs {bias_cn}（{grade}级）— {ctx}",
                "ga_decision_id": ga_decision["id"],
            }

    # Rule 2: Late trend stage
    if ga_decision:
        trend_stage = str(ga_decision.get("trend_stage") or "").lower()
        if trend_stage in LATE_TREND_STAGES:
            ctx = _ga_context(ga_decision)
            return {
                "action": "convert_to_watch",
                "reason": f"趋势阶段已进入 {trend_stage}，不适合新开仓 — {ctx}",
                "ga_decision_id": ga_decision["id"],
            }

    # Rule 3: Price deviation
    current_price = _latest_price(repo, symbol)
    entry_ref = _entry_reference_price(order)
    if current_price and entry_ref and entry_ref > 0:
        deviation_pct = abs(current_price - entry_ref) / entry_ref * 100.0
        if deviation_pct >= PRICE_DEVIATION_CANCEL_PCT:
            return {
                "action": "cancel",
                "reason": f"价格偏离 {deviation_pct:.1f}% 超过 {PRICE_DEVIATION_CANCEL_PCT}%，取消",
                "deviation_pct": deviation_pct,
            }
        if deviation_pct >= PRICE_DEVIATION_WATCH_PCT:
            return {
                "action": "convert_to_watch",
                "reason": f"价格偏离 {deviation_pct:.1f}% 超过 {PRICE_DEVIATION_WATCH_PCT}%，转观察",
                "deviation_pct": deviation_pct,
            }

    # Rule 4: needs_recheck timeout (lowest priority)
    if status == "needs_recheck":
        created_at_str = order.get("created_at")
        if created_at_str:
            try:
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if now - created_at > timedelta(hours=NEEDS_RECHECK_MAX_HOURS):
                    return {
                        "action": "convert_to_watch",
                        "reason": f"needs_recheck 超时超过{NEEDS_RECHECK_MAX_HOURS}小时，转观察",
                    }
            except (ValueError, TypeError):
                pass

    return {"action": "keep"}


def _apply_action(repo: CryptoGuardRepository, order: dict[str, Any], action: dict[str, Any], now: datetime) -> None:
    """Apply the review action to the order in the database."""
    now_iso = now.isoformat()
    order_id = order["id"]
    reason = action.get("reason", "")
    act = action["action"]

    if act == "cancel":
        ga_decision_id = action.get("ga_decision_id")
        repo.conn.execute(
            "UPDATE paper_orders SET status='revalidator_cancelled', cancelled_at=?, cancel_reason=?, invalidated_by_ga_decision_id=? WHERE id=?",
            (now_iso, reason, ga_decision_id, order_id),
        )
    elif act == "convert_to_watch":
        repo.conn.execute(
            "UPDATE paper_orders SET status='watch_cancelled', cancelled_at=?, cancel_reason=? WHERE id=?",
            (now_iso, reason, order_id),
        )
        # Create opportunity_watch entry so the signal isn't lost
        _create_watch_from_order(repo, order, reason)
    elif act == "needs_manual_review":
        repo.conn.execute(
            "UPDATE paper_orders SET status='needs_manual_review' WHERE id=? AND status IN ('pending', 'needs_recheck')",
            (order_id,),
        )


def _create_watch_from_order(repo: CryptoGuardRepository, order: dict[str, Any], reason: str) -> None:
    """Create an opportunity_watch entry from a cancelled pending order."""
    symbol = order.get("symbol", "")
    side = order.get("side", "")
    ga_decision_id = order.get("ga_decision_id")
    if not ga_decision_id:
        signal_id = order.get("signal_id")
        if signal_id:
            sig = repo.conn.execute("SELECT ga_decision_id FROM signals WHERE id=?", (signal_id,)).fetchone()
            if sig:
                ga_decision_id = sig["ga_decision_id"]

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        repo.conn.execute(
            """
            INSERT INTO opportunity_watches(symbol, direction, ga_decision_id, watch_reason, watch_condition_json, status, created_at)
            VALUES (?, ?, ?, ?, '{}', 'active', ?)
            """,
            (symbol, side, ga_decision_id, f"pending转观察：{reason}", now_iso),
        )
    except Exception as e:
        LOGGER.debug("create_watch_from_order failed: %s", e)


def _latest_ga_decision(repo: CryptoGuardRepository, symbol: str) -> dict[str, Any] | None:
    row = repo.conn.execute(
        "SELECT id, market_bias, signal_grade, trend_stage, confidence, analysis_time_utc FROM ga_decisions WHERE symbol=? ORDER BY analysis_time_utc DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def _ga_context(ga_decision: dict[str, Any]) -> str:
    """Format GA decision context for reason strings."""
    ga_id = ga_decision.get("id", "?")
    grade = ga_decision.get("signal_grade", "?")
    bias = ga_decision.get("market_bias", "?")
    conf = ga_decision.get("confidence", 0)
    ts = str(ga_decision.get("analysis_time_utc", ""))[:16]
    return f"GA#{ga_id} {grade}级 {bias} conf={conf:.2f} @ {ts}"


def _latest_price(repo: CryptoGuardRepository, symbol: str) -> float | None:
    row = repo.conn.execute(
        "SELECT close FROM candles WHERE symbol=? ORDER BY close_time DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return float(row["close"]) if row else None


def _entry_reference_price(order: dict[str, Any]) -> float | None:
    """Get the reference entry price for deviation check."""
    trigger = order.get("trigger_price")
    if trigger and float(trigger) > 0:
        return float(trigger)
    entry = order.get("entry_price")
    if entry and float(entry) > 0:
        return float(entry)
    return None


def _notify_action(
    repo: CryptoGuardRepository,
    action: dict[str, Any],
    send_message: Callable[..., Any] | None,
) -> None:
    """Send notification for revalidator actions."""
    if not send_message:
        return

    from plugins.crypto_guard.notify.alert_delivery import send_markdown_alert
    from plugins.crypto_guard.notify.hourly_report import resolve_report_target

    order_id = action.get("order_id", "-")
    symbol = action.get("symbol", "-")
    side = action.get("side", "-")
    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(side).upper(), side)
    act = action["action"]
    reason = action.get("reason", "")
    alert_type = "paper_order_expired" if act == "cancel" else "conflict_cancelled"

    lines = [
        "**模拟盘挂单复核**",
        "",
        f"- 产品：{symbol}",
        f"- 方向：{side_cn}",
        f"- 订单：#{order_id}",
        f"- 动作：{'取消' if act == 'cancel' else '转观察'}",
        f"- 原因：{reason}",
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)

    target = resolve_report_target(repo)
    if not target:
        return

    send_markdown_alert(
        repo,
        send_message,
        receive_id=target["receive_id"],
        receive_id_type=target.get("receive_id_type", "chat_id"),
        text=text,
        alert_type=alert_type,
        symbol=symbol,
        priority=3,
    )
