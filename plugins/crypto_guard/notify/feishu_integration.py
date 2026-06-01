from __future__ import annotations

import threading
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.notify.intent_parser import is_crypto_intent
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path
from plugins.crypto_guard.storage.sqlite_db import connect_db

LOGGER = get_logger("crypto_guard.feishu")


def enqueue_feishu_message(
    *,
    text: str,
    open_id: str,
    receive_id: str,
    receive_id_type: str,
    message_id: str,
    send_message: Callable[..., Any] | None = None,
) -> bool:
    if not is_crypto_intent(text):
        return False
    LOGGER.info("enqueue_feishu_message message_id=%s open_id=%s receive_id=%s text=%s", message_id, open_id, receive_id, text[:200])
    cfg = load_config()
    initialize_database(cfg)
    use_redis = should_use_redis_for_path(cfg.database_path)
    redis = RedisAdapter() if use_redis else None
    if redis and redis.is_available() and not redis.dedupe_event(message_id):
        LOGGER.info("duplicate feishu message skipped by redis message_id=%s", message_id)
        return True
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        if not repo.claim_feishu_event(message_id, "message", {"text": text, "open_id": open_id, "receive_id": receive_id}):
            LOGGER.info("duplicate feishu message skipped message_id=%s", message_id)
            return True
        payload = {"text": text, "open_id": open_id, "receive_id": receive_id, "receive_id_type": receive_id_type, "message_id": message_id}
        redis_job_id = redis.enqueue_user_job({"job_type": "feishu_user_message", "priority": 1, "source": "feishu", "session_id": f"feishu:user:{open_id}", "payload": payload}) if redis else None
        if redis_job_id:
            LOGGER.info("enqueued redis feishu_user_message redis_job_id=%s message_id=%s", redis_job_id, message_id)
        else:
            repo.enqueue_job("feishu_user_message", 1, "feishu", f"feishu:user:{open_id}", payload)
            LOGGER.info("enqueued sqlite feishu_user_message message_id=%s session=%s", message_id, f"feishu:user:{open_id}")
    finally:
        conn.close()
    if send_message:
        send_message(receive_id, "已收到，正在进入 CryptoGuard 高优先级队列处理。", receive_id_type=receive_id_type)
        if not _service_started():
            _start_inline_user_worker(send_message)
    return True


def enqueue_button_callback(payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    LOGGER.info("enqueue_button_callback action=%s symbol=%s signal_id=%s", payload.get("action"), payload.get("symbol"), payload.get("signal_id"))
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        event_id = str(payload.get("event_id") or f"button:{payload.get('open_id')}:{payload.get('action')}:{payload.get('signal_id')}")
        if not repo.claim_feishu_event(event_id, "button_callback", payload):
            LOGGER.info("duplicate feishu button skipped event_id=%s", event_id)
            return {"ok": True, "duplicate": True}
        job_id = repo.enqueue_job("feishu_button_callback", 2, "feishu", f"feishu:button:{payload.get('open_id', 'unknown')}", payload)
    finally:
        conn.close()
    if send_message:
        if not _service_started():
            _start_inline_user_worker(send_message)
    return {"ok": True, "job_id": job_id}


def _service_started() -> bool:
    try:
        from plugins.crypto_guard.service_manager import is_started

        return is_started()
    except Exception:
        return False


def _start_inline_user_worker(send_message: Callable[..., Any]) -> None:
    def _run() -> None:
        try:
            from plugins.crypto_guard.run_ga_workers import run_once

            run_once(user_only=True, send_message=send_message)
        except Exception as exc:
            print(f"[crypto_guard] inline user worker failed: {exc}")

    threading.Thread(target=_run, daemon=True).start()
