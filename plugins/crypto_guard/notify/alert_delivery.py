from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.notify.markdown_cards import build_markdown_card_json
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path


DEFAULT_NEVER_SILENCE = {
    "open_position",
    "close_position",
    "stop_loss_adjustment",
    "take_profit_hit",
    "stop_loss_hit",
    "risk_alert",
    # 08-04 contract A: opportunity_triggered is removed. A watch lifecycle
    # event is internal-only evidence and must remain silenceable, so a stray
    # legacy push can be deduped/suppressed.
    "paper_order_filled",
    "paper_order_expired",
    "evolution_trigger",
}

# Truly periodic alert types may share a fixed dedupe_key (symbol:alert_type):
# they are produced at most once per cycle per symbol, so a stable key enables
# the pending-only dedup check in enqueue_alert to collapse retry storms.
# Every OTHER alert_type is a one-shot event (a fill, a stop move, etc.) and
# must NOT use a fixed key — otherwise two simultaneously-pending events
# sharing the same (symbol, alert_type) would collide and the second enqueue
# would be wrongly rejected by the pending-only dedup check.
PERIODIC_ALERT_TYPES = {
    "hourly_summary",
    "daily_summary",
    "weekly_summary",
}


def send_markdown_alert(
    repo: CryptoGuardRepository,
    send_message: Callable[..., Any] | None,
    *,
    receive_id: str,
    receive_id_type: str,
    text: str,
    alert_type: str,
    symbol: str | None = None,
    priority: int = 5,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    cfg = load_config().trading_mode
    feishu_cfg = cfg.get("feishu", {})
    quiet = (feishu_cfg.get("quiet_period") or {})
    quiet_minutes = int(quiet.get("normal_duplicate_alert_minutes", 5))
    never = set(quiet.get("never_silence") or DEFAULT_NEVER_SILENCE)
    # 07-16 cutover: Redis eligibility no longer depends on the (removed) SQLite
    # file path. PostgreSQL is a single shared durable DB, so Redis is always
    # eligible unless explicitly disabled via env (``should_use_redis_for_path(None)``
    # encodes the production case -- matches feishu_integration.py / run_ga_workers.py).
    use_redis = should_use_redis_for_path(None)
    redis = RedisAdapter() if use_redis else None
    if alert_type not in never and redis and redis.is_quiet(symbol or "-", alert_type):
        return {"ok": True, "sent": False, "silenced": True}
    if repo.should_silence_alert(alert_type=alert_type, symbol=symbol, quiet_minutes=quiet_minutes, never_silence=never):
        return {"ok": True, "sent": False, "silenced": True}
    if alert_type not in never:
        lock_name = f"alert_dedupe:{symbol or '-'}:{alert_type}"
        ttl = max(quiet_minutes * 60, 1)
        redis_locked = bool(redis and redis.acquire_lock(f"job:{lock_name}", ttl, owner="alert_delivery"))
        if not (redis_locked or repo.acquire_lock(lock_name, "alert_delivery", ttl)):
            return {"ok": True, "sent": False, "silenced": True}
        if redis:
            redis.set_quiet(symbol or "-", alert_type, ttl)

    payload = {
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "msg_type": "interactive",
        "content": build_markdown_card_json(text),
        "fallback_text": text,
    }
    # Only genuinely periodic alerts reuse a time-bucketed dedupe_key so that
    # successive periods (e.g. 14:00 vs 15:00) produce independent pending rows
    # even when the dispatcher is slow. A fixed key like "-:hourly_summary"
    # would make the next period's enqueue collide with the previous period's
    # pending row and silently reuse the stale payload.
    now_utc = datetime.now(timezone.utc)
    if alert_type in PERIODIC_ALERT_TYPES:
        if alert_type == "hourly_summary":
            default_dedupe_key = f"hourly_summary:{now_utc.strftime('%Y-%m-%dT%H')}"
        elif alert_type == "daily_summary":
            default_dedupe_key = f"daily_summary:{now_utc.strftime('%Y-%m-%d')}"
        elif alert_type == "weekly_summary":
            default_dedupe_key = f"weekly_summary:{now_utc.strftime('%Y-W%W')}"
        else:
            default_dedupe_key = f"{symbol or '-'}:{alert_type}"
    else:
        default_dedupe_key = None
    resolved_dedupe_key = dedupe_key or default_dedupe_key
    # 08-12 P2-2 (fresh reviewer Recommended-1): a daily_review:<date>
    # delivery is once-ever — that guarantee lives in the outbox QUEUE's
    # claim -> send -> finalize sequence (process_alert_outbox). A direct
    # send here bypasses the claim entirely: a crash between the send and
    # the finalize leaves the row claimable again, and a second run would
    # send AGAIN (the production defect this task repairs). Reject it
    # BEFORE enqueue so no intent row is left behind.
    if resolved_dedupe_key and resolved_dedupe_key.startswith("daily_review:") and send_message is not None:
        raise ValueError(
            "daily_review:<date> deliveries must go through the outbox queue "
            "(process_alert_outbox / send_message=None): a direct send cannot "
            "guarantee the at-most-once once-ever contract"
        )
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=symbol,
        priority=priority,
        payload=payload,
        dedupe_key=resolved_dedupe_key,
    )
    if not send_message:
        # 08-12 P1 (Codex P2-1): with send_message=None the enqueue result is
        # ambiguous — enqueue_alert returns the ORIGINAL row id on a
        # daily_review:<date> dedupe hit in ANY state. Only a 'pending' row is
        # a real queue slot; a sent/sending/failed hit means the once-ever
        # delivery already happened or was fail-closed, so report deduped —
        # never queued=true on a row the dispatcher must not claim.
        status = repo.alert_outbox_status(alert_id)
        if status and status["status"] != "pending":
            return {"ok": True, "sent": False, "deduped": True, "alert_id": alert_id, "status": status["status"]}
        return {"ok": True, "sent": False, "queued": True, "alert_id": alert_id}
    # 08-12 P1: enqueue is the cross-state dedupe gate — a daily_review
    # re-enqueue returns the ORIGINAL row id whatever its state; only a
    # 'pending' row may be delivered. Anything else (sent/sending/failed
    # after a crash, rollback or restart) means the once-ever delivery
    # already happened or was fail-closed: report deduped, never a second
    # external send.
    status = repo.alert_outbox_status(alert_id)
    if status and status["status"] != "pending":
        return {"ok": True, "sent": False, "deduped": True, "alert_id": alert_id, "status": status["status"]}
    return _deliver_alert(repo, alert_id, payload, send_message, resolved_dedupe_key)


