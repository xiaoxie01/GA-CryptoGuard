"""SQLite database cleanup and maintenance.

Usage:
    python -m plugins.crypto_guard.storage.cleanup --vacuum
    python -m plugins.crypto_guard.storage.cleanup --clean-old 30
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone

from plugins.crypto_guard.logging_utils import get_logger

LOGGER = get_logger("crypto_guard.storage.cleanup")

# Table -> (column_with_timestamp, retention_days)
# None retention means "never auto-delete"
RETENTION_POLICY: dict[str, tuple[str | None, int | None]] = {
    "candles": ("open_time", 7),                    # 7 days, historical in Parquet
    "module_analysis_results": ("created_at", 30),   # 30 days
    "skill_execution_logs": ("created_at", 30),      # 30 days
    "market_snapshots": ("created_at", 30),           # 30 days
    "scheduler_runs": ("started_at", 30),             # 30 days
    "agent_jobs": ("created_at", 30),                 # 30 days
    "paper_equity_snapshots": ("snapshot_time", 30),  # 30 days
    "analysis_states": ("updated_at", 30),            # 30 days
    "alert_outbox": ("created_at", 14),               # 14 days
    # Keep indefinitely: ga_decisions, skill_feedback_memory, strategy_*, symbols, parquet_archive_runs
}


def clean_old_data(db_path: str, retention_days: int | None = None) -> dict[str, int]:
    """Delete rows older than retention period from operational tables.

    Args:
        db_path: Path to SQLite database.
        retention_days: Override retention for all tables. If None, uses per-table policy.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    deleted: dict[str, int] = {}

    try:
        for table, (col, default_days) in RETENTION_POLICY.items():
            if col is None:
                continue
            days = retention_days if retention_days is not None else default_days
            if days is None:
                continue

            # Check table and column exist
            try:
                conn.execute(f"SELECT {col} FROM [{table}] LIMIT 1")
            except Exception:
                continue

            cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
            cutoff_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

            # For candles, use millisecond timestamp comparison
            if table == "candles":
                cur = conn.execute(f"DELETE FROM [{table}] WHERE {col} < ?", (cutoff_ms,))
            else:
                # For other tables, try ISO string comparison
                cur = conn.execute(
                    f"DELETE FROM [{table}] WHERE {col} IS NOT NULL AND {col} < ?",
                    (cutoff_iso,),
                )
            count = cur.rowcount
            if count > 0:
                deleted[table] = count
                LOGGER.info("Deleted %d rows from %s (older than %d days)", count, table, days)

        conn.commit()
    finally:
        conn.close()

    return deleted


def vacuum_database(db_path: str) -> dict[str, Any]:
    """Run VACUUM to reclaim space. Returns size before/after."""
    import os

    size_before = os.path.getsize(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        LOGGER.info("VACUUM complete on %s", db_path)
    finally:
        conn.close()
    size_after = os.path.getsize(db_path)

    freed_mb = (size_before - size_after) / (1024 * 1024)
    LOGGER.info("VACUUM freed %.1f MB (%.1f MB -> %.1f MB)", freed_mb, size_before / (1024*1024), size_after / (1024*1024))

    return {
        "ok": True,
        "size_before_mb": round(size_before / (1024 * 1024), 1),
        "size_after_mb": round(size_after / (1024 * 1024), 1),
        "freed_mb": round(freed_mb, 1),
    }


def get_table_stats(db_path: str) -> dict[str, Any]:
    """Get row counts and estimated sizes for all tables."""
    conn = sqlite3.connect(db_path)
    stats: dict[str, Any] = {}
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        total_rows = 0
        for t in tables:
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                stats[t] = {"rows": cnt}
                total_rows += cnt
            except Exception:
                stats[t] = {"rows": -1}
        stats["_total_rows"] = total_rows
        stats["_db_size_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 1)
    finally:
        conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite cleanup and maintenance")
    parser.add_argument("--db", default=None, help="Database path (default: auto-detect)")
    parser.add_argument("--clean-old", type=int, metavar="DAYS", help="Delete data older than N days")
    parser.add_argument("--vacuum", action="store_true", help="Run VACUUM to reclaim space")
    parser.add_argument("--stats", action="store_true", help="Show table statistics")
    parser.add_argument("--full", action="store_true", help="Clean old data + VACUUM + stats")
    args = parser.parse_args()

    db_path = args.db or os.environ.get(
        "CRYPTO_GUARD_DB",
        str(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "crypto_guard", "crypto_guard.sqlite3")),
    )

    if not os.path.exists(db_path):
        # Try alternate path
        alt = os.path.join(os.path.dirname(__file__), "..", "..", "..", "crypto_guard.sqlite3")
        if os.path.exists(alt):
            db_path = alt
        else:
            print(f"Database not found: {db_path}")
            return

    if args.stats or args.full:
        stats = get_table_stats(db_path)
        print(f"\nDatabase: {db_path}")
        print(f"Size: {stats.pop('_db_size_mb')} MB, Total rows: {stats.pop('_total_rows'):,}")
        print(f"{'Table':<30} {'Rows':>10}")
        print("-" * 42)
        for t, info in sorted(stats.items()):
            if info["rows"] > 0:
                print(f"  {t:<28} {info['rows']:>10,}")

    if args.clean_old is not None or args.full:
        days = args.clean_old or 30
        print(f"\nCleaning data older than {days} days...")
        deleted = clean_old_data(db_path, days)
        if deleted:
            total = sum(deleted.values())
            print(f"Deleted {total:,} rows total:")
            for t, cnt in sorted(deleted.items()):
                print(f"  {t}: {cnt:,}")
        else:
            print("No rows to delete.")

    if args.vacuum or args.full:
        print("\nRunning VACUUM...")
        result = vacuum_database(db_path)
        print(f"  Before: {result['size_before_mb']} MB")
        print(f"  After:  {result['size_after_mb']} MB")
        print(f"  Freed:  {result['freed_mb']} MB")


if __name__ == "__main__":
    main()
