from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
from typing import Any

from plugins.crypto_guard.config.loader import PROJECT_ROOT


# DuckDB executable is installed under Program Files, but the analytics
# database itself lives in the project data directory so normal user processes
# can create and update it without elevation.
DEFAULT_DUCKDB_PATH = PROJECT_ROOT / "data" / "duckdb" / "crypto_guard_analytics.duckdb"
DEFAULT_DUCKDB_EXE = Path("D:/Program Files/duckdb/duckdb.exe")


class DuckDBAnalytics:
    """DuckDB-backed analytics.

    Two read paths:

    * ``query_klines`` reads candle data from the Parquet archive (unchanged by
      the PostgreSQL cutover — Parquet is the candle store of record).
    * The frame analytics (``hourly_signal_distribution`` /
      ``paper_account_summary`` / ``daily_review_stats`` /
      ``strategy_performance``) historically pulled rows from the legacy SQLite
      OLTP file into a pandas DataFrame, registered it in DuckDB, and ran
      DuckDB aggregation SQL. The SQLite file no longer exists under the
      PostgreSQL-only runtime, so these now read source rows from PostgreSQL
      via the pooled connection (``pg_db.get_conn()``) and register the frame in
      DuckDB the same way. Aggregation SQL is unchanged.
    """

    def __init__(self, database_path: str | Path = DEFAULT_DUCKDB_PATH, parquet_root: str | Path | None = None, sqlite_path: str | Path | None = None):
        self.database_path = Path(os.environ.get("CRYPTO_GUARD_DUCKDB_PATH") or database_path)
        self.parquet_root = Path(parquet_root) if parquet_root else PROJECT_ROOT / "data" / "parquet" / "klines" / "binance_um"
        # ``sqlite_path`` is retained on the signature for backward-compat with
        # callers/tests that pass it, but is intentionally unused: the SQLite
        # OLTP file is gone under the PostgreSQL-only runtime.

    def health_check(self) -> dict[str, Any]:
        try:
            import duckdb

            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with duckdb.connect(str(self.database_path)) as conn:
                value = conn.execute("SELECT 1").fetchone()[0]
            return {"status": "ok", "database": str(self.database_path), "query": value, "engine": "duckdb_python"}
        except Exception as exc:
            cli = self._cli_query("SELECT 1 AS query", [])
            if cli.get("ok"):
                return {"status": "ok", "database": str(self.database_path), "query": 1, "engine": "duckdb_cli", "python_module_error": str(exc)}
            return {"status": "degraded", "database": str(self.database_path), "error": str(exc), "cli_error": cli.get("error")}

    def query_klines(self, symbol: str, interval: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        path = self.parquet_root / symbol.upper() / interval / "*.parquet"
        sql = "SELECT * FROM read_parquet(?)"
        params: list[Any] = [str(path)]
        where = []
        if start:
            where.append("close_time_utc >= ?")
            params.append(start)
        if end:
            where.append("close_time_utc <= ?")
            params.append(end)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY open_time"
        return self._query(sql, params)

    def hourly_signal_distribution(self, start: str, end: str) -> dict[str, int]:
        rows = self._pg_query(
            "SELECT signal_grade, COUNT(*) AS count "
            "FROM ga_decisions WHERE analysis_time_utc >= %s AND analysis_time_utc < %s "
            "GROUP BY signal_grade",
            [start, end],
        )
        return {str(row["signal_grade"] or "-"): int(row["count"]) for row in rows}

    def paper_account_summary(self, date_utc: str) -> dict[str, Any]:
        # ``arg_max`` (DuckDB) -> PostgreSQL: pick the value at the max
        # ``created_at`` via a DISTINCT ON / ORDER BY pattern. ``created_at`` is
        # TIMESTAMPTZ; compare its UTC date against ``date_utc``.
        rows = self._pg_query(
            """
            SELECT
              COUNT(*) AS samples,
              (SELECT account_equity FROM paper_equity_snapshots
                 WHERE to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') = %s
                 ORDER BY created_at DESC LIMIT 1) AS latest_equity,
              (SELECT realized_pnl FROM paper_equity_snapshots
                 WHERE to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') = %s
                 ORDER BY created_at DESC LIMIT 1) AS realized_pnl,
              (SELECT unrealized_pnl FROM paper_equity_snapshots
                 WHERE to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') = %s
                 ORDER BY created_at DESC LIMIT 1) AS unrealized_pnl,
              MIN(account_equity) AS min_equity,
              MAX(account_equity) AS max_equity
            FROM paper_equity_snapshots
            WHERE to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') = %s
            """,
            [date_utc, date_utc, date_utc, date_utc],
        )
        summary = rows[0] if rows else {}
        # Native PostgreSQL ``COUNT(*)`` over zero rows still yields one row
        # (samples=0, NULL aggregates); the former DuckDB-frame path returned
        # ``[]`` (an empty frame) for zero rows, so the caller saw ``{}``.
        # Match that: treat a zero-sample result as empty.
        if not summary or not summary.get("samples") or summary.get("samples") == 0:
            return {}
        if summary.get("max_equity"):
            summary["drawdown"] = float(summary["min_equity"] or 0) - float(summary["max_equity"] or 0)
        return summary

    def daily_review_stats(self, date_utc: str) -> dict[str, Any]:
        rows = self._pg_query(
            "SELECT COUNT(*) AS reports FROM daily_review_reports WHERE review_date=%s",
            [date_utc],
        )
        return rows[0] if rows else {"reports": 0}

    def strategy_performance(self, strategy_name: str, days: int = 30) -> dict[str, Any]:
        rows = self._pg_query(
            """
            SELECT
              strategy_name,
              SUM(sample_count) AS samples,
              SUM(win_count) AS wins,
              SUM(loss_count) AS losses,
              AVG(avg_rr) AS avg_r
            FROM strategy_memory
            WHERE strategy_name=%s
            GROUP BY strategy_name
            """,
            [strategy_name],
        )
        return rows[0] if rows else {}

    def _query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        try:
            import duckdb

            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with duckdb.connect(str(self.database_path)) as conn:
                cursor = conn.execute(sql, params)
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except ModuleNotFoundError:
            cli = self._cli_query(sql, params)
            if cli.get("ok"):
                return cli["rows"]
            raise RuntimeError(cli.get("error"))

    def _cli_query(self, sql: str, params: list[Any]) -> dict[str, Any]:
        if not DEFAULT_DUCKDB_EXE.exists():
            return {"ok": False, "error": f"duckdb executable not found: {DEFAULT_DUCKDB_EXE}"}
        rendered = _inline_params(sql, params)
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [str(DEFAULT_DUCKDB_EXE), str(self.database_path), "-json", "-c", rendered],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            rows = json.loads(proc.stdout or "[]")
            return {"ok": True, "rows": rows}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _pg_query(self, pg_sql: str, pg_params: list[Any]) -> list[dict[str, Any]]:
        """Run an analytics SELECT directly on PostgreSQL via the pooled
        connection and return dict rows.

        The four frame analytics (``hourly_signal_distribution`` /
        ``paper_account_summary`` / ``daily_review_stats`` /
        ``strategy_performance``) formerly pulled source rows from the legacy
        SQLite file into a pandas DataFrame and ran DuckDB aggregation SQL over
        it. PostgreSQL supports every aggregate they used (COUNT/MIN/MAX/SUM/AVG
        and an ``arg_max``-equivalent via correlated subqueries), so the
        aggregations now run natively in PostgreSQL - no duckdb/pandas/CLI
        dependency, no intermediate frame. The connection is read-only; the
        clean ``get_conn`` boundary closes its read transaction before pool
        return.
        """
        from plugins.crypto_guard.storage import pg_db

        # ``pg_db.get_conn()`` yields a ``dict_row``-factory connection, so each
        # fetched row is already a ``dict`` keyed by column name - no manual
        # ``zip(columns, row)`` is needed (that would zip column names with the
        # dict's keys and produce {col: col} nonsense).
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_sql, pg_params)
                return [dict(row) for row in cur.fetchall()]


def _inline_params(sql: str, params: list[Any]) -> str:
    rendered = sql
    for value in params:
        rendered = rendered.replace("?", _sql_literal(value), 1)
    return rendered


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
