from __future__ import annotations

import json
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
    use_redis = should_use_redis_for_path(load_config().database_path)
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
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=symbol,
        priority=priority,
        payload=payload,
        dedupe_key=dedupe_key or f"{symbol or '-'}:{alert_type}",
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
        payload = json.loads(row["payload_json"])
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
