from __future__ import annotations

from pathlib import Path
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import log_path
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter


def crypto_system_status() -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    # PG cutover: pooled connection (auto-returned; the queries below are
    # read-only, so no commit is needed).
    with pg_db.get_conn() as conn:
        queues = {
            "pending_user": _count(conn, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority <= 2"),
            "pending_background": _count(conn, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority > 2"),
            "running": _count(conn, "SELECT COUNT(*) FROM agent_jobs WHERE status='running'"),
            "failed_24h": _count(conn, "SELECT COUNT(*) FROM agent_jobs WHERE status='failed' AND finished_at >= NOW() - INTERVAL '1 day'"),
        }
        scheduler = [
            dict(r)
            for r in conn.execute(
                """
                SELECT job_name, scheduled_time, status, started_at, finished_at, error_message
                FROM scheduler_runs
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
        ]
        locks = [
            dict(r)
            for r in conn.execute(
                "SELECT lock_name, owner, locked_until FROM task_locks ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()
        ]
        symbols = {
            "enabled": _count(conn, "SELECT COUNT(*) FROM symbols WHERE enabled=TRUE"),
            "disabled": _count(conn, "SELECT COUNT(*) FROM symbols WHERE enabled=FALSE"),
        }
        paper = {
            "pending": _count(conn, "SELECT COUNT(*) FROM paper_orders WHERE status='pending'"),
            "open": _count(conn, "SELECT COUNT(*) FROM paper_orders WHERE status='open'"),
            "closed": _count(conn, "SELECT COUNT(*) FROM paper_orders WHERE status='closed'"),
        }
        reviews = {
            "total": _count(conn, "SELECT COUNT(*) FROM trade_reviews"),
            "unreviewed_closed": _count(conn, "SELECT COUNT(*) FROM paper_trades t LEFT JOIN trade_reviews r ON r.trade_id=t.id WHERE t.closed_at IS NOT NULL AND r.id IS NULL"),
            "candidate_patches": _count(conn, "SELECT COUNT(*) FROM strategy_patches WHERE status='candidate'"),
        }
        from plugins.crypto_guard.storage.repository import CryptoGuardRepository

        repo = CryptoGuardRepository(conn)
        parquet_run = repo.latest_parquet_archive_run()
        redis_status = RedisAdapter().health_check()
        duckdb_status = DuckDBAnalytics().health_check()
        log_file = log_path()
        return {
            "ok": True,
            "service_started": _service_started(),
            "database": pg_db.database_identity(),
            "postgres": {"status": "ok", "identity": pg_db.database_identity()},
            "redis": redis_status,
            "parquet": {"status": "ok" if parquet_run and parquet_run.get("status") == "success" else "degraded", "last_write": (parquet_run or {}).get("created_at"), "path": (parquet_run or {}).get("path")},
            "duckdb": duckdb_status,
            "log_path": str(log_file),
            "log_exists": Path(log_file).exists(),
            "queues": queues,
            "symbols": symbols,
            "paper": paper,
            "reviews": reviews,
            "recent_scheduler_runs": scheduler,
            "locks": locks,
        }


def render_system_status_text(status: dict[str, Any]) -> str:
    if not status.get("ok"):
        return f"CryptoGuard 系统状态获取失败：{status.get('error', 'unknown')}"
    lines = [
        "**CryptoGuard 系统状态**",
        "",
        f"服务：{'已启动' if status.get('service_started') else '未检测到自动服务线程'}",
        f"数据库：{status.get('database')}",
        f"日志：{status.get('log_path')}",
        f"Redis：{(status.get('redis') or {}).get('status', '-')}",
        f"Parquet：{(status.get('parquet') or {}).get('status', '-')} last_write={(status.get('parquet') or {}).get('last_write') or '-'}",
        f"DuckDB：{(status.get('duckdb') or {}).get('status', '-')}",
        "",
        "**队列：**",
        f"- 用户待处理：{status['queues']['pending_user']}",
        f"- 后台待处理：{status['queues']['pending_background']}",
        f"- 运行中：{status['queues']['running']}",
        f"- 24h 失败：{status['queues']['failed_24h']}",
        "",
        "**产品池：**",
        f"- 启用：{status['symbols']['enabled']}",
        f"- 暂停：{status['symbols']['disabled']}",
        "",
        "**模拟盘：**",
        f"- pending：{status['paper']['pending']}",
        f"- open：{status['paper']['open']}",
        f"- closed：{status['paper']['closed']}",
        "",
        "**复盘：**",
        f"- 总复盘数：{status['reviews']['total']}",
        f"- 未复盘已平仓：{status['reviews']['unreviewed_closed']}",
        f"- candidate 补丁：{status['reviews']['candidate_patches']}",
        "",
        "**最近定时任务：**",
    ]
    runs = status.get("recent_scheduler_runs") or []
    if not runs:
        lines.append("- 暂无 scheduler_runs 记录")
    else:
        for run in runs[:8]:
            err = f"，错误：{run['error_message']}" if run.get("error_message") else ""
            lines.append(f"- {run['job_name']}：{run['status']}，scheduled_time={run['scheduled_time']}{err}")
    locks = status.get("locks") or []
    if locks:
        lines.append("")
        lines.append("**当前锁：**")
        for lock in locks[:5]:
            lines.append(f"- {lock['lock_name']} until {lock['locked_until']}")
    return "\n".join(lines)


def crypto_list_recent_errors(limit: int = 20) -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    # PG cutover: pooled connection (auto-returned; list_recent_errors is
    # read-only, so no commit is needed).
    with pg_db.get_conn() as conn:
        from plugins.crypto_guard.storage.repository import CryptoGuardRepository

        repo = CryptoGuardRepository(conn)
        errors = repo.list_recent_errors(limit=limit)
        return {"ok": True, "errors": errors, "count": len(errors), "text": render_recent_errors_text(errors)}


def render_recent_errors_text(errors: list[dict[str, Any]]) -> str:
    lines = ["**CryptoGuard 最近错误**", ""]
    if not errors:
        lines.append("- 最近没有失败任务或错误记录。")
        return "\n".join(lines)
    for err in errors:
        msg = (err.get("error_message") or "").replace("\n", " ")
        if len(msg) > 240:
            msg = msg[:240] + "..."
        lines.append(f"- **{err.get('source')} #{err.get('id')}** `{err.get('name')}` @ {err.get('ts') or '-'}")
        lines.append(f"  - session/scheduled：`{err.get('session_id')}`")
        lines.append(f"  - error：{msg or '-'}")
    return "\n".join(lines)


def _count(conn: Any, sql: str) -> int:
    # PG cutover: psycopg dict_row factory returns a dict; ``COUNT(*)`` is
    # exposed as the lowercase ``"count"`` key (not a positional tuple).
    return int(conn.execute(sql).fetchone()["count"])


def _service_started() -> bool:
    try:
        from plugins.crypto_guard.service_manager import is_started

        return is_started()
    except Exception:
        return False
