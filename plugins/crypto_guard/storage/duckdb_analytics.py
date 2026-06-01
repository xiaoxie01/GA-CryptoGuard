from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
from typing import Any

from plugins.crypto_guard.config.loader import PROJECT_ROOT, load_config


# DuckDB executable is installed under Program Files, but the analytics
# database itself lives in the project data directory so normal user processes
# can create and update it without elevation.
DEFAULT_DUCKDB_PATH = PROJECT_ROOT / "data" / "duckdb" / "crypto_guard_analytics.duckdb"
DEFAULT_DUCKDB_EXE = Path("D:/Program Files/duckdb/duckdb.exe")


class DuckDBAnalytics:
    def __init__(self, database_path: str | Path = DEFAULT_DUCKDB_PATH, parquet_root: str | Path | None = None, sqlite_path: str | Path | None = None):
        self.database_path = Path(os.environ.get("CRYPTO_GUARD_DUCKDB_PATH") or database_path)
        self.parquet_root = Path(parquet_root) if parquet_root else PROJECT_ROOT / "data" / "parquet" / "klines" / "binance_um"
        self.sqlite_path = Path(sqlite_path) if sqlite_path else Path(load_config().database_path)

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
        rows = self._query_sqlite_frame(
            "SELECT signal_grade FROM ga_decisions WHERE analysis_time_utc >= ? AND analysis_time_utc < ?",
            [start, end],
            "SELECT signal_grade, COUNT(*) AS count FROM rows GROUP BY signal_grade",
        )
        return {str(row["signal_grade"] or "-"): int(row["count"]) for row in rows}

    def paper_account_summary(self, date_utc: str) -> dict[str, Any]:
        rows = self._query_sqlite_frame(
            """
            SELECT account_equity, realized_pnl, unrealized_pnl, created_at
            FROM paper_equity_snapshots
            WHERE substr(created_at, 1, 10)=?
            ORDER BY created_at
            """,
            [date_utc],
            """
            SELECT
              COUNT(*) AS samples,
              arg_max(account_equity, created_at) AS latest_equity,
              arg_max(realized_pnl, created_at) AS realized_pnl,
              arg_max(unrealized_pnl, created_at) AS unrealized_pnl,
              min(account_equity) AS min_equity,
              max(account_equity) AS max_equity
            FROM rows
            """,
        )
        summary = rows[0] if rows else {}
        if summary and summary.get("max_equity"):
            summary["drawdown"] = float(summary["min_equity"] or 0) - float(summary["max_equity"] or 0)
        return summary

    def daily_review_stats(self, date_utc: str) -> dict[str, Any]:
        rows = self._query_sqlite_frame(
            "SELECT review_date, summary_json FROM daily_review_reports WHERE review_date=?",
            [date_utc],
            "SELECT COUNT(*) AS reports FROM rows",
        )
        return rows[0] if rows else {"reports": 0}

    def strategy_performance(self, strategy_name: str, days: int = 30) -> dict[str, Any]:
        rows = self._query_sqlite_frame(
            """
            SELECT strategy_name, sample_count, win_count, loss_count, avg_rr
            FROM strategy_memory
            WHERE strategy_name=?
            """,
            [strategy_name],
            """
            SELECT
              strategy_name,
              sum(sample_count) AS samples,
              sum(win_count) AS wins,
              sum(loss_count) AS losses,
              avg(avg_rr) AS avg_r
            FROM rows
            GROUP BY strategy_name
            """,
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

    def _query_sqlite_frame(self, sqlite_sql: str, sqlite_params: list[Any], duckdb_sql: str) -> list[dict[str, Any]]:
        import sqlite3

        if not self.sqlite_path.exists():
            return []

        try:
            import duckdb
            import pandas as pd

            with sqlite3.connect(str(self.sqlite_path)) as sqlite_conn:
                frame = pd.read_sql_query(sqlite_sql, sqlite_conn, params=sqlite_params)
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with duckdb.connect(str(self.database_path)) as conn:
                conn.register("rows", frame)
                cursor = conn.execute(duckdb_sql)
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except ModuleNotFoundError:
            # pandas or duckdb Python module unavailable: fallback to pure sqlite3
            return self._sqlite_only_fallback(sqlite_sql, sqlite_params)

    def _sqlite_only_fallback(self, sqlite_sql: str, sqlite_params: list[Any]) -> list[dict[str, Any]]:
        """Fallback when neither pandas nor duckdb Python module is available."""
        import sqlite3

        with sqlite3.connect(str(self.sqlite_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sqlite_sql, sqlite_params)
            return [dict(row) for row in cursor.fetchall()]


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
