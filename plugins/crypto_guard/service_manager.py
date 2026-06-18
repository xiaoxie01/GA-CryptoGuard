from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger, log_path
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.run_ga_workers import run_once
from plugins.crypto_guard.run_scheduler import run_job
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db


_START_LOCK = threading.Lock()
_STARTED = False
_THREADS: list[threading.Thread] = []
LOGGER = get_logger("crypto_guard.service")


def start_all_services(*, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """随飞书入口启动 CryptoGuard 后台服务。

    这些线程都只做轻量轮询和任务入队/消费；飞书 event handler 仍然快速 ACK，
    用户消息通过 priority=1 的 agent_jobs 优先处理。
    """

    global _STARTED
    if os.environ.get("CRYPTO_GUARD_AUTOSTART", "1").lower() in {"0", "false", "no"}:
        return {"ok": True, "started": False, "reason": "CRYPTO_GUARD_AUTOSTART disabled"}

    with _START_LOCK:
        if _STARTED:
            return {"ok": True, "started": False, "reason": "already_started", "threads": [t.name for t in _THREADS]}

        cfg = load_config()
        init_result = initialize_database(cfg)
        LOGGER.info("CryptoGuard autostart initializing database path=%s log=%s", cfg.database_path, log_path())
        conn = connect_db(cfg.database_path)
        try:
            recovered = CryptoGuardRepository(conn).recover_stale_running_jobs(older_than_minutes=30)
            if recovered:
                LOGGER.warning("Recovered stale running agent_jobs count=%s", recovered)
        finally:
            conn.close()

        _spawn("crypto_guard_user_worker", _user_worker_loop, send_message)
        _spawn("crypto_guard_background_worker", _background_worker_loop, send_message)
        _spawn("crypto_guard_scheduler", _scheduler_loop, None)
        _spawn("crypto_guard_paper_worker", _paper_loop, None)

        _STARTED = True
        LOGGER.info("CryptoGuard services started threads=%s", [t.name for t in _THREADS])
        return {"ok": True, "started": True, "init": init_result, "threads": [t.name for t in _THREADS]}


def is_started() -> bool:
    return _STARTED


def _spawn(name: str, target: Callable[..., None], arg: Any) -> None:
    thread = threading.Thread(target=target, args=(arg,), name=name, daemon=True)
    thread.start()
    _THREADS.append(thread)
    LOGGER.info("Started background thread name=%s", name)


def _user_worker_loop(send_message: Callable[..., Any] | None) -> None:
    while True:
        try:
            result = run_once(user_only=True, send_message=send_message)
            if result.get("processed"):
                LOGGER.info("user_worker processed job_id=%s result_ok=%s", result.get("job_id"), (result.get("result") or {}).get("ok"))
        except Exception:
            LOGGER.exception("user_worker loop failed")
            traceback.print_exc()
        time.sleep(0.5)


def _background_worker_loop(send_message: Callable[..., Any] | None) -> None:
    while True:
        try:
            result = run_once(background=True, send_message=send_message)
            if result.get("processed"):
                LOGGER.info("background_worker processed job_id=%s result_ok=%s", result.get("job_id"), (result.get("result") or {}).get("ok"))
        except Exception:
            LOGGER.exception("background_worker loop failed")
            traceback.print_exc()
        time.sleep(1.5)


def _scheduler_loop(_: Any = None) -> None:
    last_tick: dict[str, int] = {}
    while True:
        try:
            now = datetime.now(timezone.utc)
            due_jobs = _due_scheduler_jobs(now)
            for job_name in due_jobs:
                tick_key = _tick_key(job_name, now)
                if last_tick.get(job_name) == tick_key:
                    continue
                last_tick[job_name] = tick_key
                try:
                    LOGGER.info("scheduler running job=%s tick=%s", job_name, tick_key)
                    run_job(job_name)
                    LOGGER.info("scheduler finished job=%s tick=%s", job_name, tick_key)
                except Exception:
                    LOGGER.exception("scheduler job failed job=%s tick=%s", job_name, tick_key)
                    traceback.print_exc()
        except Exception:
            LOGGER.exception("scheduler loop failed")
            traceback.print_exc()
        time.sleep(20)


def _paper_loop(_: Any = None) -> None:
    while True:
        try:
            cfg = load_config()
            initialize_database(cfg)
            conn = connect_db(cfg.database_path)
            try:
                result = update_paper_positions(CryptoGuardRepository(conn))
                if result.get("results"):
                    LOGGER.info("paper_worker update results=%s", result.get("results"))
            finally:
                conn.close()
        except Exception:
            LOGGER.exception("paper_worker loop failed")
            traceback.print_exc()
        time.sleep(180)


def _due_scheduler_jobs(now: datetime) -> list[str]:
    jobs: list[str] = []
    minute = now.minute
    hour = now.hour
    if minute in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}:
        jobs.append("hourly_feishu_report")
    jobs.append("alert_outbox_retry")
    if minute == 1:
        jobs.append("fetch_1h_klines")
        if hour in {0, 4, 8, 12, 16, 20}:
            jobs.append("fetch_4h_klines")
        if hour == 0:
            jobs.append("fetch_1d_klines")
    if minute in {1, 16, 31, 46}:
        jobs.append("fetch_15m_klines")
    if minute % 5 == 1:
        jobs.append("fetch_5m_klines")
    if minute in {1, 16, 31, 46}:
        jobs.append("analyze_market_15m")
    if minute in {3, 18, 33, 48}:
        jobs.append("update_opportunity_watches")
    if minute % 3 == 0:
        jobs.append("update_paper_positions_3m")
    # Pending order lifecycle: TTL expiry + conflict cancellation (every 60 minutes)
    if minute == 0:
        jobs.append("pending_order_management")
    # Pending order revalidation: multi-dimensional review (every 60 minutes, offset by 15)
    if minute == 15:
        jobs.append("pending_order_revalidation")
    # Daily review: run between 00:05-00:30 UTC (wider window for crash recovery)
    # _tick_key ensures it only runs once per day
    if hour == 0 and 5 <= minute <= 30:
        jobs.append("daily_review")
    return jobs


def _tick_key(job_name: str, now: datetime) -> int:
    if job_name == "analyze_market_15m":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "update_opportunity_watches":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "fetch_15m_klines":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "fetch_5m_klines":
        return int(now.timestamp()) // (5 * 60)
    if job_name == "fetch_1h_klines":
        return int(now.timestamp()) // 3600
    if job_name == "hourly_feishu_report":
        return int(now.timestamp()) // 3600
    if job_name == "alert_outbox_retry":
        return int(now.timestamp()) // 60
    if job_name == "fetch_4h_klines":
        return int(now.timestamp()) // (4 * 3600)
    if job_name == "update_paper_positions_3m":
        return int(now.timestamp()) // (3 * 60)
    return int(now.timestamp()) // 86400
