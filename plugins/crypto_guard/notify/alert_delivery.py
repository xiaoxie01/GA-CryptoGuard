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
    "opportunity_triggered",
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
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=symbol,
        priority=priority,
        payload=payload,
        dedupe_key=dedupe_key or default_dedupe_key,
    )
    if not send_message:
        return {"ok": True, "sent": False, "queued": True, "alert_id": alert_id}
    return _deliver_alert(repo, alert_id, payload, send_message)


def process_alert_outbox(repo: CryptoGuardRepository, send_message: Callable[..., Any] | None, *, limit: int = 10) -> dict[str, Any]:
    if not send_message:
        return {"ok": True, "processed": 0, "sent": 0, "failed": 0}
    processed = sent = failed = 0
    for row in repo.claim_pending_alerts(limit=limit):
        processed += 1
        # 07-16 cutover: ``payload_json`` is a JSONB column, so psycopg3 returns
        # it as an already-decoded dict (NOT str). ``json.loads(dict)`` raises
        # TypeError -> ``_deliver_alert`` never runs -> every alert silently
        # fails to send. Pass dict/list through; only parse str.
        raw_payload = row["payload_json"]
        payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload or "{}")
        result = _deliver_alert(repo, int(row["id"]), payload, send_message)
        if result.get("sent"):
            sent += 1
        elif result.get("failed"):
            failed += 1
    return {"ok": True, "processed": processed, "sent": sent, "failed": failed}


def _deliver_alert(repo: CryptoGuardRepository, alert_id: int, payload: dict[str, Any], send_message: Callable[..., Any]) -> dict[str, Any]:
    try:
        sent = send_message(
            payload["receive_id"],
            payload["content"],
            msg_type=payload.get("msg_type", "interactive"),
            receive_id_type=payload.get("receive_id_type", "chat_id"),
        )
        if sent:
            repo.mark_alert_sent(alert_id)
            return {"ok": True, "sent": True, "alert_id": alert_id}
        raise RuntimeError("send_message returned falsy")
    except Exception as exc:
        max_attempts = int((load_config().trading_mode.get("alerts") or {}).get("retry_max_attempts", 3))
        repo.mark_alert_failed(alert_id, str(exc), max_attempts=max_attempts)
        return {"ok": True, "sent": False, "failed": True, "alert_id": alert_id, "error": str(exc)}
