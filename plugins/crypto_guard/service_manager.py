from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger, log_path
from plugins.crypto_guard.run_ga_workers import run_once
from plugins.crypto_guard.run_scheduler import run_job
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db


_START_LOCK = threading.Lock()
_STARTED = False
_THREADS: list[threading.Thread] = []

# P1-2 R4: Warmup readiness gate — explicit 3-state machine.
#
# The old binary ``threading.Event`` was fail-open: it was set to True on
# BOTH success and failure, so a failed/degraded warmup let analysis
# proceed on bad data. The state machine fixes this:
#
#   "pending"  — warmup started but not yet finished (analysis deferred)
#   "ready"    — warmup succeeded AND data is not degraded (analysis allowed)
#   "failed"   — warmup raised, returned degraded=True, or timed out
#                (analysis deferred; next periodic warmup can recover to "ready")
#
# Only ``state == "ready"`` opens the gate. Exceptions, degraded results,
# and timeouts all transition to "failed" — the gate stays closed and
# ``enqueue_market_analysis`` returns a deferred result.
#
# Default is "ready" so tests/CLI that never call ``start_all_services``
# proceed normally. The readiness gate respects this default. In tests we
# don't run the warmup thread, so the default must allow analysis.
_WARMUP_STATE = "ready"  # one of "pending", "ready", "failed"
_WARMUP_LOCK = threading.Lock()
_WARMUP_STARTED_AT: float | None = None
_WARMUP_TIMEOUT_SECONDS = 600.0  # 10 minutes
_WARMUP_FAILURE_REASON: str = ""

LOGGER = get_logger("crypto_guard.service")


def _set_warmup_started() -> None:
    """Called by ``start_all_services`` to mark the warmup race window open."""
    global _WARMUP_STARTED_AT, _WARMUP_FAILURE_REASON
    with _WARMUP_LOCK:
        _WARMUP_STATE_PENDING()  # transition to pending
        _WARMUP_STARTED_AT = time.time()
        _WARMUP_FAILURE_REASON = ""


def _WARMUP_STATE_PENDING() -> None:
    """Internal helper to set state=pending (assumes lock held)."""
    global _WARMUP_STATE
    _WARMUP_STATE = "pending"


def _set_warmup_ready() -> None:
    """Called when warmup succeeded AND data is not degraded."""
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON, _WARMUP_STARTED_AT
    with _WARMUP_LOCK:
        _WARMUP_STATE = "ready"
        _WARMUP_FAILURE_REASON = ""
        _WARMUP_STARTED_AT = None  # clear so timeout check doesn't fire


def _set_warmup_failed(reason: str) -> None:
    """Called on exception, degraded result, or timeout."""
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON, _WARMUP_STARTED_AT
    with _WARMUP_LOCK:
        _WARMUP_STATE = "failed"
        _WARMUP_FAILURE_REASON = str(reason)
        _WARMUP_STARTED_AT = None  # clear so timeout check doesn't fire


def is_warmup_complete() -> bool:
    """Check whether the warmup gate is open.

    Returns ``True`` ONLY when ``_WARMUP_STATE == "ready"``. The timeout
    fallback transitions to "failed" (not "ready") so analysis stays
    deferred until a subsequent periodic warmup succeeds.
    """
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON
    # Check timeout first: if warmup has been pending longer than the
    # timeout, transition to failed (fail-closed, not fail-open).
    if _WARMUP_STARTED_AT is not None:
        elapsed = time.time() - _WARMUP_STARTED_AT
        if elapsed >= _WARMUP_TIMEOUT_SECONDS:
            with _WARMUP_LOCK:
                if _WARMUP_STATE == "pending":
                    _WARMUP_STATE = "failed"
                    _WARMUP_FAILURE_REASON = "timeout"
                    LOGGER.warning(
                        "warmup timeout (%.0fs elapsed >= %.0fs) — gate "
                        "transitions to failed; analysis stays deferred",
                        elapsed, _WARMUP_TIMEOUT_SECONDS,
                    )
    with _WARMUP_LOCK:
        return _WARMUP_STATE == "ready"


def get_warmup_state() -> str:
    """Return the current warmup state string for diagnostics."""
    with _WARMUP_LOCK:
        return _WARMUP_STATE


