"""PostgreSQL database cleanup and maintenance.

CryptoGuard runs on PostgreSQL only (no SQLite fallback). This module replaces
the legacy SQLite cleanup CLI: it deletes aged operational rows by retention
policy and runs ``VACUUM (ANALYZE)`` to reclaim space.

Usage:
    python -m plugins.crypto_guard.storage.cleanup --vacuum
    python -m plugins.crypto_guard.storage.cleanup --clean-old 30
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

import psycopg

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage import pg_db

LOGGER = get_logger("crypto_guard.storage.cleanup")

# Table -> (timestamp_column, retention_days, column_kind)
# column_kind:
#   "timestamptz" - column is TIMESTAMPTZ; compare against NOW() - interval
#   "epoch_ms"    - column is BIGINT epoch-milliseconds; compare against ms cutoff
# None retention means "never auto-delete".
RETENTION_POLICY: dict[str, tuple[str, int | None, str]] = {
    "candles": ("open_time", 7, "epoch_ms"),                # 7 days, historical in Parquet
    "module_analysis_results": ("created_at", 30, "timestamptz"),  # 30 days
    "skill_execution_logs": ("created_at", 30, "timestamptz"),     # 30 days
    "market_snapshots": ("created_at", 30, "timestamptz"),          # 30 days
    "scheduler_runs": ("started_at", 30, "timestamptz"),            # 30 days
    "agent_jobs": ("created_at", 30, "timestamptz"),                # 30 days
    "paper_equity_snapshots": ("ts", 30, "epoch_ms"),               # 30 days
    "analysis_states": ("created_at", 30, "timestamptz"),           # 30 days
    "alert_outbox": ("created_at", 14, "timestamptz"),              # 14 days
    # Keep indefinitely: ga_decisions, skill_feedback_memory, strategy_*, symbols, parquet_archive_runs
}


def clean_old_data(retention_days: int | None = None) -> dict[str, int]:
    """Delete rows older than retention period from operational tables.

    Args:
        retention_days: Override retention for all tables. If None, uses per-table policy.
    """
    deleted: dict[str, int] = {}
    with pg_db.get_conn() as conn:
        for table, (col, default_days, kind) in RETENTION_POLICY.items():
            days = retention_days if retention_days is not None else default_days
            if days is None:
                continue

            # Check table and column exist (best-effort: skip silently if absent)
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema=current_schema() "
                            "AND table_name=%s AND column_name=%s",
                            (table, col),
                        )
                        if cur.fetchone() is None:
                            continue
            except Exception:
                continue

            if kind == "epoch_ms":
                cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
                sql = f'DELETE FROM "{table}" WHERE "{col}" IS NOT NULL AND "{col}" < %s'
                params: tuple[Any, ...] = (cutoff_ms,)
            else:
                # TIMESTAMPTZ column: compare against NOW() - interval (server-side)
                sql = (
                    f'DELETE FROM "{table}" WHERE "{col}" IS NOT NULL '
                    f'AND "{col}" < (NOW() - (%s || \' days\')::interval)'
                )
                params = (str(int(days)),)

            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        count = cur.rowcount
            except Exception as exc:  # noqa: BLE001 - best-effort per-table
                LOGGER.warning("cleanup failed for %s.%s: %s", table, col, exc)
                continue
            if count and count > 0:
                deleted[table] = count
                LOGGER.info("Deleted %d rows from %s (older than %d days)", count, table, days)
    return deleted


def vacuum_database() -> dict[str, Any]:
    """Run VACUUM ANALYZE to reclaim space and refresh planner stats.

    PostgreSQL ``VACUUM`` cannot run inside a transaction block, so it opens a
    dedicated autocommit connection from the pool (not the transactional
    ``get_conn`` path). Returns an ok marker - PG does not expose a reliable
    size delta per-VACUUM the way SQLite's file did, so no before/after MB.
    """
    pool = pg_db.get_pool()
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM (ANALYZE)")
    LOGGER.info("VACUUM (ANALYZE) complete on crypto_guard")
    return {"ok": True, "engine": "postgresql"}


def get_table_stats() -> dict[str, Any]:
    """Get row counts for all CryptoGuard tables."""
    stats: dict[str, Any] = {}
    with pg_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            tables = [r["table_name"] for r in cur.fetchall()]
            total_rows = 0
            for t in tables:
                try:
                    with conn.cursor() as c2:
                        c2.execute(f'SELECT COUNT(*) AS n FROM "{t}"')
                        cnt = int(c2.fetchone()["n"])
                    stats[t] = {"rows": cnt}
                    total_rows += cnt
                except Exception:
                    stats[t] = {"rows": -1}
            stats["_total_rows"] = total_rows
        conn.rollback()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL cleanup and maintenance")
    parser.add_argument("--clean-old", type=int, metavar="DAYS", help="Delete data older than N days")
    parser.add_argument("--vacuum", action="store_true", help="Run VACUUM ANALYZE to reclaim space")
    parser.add_argument("--stats", action="store_true", help="Show table statistics")
    parser.add_argument("--full", action="store_true", help="Clean old data + VACUUM + stats")
    args = parser.parse_args()

    if args.stats or args.full:
        stats = get_table_stats()
        total_rows = stats.pop("_total_rows")
        print(f"Engine: PostgreSQL (crypto_guard), Total rows: {total_rows:,}")
        print(f"{'Table':<30} {'Rows':>10}")
        print("-" * 42)
        for t, info in sorted(stats.items()):
            if info["rows"] > 0:
                print(f"  {t:<28} {info['rows']:>10,}")

    if args.clean_old is not None or args.full:
        days = args.clean_old or 30
        print(f"\nCleaning data older than {days} days...")
        deleted = clean_old_data(days)
        if deleted:
            total = sum(deleted.values())
            print(f"Deleted {total:,} rows total:")
            for t, cnt in sorted(deleted.items()):
                print(f"  {t}: {cnt:,}")
        else:
            print("No rows to delete.")

    if args.vacuum or args.full:
        print("\nRunning VACUUM ANALYZE...")
        result = vacuum_database()
        if result.get("ok"):
            print("  VACUUM (ANALYZE) complete.")
        else:
            print(f"  VACUUM failed: {result}")


if __name__ == "__main__":
    main()