def process_alert_outbox(repo: CryptoGuardRepository, send_message: Callable[..., Any] | None, *, limit: int = 10) -> dict[str, Any]:
    if not send_message:
        return {"ok": True, "processed": 0, "sent": 0, "failed": 0}
    # 08-12 P1: reclaim dispatcher crashes first, fail-closed (terminal
    # 'failed' + alert_failure_log; such rows are NEVER re-sent — the external
    # side effect may already have happened).
    repo.recover_stale_sending_alerts()
    processed = sent = failed = 0
    for row in repo.claim_pending_alerts(limit=limit):
        processed += 1
        # Dispatcher transaction boundary: the atomic claim must be durable
        # BEFORE the external send. Committing here closes the implicit
        # transaction / savepoint (psycopg3) left open by the enqueue, so the
        # side effect below is never inside a business transaction that a
        # later failure could roll back.
        repo.conn.commit()
        # 07-16 cutover: ``payload_json`` is a JSONB column, so psycopg3 returns
        # it as an already-decoded dict (NOT str). ``json.loads(dict)`` raises
        # TypeError -> ``_deliver_alert`` never runs -> every alert silently
        # fails to send. Pass dict/list through; only parse str.
        raw_payload = row["payload_json"]
        payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload or "{}")
        result = _deliver_alert(repo, int(row["id"]), payload, send_message, row.get("dedupe_key"))
        # Finalize (mark_alert_sent / mark_alert_failed) is its own short
        # transaction; commit it immediately so a racing dispatcher never
        # blocks on an uncommitted row lock (savepoint-in-implicit-txn
        # deadlock pattern) and a later failure cannot roll the finalize back.
        repo.conn.commit()
        if result.get("sent"):
            sent += 1
        elif result.get("failed"):
            failed += 1
    return {"ok": True, "processed": processed, "sent": sent, "failed": failed}


