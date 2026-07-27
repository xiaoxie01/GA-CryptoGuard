"""终审返工 R2 P2-2 (2026-07-26): delete dead DuckDB stats implementation.

RED-first test for the Codex re-review finding:

  The production hourly report no longer uses DuckDB for the hourly scheduled
  aggregate - it is a real PostgreSQL ``scheduled_analysis`` query
  (``_pg_hourly_scheduled_stats`` -> ``repo.hourly_scheduled_analysis_distribution``).
  Three pieces of the old DuckDB path are now DEAD code with no production
  consumer:

    1. ``hourly_report._duckdb_hourly_stats`` - the old DuckDB hourly aggregate.
       Nothing in the production render path calls it; the only remaining
       references were in tests and comments.
    2. ``hourly_report._distribution_source_label`` - returned a static
       "PostgreSQL 当前批次" for any input. No production consumer calls it
       (the render path hardcodes the labels inline); only tests referenced it.
    3. The ``from plugins.crypto_guard.storage.duckdb_analytics import
       DuckDBAnalytics`` at the top of ``hourly_report.py`` - was ONLY used by
       ``_duckdb_hourly_stats``; with that function gone the import is dead.

  Contract (verbatim from the re-review):
    - 删除 hourly_report._duckdb_hourly_stats.
    - 删除 _distribution_source_label (无生产消费方).
    - 删除 hourly_report.py 中已无用的 DuckDBAnalytics import.
    - 删除或重写仅校验这些死辅助函数的测试.
    - 保留 "PostgreSQL 当前批次" + "最近一小时定时分析" 准确标签.

  Note on scope: ``DuckDBAnalytics`` itself is NOT dead - ``run_backtest.py``,
  ``run_pnl_backtest.py``, ``status_tools.py`` and the backtest acceptance test
  still use it for the backtest / health-check path. Only the ``hourly_report``
  usage (the import + ``_duckdb_hourly_stats``) is dead, so only those are
  deleted; the ``duckdb_analytics`` module is untouched.

Uses no PG fixture - this is a pure import/structure test, so it does NOT carry
the ``pg`` / ``e2e`` marks and runs fast in any worker.
"""

from __future__ import annotations

import unittest


class TestDeadDuckDbStatsHelpersRemovedP2(unittest.TestCase):
    """P2-2: the dead DuckDB stats helpers must be GONE from hourly_report."""

    def test_duckdb_hourly_stats_removed(self) -> None:
        """``hourly_report._duckdb_hourly_stats`` must no longer exist."""
        from plugins.crypto_guard.notify import hourly_report
        self.assertFalse(
            hasattr(hourly_report, "_duckdb_hourly_stats"),
            "hourly_report._duckdb_hourly_stats is dead code and must be "
            "deleted; the production hourly aggregate is "
            "_pg_hourly_scheduled_stats (PostgreSQL).",
        )

    def test_distribution_source_label_removed(self) -> None:
        """``hourly_report._distribution_source_label`` must no longer exist
        (no production consumer; the render path hardcodes the PostgreSQL
        labels inline)."""
        from plugins.crypto_guard.notify import hourly_report
        self.assertFalse(
            hasattr(hourly_report, "_distribution_source_label"),
            "hourly_report._distribution_source_label has no production "
            "consumer and must be deleted; the render path hardcodes "
            "'PostgreSQL 当前批次' / '最近1小时定时分析（...）等级分布' inline.",
        )

    def test_duckdbanalytics_import_removed_from_hourly_report(self) -> None:
        """The ``DuckDBAnalytics`` import must be gone from ``hourly_report``
        (it was only used by the now-deleted ``_duckdb_hourly_stats``).

        Verified by inspecting the module's own globals - the name must NOT be
        bound, AND the import line must not appear in the source. The module
        ``duckdb_analytics`` itself is untouched (other modules still use it).
        """
        import inspect
        from plugins.crypto_guard.notify import hourly_report
        # Name not bound in the module namespace.
        self.assertFalse(
            hasattr(hourly_report, "DuckDBAnalytics"),
            "hourly_report must not import DuckDBAnalytics; it was only used "
            "by the deleted _duckdb_hourly_stats.",
        )
        source = inspect.getsource(hourly_report)
        self.assertNotIn(
            "from plugins.crypto_guard.storage.duckdb_analytics import",
            source,
            "the DuckDBAnalytics import line must be removed from "
            "hourly_report.py source.",
        )

    def test_postgresql_labels_preserved_in_render(self) -> None:
        """P2-2 preservation contract: the accurate PostgreSQL labels MUST
        still appear in the rendered hourly summary. Deleting the dead helpers
        must NOT remove the 'PostgreSQL 当前批次' current-batch label or the
        '最近1小时定时分析' hourly line label."""
        from plugins.crypto_guard.notify import hourly_report
        # A current batch of 4 B + 6 C, plus a present hourly aggregate.
        decisions = []
        # Build minimal decision dicts that carry the fields the render path
        # reads (symbol, signal_grade, batch_id). The real render path groups
        # by batch_id and counts grades; we feed a flat list with one batch.
        for i in range(4):
            decisions.append({"symbol": f"B{i}USDT", "signal_grade": "B",
                              "batch_id": "15m:t", "decision_type": "scheduled_analysis"})
        for i in range(6):
            decisions.append({"symbol": f"C{i}USDT", "signal_grade": "C",
                              "batch_id": "15m:t", "decision_type": "scheduled_analysis"})
        stats = {"ok": True, "source": "postgres", "total_decisions": 10,
                 "batch_count": 1, "signal_distribution": {"B": 4, "C": 6}}
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=[], ga_decisions=decisions, open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=stats,
        )
        # Current-batch label preserved.
        self.assertIn("PostgreSQL 当前批次", text, text)
        # Hourly line label preserved.
        self.assertIn("最近1小时定时分析", text, text)
        self.assertIn("PostgreSQL 定时分析", text, text)
        # The dead DuckDB label must NEVER appear.
        self.assertNotIn("DuckDB 时序", text, text)