def get_warmup_failure_reason() -> str:
    """Return the failure reason (empty string if not failed)."""
    with _WARMUP_LOCK:
        return _WARMUP_FAILURE_REASON


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

        # R5: Run market-data warmup once at startup before the scheduler loop.
        # This backfills any gaps from downtime so the first analysis tick has
        # healthy data. P1-9: runs in a background thread so it doesn't block
        # startup or delay the scheduler/worker threads from starting. The
        # scheduler loop will retry on the next tick if the warmup fails.
        # P1-2 R4: Transition to "pending" so enqueue_market_analysis defers
        # analysis until warmup finishes. The warmup thread transitions to
        # "ready" (success, no degradation) or "failed" (exception/degraded).
        # The periodic market_data_warmup cron job (every 5 min) can recover
        # from "failed" to "ready" on a subsequent successful run.
        _set_warmup_started()

        def _warmup_bg(_=None) -> None:
            # P1-2 R4: on entry, state is "pending" (set by _set_warmup_started).
            # market_data_warmup() now handles the state transitions internally:
            #   - success (no degradation) → _set_warmup_ready()
            #   - degraded → _set_warmup_failed("degraded")
            #   - exception → _set_warmup_failed(str(exc))
            # The finally block below is a safety net: if market_data_warmup
            # returns without setting state (shouldn't happen, but defensive),
            # transition to "failed" so the gate doesn't stay in "pending".
            try:
                from plugins.crypto_guard.scheduler.cron_scheduler import market_data_warmup
                warmup_result = market_data_warmup()
                if warmup_result.get("degraded"):
                    LOGGER.warning(
                        "CryptoGuard startup market_data_warmup: degraded=%s symbols=%s",
                        warmup_result.get("degraded"),
                        list(warmup_result.get("symbols", {}).keys()),
                    )
                else:
                    LOGGER.info("CryptoGuard startup market_data_warmup: all TFs ready")
            except Exception as exc:
                LOGGER.exception("CryptoGuard startup market_data_warmup failed; scheduler will retry on next tick")
                _set_warmup_failed(str(exc))
            finally:
                # P1-2 R4: if state is still "pending" (e.g. market_data_warmup
                # returned without transitioning state, or an early return path
                # didn't set it), transition to "failed" so the gate doesn't
                # stay closed forever. The next periodic warmup job can
                # recover to "ready".
                if get_warmup_state() == "pending":
                    _set_warmup_failed("incomplete")

        _spawn("crypto_guard_warmup", _warmup_bg, None)

        _spawn("crypto_guard_user_worker", _user_worker_loop, send_message)
        _spawn("crypto_guard_background_worker", _background_worker_loop, send_message)
        _spawn("crypto_guard_scheduler", _scheduler_loop, None)

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


def _due_scheduler_jobs(now: datetime) -> list[str]:
    jobs: list[str] = []
    minute = now.minute
    hour = now.hour
    # P0 (R4): analyze_market_15m must be dispatched before hourly_feishu_report
    # so the analysis batch exists when the report checks for it.
    if minute in {1, 16, 31, 46}:
        jobs.append("fetch_15m_klines")
    if minute % 5 == 1:
        jobs.append("fetch_5m_klines")
    if minute in {1, 16, 31, 46}:
        jobs.append("analyze_market_15m")
    if minute in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}:
        jobs.append("hourly_feishu_report")
    jobs.append("alert_outbox_retry")
    if minute == 1:
        jobs.append("fetch_1h_klines")
        if hour in {0, 4, 8, 12, 16, 20}:
            jobs.append("fetch_4h_klines")
        if hour == 0:
            jobs.append("fetch_1d_klines")
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
    # Position conflict revalidation: every 10 minutes at minute % 10 == 5
    if minute % 10 == 5:
        jobs.append("position_conflict_revalidation")
    # Shadow virtual trade update: every minute
    jobs.append("shadow_virtual_trade_update")
    # R5: market-data warmup — runs every 5 min before analysis to backfill gaps
    if minute % 5 == 0:
        jobs.append("market_data_warmup")
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
    if job_name == "position_conflict_revalidation":
        return int(now.timestamp()) // (10 * 60)
    if job_name == "shadow_virtual_trade_update":
        return int(now.timestamp()) // 60
    if job_name == "market_data_warmup":
        return int(now.timestamp()) // (5 * 60)
    return int(now.timestamp()) // 86400