def _deliver_alert(
    repo: CryptoGuardRepository,
    alert_id: int,
    payload: dict[str, Any],
    send_message: Callable[..., Any],
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    try:
        sent = send_message(
            payload["receive_id"],
            payload["content"],
            msg_type=payload.get("msg_type", "interactive"),
            receive_id_type=payload.get("receive_id_type", "chat_id"),
        )
    except Exception as exc:
        # 08-12 P1 (Codex P1-1): for a daily_review send the outcome is
        # UNKNOWN — the provider may have accepted the request before the
        # client raised (timeout reading the response). There is no upstream
        # provider idempotency key, so at-most-once and exactly-once cannot be
        # jointly held; duplicate daily pushes must be avoided first:
        # TERMINAL 'failed' + reason code, never recycled to 'pending', never
        # re-dispatched. Other alert types keep the existing bounded-retry
        # policy (max_attempts, backoff) on the same row.
        if dedupe_key and dedupe_key.startswith("daily_review:"):
            repo.mark_alert_failed(
                alert_id,
                f"daily_review_send_outcome_unknown_no_retry: {exc}",
                max_attempts=1,
                force_terminal=True,
            )
            return {
                "ok": True, "sent": False, "failed": True, "alert_id": alert_id,
                "error": f"daily_review_send_outcome_unknown_no_retry: {exc}",
            }
        max_attempts = int((load_config().trading_mode.get("alerts") or {}).get("retry_max_attempts", 3))
        repo.mark_alert_failed(alert_id, str(exc), max_attempts=max_attempts)
        return {"ok": True, "sent": False, "failed": True, "alert_id": alert_id, "error": str(exc)}
    if not sent:
        # 08-12 (reviewer round 3 Recommended-3): a FALSY return is the
        # provider EXPLICITLY refusing the message — it was never accepted, so
        # a retry cannot duplicate (only the EXCEPTION path above is
        # outcome-unknown). A daily_review falsy return keeps the bounded
        # retry policy on the SAME row (once-ever index untouched); it must
        # NOT terminate with the outcome-unknown reason code, which would
        # silently miss the daily push forever for a recoverable condition.
        max_attempts = int((load_config().trading_mode.get("alerts") or {}).get("retry_max_attempts", 3))
        repo.mark_alert_failed(alert_id, "send_message returned falsy", max_attempts=max_attempts)
        return {"ok": True, "sent": False, "failed": True, "alert_id": alert_id, "error": "send_message returned falsy"}
    # 08-12 P1: the external send SUCCEEDED — a finalize failure must NOT
    # recycle the row to 'pending' (a retry would send AGAIN: the production
    # defect restarted the same daily session >=14 times). For daily_review
    # keys the 'sent' flag and the report's pushed_to_feishu marker commit
    # together (single short transaction); any finalize exception fails closed
    # to terminal 'failed' + alert_failure_log, never a second external send.
    daily_review_date = (
        dedupe_key.split(":", 1)[1]
        if dedupe_key and dedupe_key.startswith("daily_review:")
        else None
    )
    try:
        repo.mark_alert_sent(alert_id, daily_review_date=daily_review_date)
    except Exception as exc:
        repo.mark_alert_failed(
            alert_id,
            f"finalize_failed_after_send: {exc}",
            max_attempts=1,
            force_terminal=True,
        )
        return {
            "ok": True, "sent": False, "failed": True, "alert_id": alert_id,
            "error": f"finalize_failed_after_send: {exc}",
        }
    return {"ok": True, "sent": True, "alert_id": alert_id}
