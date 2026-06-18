from __future__ import annotations

import argparse
import json

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.scheduler.cron_scheduler import enqueue_15m_analysis, enqueue_market_analysis, fetch_closed_klines_for_active_symbols
from plugins.crypto_guard.scheduler.job_runner import run_scheduled_job
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms

LOGGER = get_logger("crypto_guard.scheduler")


def run_job(job_name: str) -> dict:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        now = utc_ms()
        LOGGER.info("run_job start job=%s now_ms=%s", job_name, now)
        if job_name == "fetch_1d_klines":
            scheduled_time = latest_closed_close_time_ms("1d", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=fetch_closed_klines_for_active_symbols, interval="1d", lookback=3, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "fetch_4h_klines":
            scheduled_time = latest_closed_close_time_ms("4h", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=fetch_closed_klines_for_active_symbols, interval="4h", lookback=6, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "fetch_1h_klines":
            scheduled_time = latest_closed_close_time_ms("1h", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=fetch_closed_klines_for_active_symbols, interval="1h", lookback=12, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "fetch_15m_klines":
            scheduled_time = latest_closed_close_time_ms("15m", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=fetch_closed_klines_for_active_symbols, interval="15m", lookback=12, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "fetch_5m_klines":
            scheduled_time = latest_closed_close_time_ms("5m", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=fetch_closed_klines_for_active_symbols, interval="5m", lookback=24, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "analyze_market_15m":
            scheduled_time = latest_closed_close_time_ms("15m", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=enqueue_15m_analysis, analysis_time_utc=now)
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "update_opportunity_watches":
            scheduled_time = latest_closed_close_time_ms("15m", now)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "update_opportunity_watches",
                        5,
                        "scheduler",
                        f"system:scheduled:opportunity_watches:{scheduled_time}",
                        {"analysis_time_utc": scheduled_time},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "daily_review":
            from datetime import datetime, timezone, timedelta
            yesterday_utc = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            scheduled_time = latest_closed_close_time_ms("1d", now)
            result = run_scheduled_job(repo, job_name=job_name, scheduled_time=scheduled_time, task_fn=lambda: {"ok": True, "queued": repo.enqueue_job_once("daily_review", 7, "scheduler", f"system:scheduled:daily:{yesterday_utc}", {"day_utc": yesterday_utc})})
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "hourly_feishu_report":
            scheduled_time = latest_closed_close_time_ms("1h", now)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "hourly_feishu_report",
                        3,
                        "scheduler",
                        f"system:scheduled:hourly_report:{scheduled_time}",
                        {"scheduled_time": scheduled_time},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "alert_outbox_retry":
            scheduled_time = (now // (60 * 1000)) * (60 * 1000)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "alert_outbox_retry",
                        2,
                        "scheduler",
                        f"system:scheduled:alert_outbox_retry:{scheduled_time}",
                        {"scheduled_time": scheduled_time, "limit": 10},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "update_paper_positions_3m":
            scheduled_time = (now // (3 * 60 * 1000)) * (3 * 60 * 1000)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "update_paper_positions",
                        5,
                        "scheduler",
                        f"system:scheduled:paper_positions:{scheduled_time}",
                        {"scheduled_time": scheduled_time},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "pending_order_management":
            scheduled_time = (now // (60 * 1000)) * (60 * 1000)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "pending_order_management",
                        5,
                        "scheduler",
                        f"system:scheduled:pending_order_mgmt:{scheduled_time}",
                        {"scheduled_time": scheduled_time},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        if job_name == "pending_order_revalidation":
            scheduled_time = (now // (60 * 1000)) * (60 * 1000)
            result = run_scheduled_job(
                repo,
                job_name=job_name,
                scheduled_time=scheduled_time,
                task_fn=lambda: {
                    "ok": True,
                    "queued": repo.enqueue_job_once(
                        "pending_order_revalidation",
                        5,
                        "scheduler",
                        f"system:scheduled:pending_order_reval:{scheduled_time}",
                        {"scheduled_time": scheduled_time},
                    ),
                },
            )
            LOGGER.info("run_job done job=%s result=%s", job_name, result)
            return result
        raise ValueError(f"未知 scheduler job: {job_name}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_name")
    args = parser.parse_args()
    print(json.dumps(run_job(args.job_name), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
