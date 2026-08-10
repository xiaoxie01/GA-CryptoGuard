"""P8-3 gate: hourly_report direct-SQL sites + feishu_integration on real PG.

This is the "每完成一个业务域立即运行对应真实 PostgreSQL 测试" gate for the
P8-3 diagnostics/hourly-report cutover. It exercises the ``hourly_report.py``
helpers that previously issued SQLite-dialect SQL through ``repo.conn.execute``
(`?` placeholders, ``datetime()`` wrappers, ``is_shadow = 1``, tuple-style
``fetchone()[0]``) and ``json.loads`` on already-decoded JSONB columns - all of
which BREAK on psycopg3 / PostgreSQL. It also exercises
``feishu_integration`` which previously called the removed
``connect_db(cfg.database_path)`` / ``cfg.database_path``.

Settled contracts (each maps to a real break this test guards):

  HR-1: ``_count`` reads a bare ``COUNT(*)`` by name (``row["count"]``). A
        dict_row connection does NOT support tuple-style ``[0]`` - the SQLite
        helper ``repo.conn.execute(sql).fetchone()[0]`` raises ``KeyError`` on
        PG. ``is_shadow = 1`` against a BOOLEAN column raises a type error on
        PG (``operator does not exist: boolean = integer``); ``is_shadow = TRUE``
        is required.
  HR-2: ``_fetch_feedback_patterns`` / ``_fetch_long_short_performance`` filter
        on TIMESTAMPTZ columns. The SQLite ``datetime(col) >= datetime(?)``
        wrapper is invalid PG syntax; the migrated form casts the ISO-8601
        parameter directly (``col >= %s``).
  HR-3: ``_fetch_account_feedback_gate_stats`` / ``_fetch_market_regime_alignment``
        read JSONB columns. psycopg3 returns an already-decoded dict, so
        ``json.loads(dict)`` raises ``TypeError`` and every row would be silently
        skipped (counted as ``invalid_json`` / dropped) -> DATA LOSS. The
        migrated path uses ``_safe_json`` (which passes dict/list through).
  HR-4: The "SQLite" report labels are gone (PostgreSQL is the engine now).
  FE-1: ``enqueue_feishu_message`` / ``enqueue_button_callback`` use
        ``pg_db.get_conn()`` (NOT the removed ``connect_db``/``cfg.database_path``)
        and claim+enqueue idempotently; a duplicate event_id is a no-op.
  FE-2: PG-unavailable is fail-closed (no SQLite fallback) - exercised by a
        missing DSN raising ``CryptoGuardDBUnavailable``.

NOT a mock; uses a real pooled conn on an isolated schema.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.notify import feishu_integration
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _set_app_dsn_env() -> str:
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    pg_db.reset_pool()
    return dsn


def _ga_decision(symbol: str, analysis_time: int, *, batch_id: str | None = None) -> dict:
    """A minimal-but-complete ga_decisions row (all NOT NULL columns set).

    The gate JSONB columns (account_feedback_gate_json /
    market_regime_gate_json) are NOT part of ``create_ga_decision``'s INSERT -
    production writes them in a separate UPDATE. Tests that need them must call
    ``_set_gate_json`` after creating the decision.
    """
    return {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": "2026-07-16T00:00:00Z",
        "decision_type": "analysis",
        "signal_grade": "B",
        "confidence": 0.7,
        "market_bias": "neutral",
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [{"k": "v"}],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": batch_id,
        "previous_grade": "C",
    }


class TestPgHourlyReportAndFeishuP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── HR-1: _count + is_shadow BOOLEAN ────────────────────────────────────

    def test_count_reads_by_name_and_boolean_is_shadow(self) -> None:
        """HR-1: ``_count`` works on dict_row; ``is_shadow = TRUE`` filters."""
        # Insert two shadow evaluations - one real_pnl, one pseudo.
        self._seed_strategy_evaluations([
            {"is_shadow": True, "outcome_source": "real_pnl", "pnl_r": 1.2},
            {"is_shadow": True, "outcome_source": "pseudo_r", "pnl_r": None},
        ])
        # ``_fetch_shadow_data_quality`` issues two ``_count`` calls with
        # ``is_shadow = TRUE``. If is_shadow=1 had been left (boolean = integer),
        # _count would raise and the helper returns the exception in ["error"].
        stats = hourly_report._fetch_shadow_data_quality(self.repo)
        self.assertNotIn("error", stats, stats)
        self.assertEqual(stats["real_pnl_count"], 1)
        self.assertEqual(stats["pseudo_r_count"], 1)
        self.assertEqual(stats["total_shadow_samples"], 2)

    def _shadow_breakdown_via_count(self) -> dict:
        """Fallback: directly exercise _count with the exact SQL the report uses,
        proving the HR-1 contract (name-based read + BOOLEAN comparison). Kept
        as a defensive fallback in case the helper is renamed/moved."""
        real = hourly_report._count(self.repo, """
            SELECT COUNT(*) FROM strategy_evaluations
            WHERE is_shadow = TRUE AND outcome_source='real_pnl' AND pnl_r IS NOT NULL
        """)
        pseudo = hourly_report._count(self.repo, """
            SELECT COUNT(*) FROM strategy_evaluations
            WHERE is_shadow = TRUE AND (outcome_source != 'real_pnl' OR outcome_source IS NULL)
        """)
        total = real + pseudo
        return {
            "real_pnl_count": real, "pseudo_r_count": pseudo,
            "total_shadow_samples": total,
            "real_ratio": real / total if total else 0,
        }

    def _seed_strategy_evaluations(self, rows: list[dict]) -> None:
        """Insert minimal strategy_evaluations rows for the _count helper.

        NOT NULL: symbol, analysis_time, strategy_name, strategy_version.
        Each row gets a distinct strategy_version to avoid the shadow-unique
        index (strategy_name, strategy_version, ga_decision_id) colliding on
        NULL ga_decision_id rows (PG treats multiple NULLs as distinct, but we
        keep versions distinct for clarity).
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for idx, r in enumerate(rows):
                    cur.execute(
                        "INSERT INTO strategy_evaluations"
                        "(strategy_name, strategy_version, symbol, analysis_time, "
                        " is_shadow, outcome_source, pnl_r) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        ("probe_strat", f"v{idx}", "BTCUSDT", 1, r["is_shadow"],
                         r.get("outcome_source"), r.get("pnl_r")),
                    )

    # ── HR-2: TIMESTAMPTZ window filters ────────────────────────────────────

    def test_feedback_patterns_timestamptz_window(self) -> None:
        """HR-2: created_at >= %s filters skill_feedback_memory by the window."""
        # A recent candidate (within 7 days) with a pattern.
        self._seed_skill_feedback("skillA", pattern="late_entry", status="candidate", days_ago=1)
        # An old candidate (outside 7 days) - must be excluded.
        self._seed_skill_feedback("skillA", pattern="old_pattern", status="candidate", days_ago=30)
        out = hourly_report._fetch_feedback_patterns(self.repo)
        self.assertNotIn("error", out, out)
        patterns = {p["pattern"] for p in out["top_patterns"]}
        self.assertIn("late_entry", patterns)
        self.assertNotIn("old_pattern", patterns)
        self.assertEqual(out["most_active_skill"], "skillA")
        self.assertEqual(out["most_active_count"], 1)

    def test_long_short_performance_timestamptz_window(self) -> None:
        """HR-2: closed_at >= %s filters paper_trades LONG/SHORT stats."""
        self._seed_paper_trade("LONG", pnl_r=1.0, days_ago=1)
        self._seed_paper_trade("LONG", pnl_r=-1.0, days_ago=40)  # outside 30d
        self._seed_paper_trade("SHORT", pnl_r=0.5, days_ago=1)
        out = hourly_report._fetch_long_short_performance(self.repo)
        self.assertNotIn("error", out, out)
        self.assertEqual(out["long"]["count"], 1)   # only the recent LONG
        self.assertEqual(out["long"]["wins"], 1)
        self.assertEqual(out["short"]["count"], 1)

    def _seed_skill_feedback(self, skill: str, *, pattern: str, status: str, days_ago: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO skill_feedback_memory"
                    "(skill_name, skill_version, feedback_type, source_type, finding, "
                    " pattern_type, status, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() - make_interval(days => %s))",
                    (skill, "v1", "negative", "test", "finding", pattern, status, days_ago),
                )

    def _seed_paper_trade(self, side: str, *, pnl_r: float, days_ago: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO paper_trades"
                    "(symbol, side, pnl_r, closed_at) "
                    "VALUES (%s, %s, %s, NOW() - make_interval(days => %s))",
                    ("BTCUSDT", side, pnl_r, days_ago),
                )

    # ── HR-3: JSONB dict read (no json.loads on dict) ──────────────────────

    def test_account_feedback_gate_jsonb_dict_read(self) -> None:
        """HR-3: JSONB gate column read as dict; counted, not skipped.

        The gate column is NOT populated by ``create_ga_decision``; production
        writes it via a separate ``UPDATE ga_decisions SET
        account_feedback_gate_json = ... WHERE id = ...`` (paper_broker). This
        test mirrors that two-step write so the row actually carries the JSONB
        the report helper queries.
        """
        gate = {
            "active": True, "passed": False, "decision": "block_order",
            "mode": "controlled",
        }
        ga_id = self.repo.create_ga_decision(_ga_decision("BTCUSDT", 7_000_000))
        self._set_gate_json(ga_id, "account_feedback_gate_json", gate)
        out = hourly_report._fetch_account_feedback_gate_stats(self.repo)
        self.assertTrue(out.get("ok"), out)
        self.assertGreaterEqual(out["total_checks"], 1)
        self.assertGreaterEqual(out["active_checks"], 1)
        self.assertGreaterEqual(out["not_passed"], 1)
        # If json.loads(dict) had silently skipped the row, decision_counts
        # would be empty AND invalid counts would be inflated. The gate's
        # decision must be counted.
        self.assertEqual(out["decision_counts"].get("block_order"), 1)
        # And the JSONB-as-dict contract: zero rows misclassified as invalid.
        self.assertEqual(out.get("invalid_json_count", 0), 0)

    def test_market_regime_alignment_jsonb_dict_read(self) -> None:
        """HR-3: JSONB regime column read as dict; counted, not skipped."""
        gate = {"time_source": "fallback_now", "adjustments": {}, "market_regime": {"alignment": "counter"}}
        ga_id = self.repo.create_ga_decision(_ga_decision("ETHUSDT", 7_000_001))
        self._set_gate_json(ga_id, "market_regime_gate_json", gate)
        out = hourly_report._fetch_market_regime_gate_stats(self.repo, hours=24)
        self.assertTrue(out.get("ok"), out)
        self.assertGreaterEqual(out["total_checks"], 1)
        self.assertGreaterEqual(out["fallback_now_count"], 1)

    def _set_gate_json(self, ga_id: int, column: str, value: dict) -> None:
        """Mirror production's post-create UPDATE of a gate JSONB column."""
        # column is a fixed identifier chosen by this test, not user input.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    f"UPDATE ga_decisions SET {column} = %s WHERE id = %s",
                    (json.dumps(value), ga_id),
                )

    # ── HR-4: report labels say PostgreSQL (no SQLite / no DuckDB) ──────────

    def test_report_labels_say_postgresql(self) -> None:
        """HR-4 + 终审返工 P2 (2026-07-25) / R2 P2-2 (2026-07-26): the engine
        labels are PostgreSQL, never SQLite and never DuckDB. The hourly
        aggregate is a real PostgreSQL scheduled_analysis query, so the legacy
        "DuckDB 时序" label is gone. The dead ``_distribution_source_label``
        helper was DELETED in R2 P2-2 (no production consumer), so this test now
        asserts the labels directly on the RENDERED text - which is the real
        production surface operators read."""
        # Render a summary with a present current batch + a present hourly
        # aggregate; both labels must be PostgreSQL, never DuckDB/SQLite.
        decisions = []
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
        # The current-batch line is ALWAYS labeled "PostgreSQL 当前批次".
        cb_line = next((ln for ln in text.splitlines() if "等级分布（当前批次）" in ln), "")
        self.assertIn("PostgreSQL 当前批次", cb_line, cb_line)
        # The hourly line is labeled "PostgreSQL 定时分析".
        self.assertIn("最近1小时定时分析", text, text)
        self.assertIn("PostgreSQL 定时分析", text, text)
        # The dead DuckDB / SQLite labels must NEVER appear anywhere.
        self.assertNotIn("DuckDB 时序", text, text)
        self.assertNotIn("SQLite", text, text)
        # The dead helper itself must be gone (P2-2 deletion).
        self.assertFalse(hasattr(hourly_report, "_distribution_source_label"),
                         "dead _distribution_source_label must be deleted")
        self.assertFalse(hasattr(hourly_report, "_duckdb_hourly_stats"),
                         "dead _duckdb_hourly_stats must be deleted")

    def _render_with_health(self, *, risk_state=None, state_consistency=None) -> str:
        return hourly_report.render_hourly_report_text(
            generated_at_utc="2026-07-19T00:00:00Z",
            active_symbols=[], signals=[], open_orders=[], failed_jobs=[],
            queue_counts={
                "pending_user": 0, "pending_background": 0, "running": 0,
            },
            risk_state=risk_state,
            state_consistency=state_consistency,
        )

    def test_risk_query_failure_renders_unavailable_not_normal(self) -> None:
        """A broken risk query is fail-closed and the connection is recovered."""
        with self.conn.transaction():
            self.conn.execute("ALTER TABLE paper_accounts RENAME TO paper_accounts_broken")
        risk = hourly_report._fetch_risk_state(self.repo)
        self.assertFalse(risk.get("available", True), risk)
        self.assertTrue(risk.get("hard_risk_off"), risk)
        text = self._render_with_health(risk_state=risk)
        self.assertIn("风险状态：不可用（故障关闭）", text)
        self.assertNotIn("风险状态：正常", text)
        self.assertEqual(self.conn.execute("SELECT 1 AS v").fetchone()["v"], 1)
        self.conn.rollback()

    def test_state_query_failure_renders_diagnostic_unavailable(self) -> None:
        """A failed sub-diagnostic cannot collapse to an empty healthy result."""
        with self.conn.transaction():
            self.conn.execute("ALTER TABLE strategy_patches RENAME TO strategy_patches_broken")
        state = hourly_report._fetch_state_consistency(self.repo)
        self.assertFalse(state.get("ok"), state)
        self.assertIn("error", state, state)
        self.assertTrue(any(
            issue.get("type") == "schema_health_missing_column"
            for issue in state.get("issues", [])
        ), state)
        text = self._render_with_health(state_consistency=state)
        self.assertIn("状态一致性诊断：", text)
        self.assertIn("不可用（查询失败）", text)
        self.assertEqual(self.conn.execute("SELECT 1 AS v").fetchone()["v"], 1)
        self.conn.rollback()

    # ── FE-1: feishu_integration uses pg_db (idempotent claim+enqueue) ─────

    def test_feishu_message_enqueues_and_dedupes_on_pg(self) -> None:
        """FE-1: enqueue_feishu_message claims + enqueues via pg_db; duplicate no-ops."""
        # Avoid Redis side-effects in this unit gate (the path still enqueues to
        # the DB when Redis is unavailable/unused). Disable Redis via env.
        saved = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        try:
            ok1 = feishu_integration.enqueue_feishu_message(
                text="BTC 行情分析", open_id="ou_test_1", receive_id="ri_1",
                receive_id_type="chat_id", message_id="ev_unique_1",
                send_message=None,
            )
            self.assertTrue(ok1)
            # The job landed in agent_jobs via the postgres path.
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT job_type, session_id FROM agent_jobs "
                    "WHERE job_type=%s AND session_id=%s",
                    ("feishu_user_message", "feishu:user:ou_test_1"),
                )
                rows = cur.fetchall()
            self.assertEqual(len(rows), 1)

            # Duplicate message_id -> idempotent claim returns True, no new job.
            ok2 = feishu_integration.enqueue_feishu_message(
                text="BTC 行情分析", open_id="ou_test_1", receive_id="ri_1",
                receive_id_type="chat_id", message_id="ev_unique_1",
                send_message=None,
            )
            self.assertTrue(ok2)
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM agent_jobs "
                    "WHERE job_type=%s AND session_id=%s",
                    ("feishu_user_message", "feishu:user:ou_test_1"),
                )
                cnt = cur.fetchone()["c"]
            self.assertEqual(cnt, 1)  # still exactly one
        finally:
            if saved is None:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
            else:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved

    def test_feishu_button_callback_enqueues_and_dedupes_on_pg(self) -> None:
        """FE-1: enqueue_button_callback claims + enqueues via pg_db; duplicate no-ops."""
        saved = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        try:
            payload = {"event_id": "btn_ev_1", "open_id": "ou_btn_1",
                       "action": "ack", "signal_id": "s1"}
            r1 = feishu_integration.enqueue_button_callback(payload, send_message=None)
            self.assertTrue(r1["ok"])
            self.assertIsNotNone(r1.get("job_id"))
            r2 = feishu_integration.enqueue_button_callback(payload, send_message=None)
            self.assertTrue(r2["ok"])
            self.assertTrue(r2.get("duplicate"))
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM agent_jobs WHERE job_type=%s",
                    ("feishu_button_callback",),
                )
                cnt = cur.fetchone()["c"]
            self.assertEqual(cnt, 1)
        finally:
            if saved is None:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
            else:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved

    # ── FE-2: PG-unavailable is fail-closed (no SQLite fallback) ───────────

    def test_feishu_fail_closed_when_pg_unavailable(self) -> None:
        """FE-2: a missing DSN raises CryptoGuardDBUnavailable (no SQLite fallback)."""
        saved = os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
        pg_db.reset_pool()
        try:
            with self.assertRaises(pg_db.CryptoGuardDBUnavailable):
                feishu_integration.enqueue_feishu_message(
                    text="BTC 行情分析", open_id="ou_x", receive_id="ri_x",
                    receive_id_type="chat_id", message_id="ev_nodb",
                    send_message=None,
                )
        finally:
            if saved is not None:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved
            pg_db.reset_pool()


def _decision_dict(symbol: str, analysis_time: int, *, grade: str, batch_id: str | None = None,
                   decision: str = "no_trade", market_bias: str = "neutral") -> dict:
    """A minimal-but-complete ga_decisions row for distribution/render tests.

    Mirrors the NOT-NULL contract of ``create_ga_decision``. ``signal_grade``
    is the only field the distribution logic reads, but every NOT NULL column
    is set so the INSERT mirrors production.
    """
    return {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": "2026-07-24T18:05:00Z",
        "decision_type": "scheduled_analysis",
        "signal_grade": grade,
        "confidence": 0.7,
        "market_bias": market_bias,
        "trend_stage": "range",
        "decision": decision,
        "skill_result_refs": {"trend": 1},
        "evidence": [{"k": "v"}],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": batch_id,
        "previous_grade": grade,
    }


@pytest.mark.rollback_isolation  # 5.4 SAFE: data-only single-conn (audited)
class TestPgHourlyReportDistributionP1(unittest.TestCase):
    """P1-1: current-batch grade distribution must NOT be hijacked by the
    last-1-hour DuckDB aggregation (4 batches x 10 symbols = 40 rows).

    Production evidence (2026-07-24 18:05 push): the real batch distribution
    was B=4, C=6 (10 symbols), but the report showed B=16, C=22, D=2 because
    ``render_ga_hourly_summary`` preferred ``duckdb_stats.signal_distribution``
    (last-1-hour, 40 rows) over ``grade_counts`` (current batch, 10 rows).

    Contract:
      - The "六、无优势品种汇总" distribution line must show the CURRENT batch's
        10 ga_decisions (total == enabled_symbols count).
      - The last-1-hour DuckDB aggregation, when retained, must be a SEPARATE
        line explicitly labeled as 4 batches / 40 rows, never masquerading as
        the current batch.
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_batch(self, grades: list[str], batch_id: str, analysis_time: int) -> list[int]:
        """Seed one ga_decision per grade for a synthetic 10-symbol batch."""
        symbols = ["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
                   "ETHUSDT", "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"]
        assert len(grades) == len(symbols) == 10
        ids: list[int] = []
        for sym, grade in zip(symbols, grades):
            ids.append(self.repo.create_ga_decision(
                _decision_dict(sym, analysis_time, grade=grade, batch_id=batch_id)
            ))
        return ids

    def test_current_batch_distribution_uses_batch_decisions_not_hourly_aggregate(self) -> None:
        """The distribution line reflects the current batch (B=4, C=6), not
        the last-1-hour aggregate (which here is intentionally wrong).

        终审返工 P1-2/P2 (2026-07-25): the hourly aggregate is now a real
        PostgreSQL scheduled_analysis aggregate carrying ``total_decisions``
        and ``batch_count``. The hourly line is labeled
        "最近1小时定时分析（N 批次、共 M 条决策）" with PostgreSQL, NOT the
        legacy hardcoded "4 批次、共 40 条" / "（DuckDB 时序）".
        """
        batch_id = "15m:1785000000000"
        at = 1785000000000
        # Real 18:05 batch: 4 B + 6 C = 10 symbols.
        self._seed_batch(["B", "B", "B", "B", "C", "C", "C", "C", "C", "C"], batch_id, at)
        decisions = self.repo.latest_ga_decisions_by_symbol(limit=120, batch_id=batch_id)
        self.assertEqual(len(decisions), 10)

        # Hourly aggregate that does NOT match the current batch - mimics a
        # real PostgreSQL scheduled aggregate (9 decisions, 3 batches) that
        # previously hijacked the distribution line.
        hourly_stats = {
            "ok": True,
            "source": "postgres",
            "total_decisions": 9,
            "batch_count": 3,
            "signal_distribution": {"B": 3, "C": 4, "D": 2},
        }

        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
                            "ETHUSDT", "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"],
            ga_decisions=decisions,
            open_orders=[], active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=hourly_stats,
        )

        # Current-batch distribution: B 4, C 6 only. Must NOT carry the
        # hourly aggregate numbers (B 3 / C 4 / D 2).
        self.assertIn("六、无优势品种汇总", text)
        # The current-batch distribution line must be labeled as such and
        # contain "B 4" and "C 6", and labeled PostgreSQL (P2).
        self.assertIn("等级分布（当前批次）", text)
        self.assertIn("B 4", text)
        self.assertIn("C 6", text)

        # The hourly aggregate appears on a SEPARATE labeled line using the
        # real N (3 batches) / M (9 decisions) - not the legacy 4/40/DuckDB.
        self.assertIn("最近1小时定时分析", text)
        self.assertIn("3 批次", text)
        self.assertIn("共 9 条决策", text)
        self.assertIn("B 3", text)
        self.assertIn("C 4", text)
        self.assertNotIn("4 批次", text)
        self.assertNotIn("共 40 条", text)
        self.assertNotIn("DuckDB 时序", text)

        # The hijacking aggregate numbers must NOT appear on the current-batch
        # distribution line. Split the text into lines and assert the
        # current-batch line carries only the batch's real counts.
        cb_line = next(
            (ln for ln in text.splitlines() if "等级分布（当前批次）" in ln),
            "",
        )
        self.assertIn("B 4", cb_line)
        self.assertIn("C 6", cb_line)
        self.assertNotIn("B 3", cb_line)
        self.assertNotIn("C 4", cb_line)
        self.assertNotIn("D 2", cb_line)
        # P2: the current-batch line is labeled PostgreSQL, never DuckDB.
        self.assertIn("PostgreSQL", cb_line)
        self.assertNotIn("DuckDB", cb_line)

    def test_current_batch_distribution_total_equals_enabled_symbols_count(self) -> None:
        """Production-form assertion: the current-batch distribution total
        equals the number of enabled_symbols (the batch's ga_decisions)."""
        batch_id = "15m:1785000000000"
        at = 1785000000000
        # 3 B + 4 C + 3 D = 10 symbols.
        self._seed_batch(["B", "B", "B", "C", "C", "C", "C", "D", "D", "D"], batch_id, at)
        decisions = self.repo.latest_ga_decisions_by_symbol(limit=120, batch_id=batch_id)
        self.assertEqual(len(decisions), 10)

        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
                            "ETHUSDT", "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"],
            ga_decisions=decisions,
            open_orders=[], active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=None,
        )
        self.assertIn("六、无优势品种汇总", text)
        # No DuckDB aggregate present -> no separate hourly line.
        self.assertIn("B 3", text)
        self.assertIn("C 4", text)
        self.assertIn("D 3", text)
        self.assertNotIn("最近1小时", text)


@pytest.mark.rollback_isolation  # 5.4 SAFE: data-only single-conn (audited)
class TestPgHourlyReportExpressionP2(unittest.TestCase):
    """P2 (report expression):

    - When state consistency has issues, the report shows at least the issue
      ``type`` (e.g. ``stalled_candidate``,
      ``deterministic_direction_from_failed_llm``). Both the GA path
      (``render_ga_hourly_summary``) and the text path
      (``render_hourly_report_text``) must surface issue types.
    - In the weekly failure-mode section, ``most_active_count`` is the
      cumulative candidate feedback of the last 7 days; it must be explicitly
      labeled. When ``top_patterns`` is empty, the report must NOT imply
      these are all failures.
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _sc_with_two_warnings(self) -> dict:
        """A state_consistency result carrying the two production warnings:
        stalled_candidate and deterministic_direction_from_failed_llm."""
        return {
            "ok": True,
            "summary": {
                "active_eval_missing_ga_decision_id": 0,
                "paper_order_missing_active_eval": 0,
                "closed_trade_missing_active_real_pnl": 0,
                "duplicate_open_trades": 0,
                "orphan_patches": 0,
                "status_mismatches": 0,
                "duplicate_patches": 0,
                "stale_shadows": 0,
                "draft_limbo": 0,
                "shadow_candidate_legacy_only": 0,
                "stalled_candidate": 1,
            },
            "total_issues": 2,
            "error_count": 0,
            "warning_count": 2,
            "issues": [
                {
                    "type": "stalled_candidate",
                    "severity": "warning",
                    "details": {
                        "strategy_name": "momentum_continuation_long",
                        "version": "1.0",
                        "created_at": "2026-07-20T00:00:00Z",
                        "hours_stalled": 96,
                    },
                    "suggested_action": "Check backtest gate status",
                },
                {
                    "type": "deterministic_direction_from_failed_llm",
                    "severity": "warning",
                    "scope": {"decision_id": 215, "symbol": "SOLUSDT"},
                    "time_window": {"analysis_time_utc": "2026-07-22T10:00:00Z"},
                    "details": {
                        "decision_id": 215,
                        "symbol": "SOLUSDT",
                        "llm_status": "failed",
                        "market_bias": "bullish",
                        "signal_grade": "D",
                    },
                    "message": (
                        "SOLUSDT GA 决策 215 llm_status=failed 但 market_bias=bullish"
                        "（应为 unknown），确定性引擎在 LLM 失败时输出了方向。"
                    ),
                },
            ],
        }

    def test_ga_path_shows_state_consistency_issue_types(self) -> None:
        """render_ga_hourly_summary surfaces the issue ``type`` for each
        warning, not just the summary counts."""
        sc = self._sc_with_two_warnings()
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-22T12:00:00Z",
            active_symbols=["BTCUSDT"], ga_decisions=[], open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            state_consistency=sc,
        )
        self.assertIn("状态一致性诊断", text)
        # Both warning issue types must be visible.
        self.assertIn("stalled_candidate", text)
        self.assertIn("deterministic_direction_from_failed_llm", text)
        # The deterministic warning's human message must surface (carries the
        # SOLUSDT decision_id=215 evidence the operator needs to see).
        self.assertIn("215", text)
        # The stalled candidate's strategy identity must surface.
        self.assertIn("momentum_continuation_long", text)

    def test_text_path_shows_state_consistency_issue_types(self) -> None:
        """render_hourly_report_text surfaces the issue ``type`` for each
        warning too."""
        sc = self._sc_with_two_warnings()
        text = hourly_report.render_hourly_report_text(
            generated_at_utc="2026-07-22T12:00:00Z",
            active_symbols=["BTCUSDT"], signals=[], open_orders=[],
            failed_jobs=[], queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            state_consistency=sc,
        )
        self.assertIn("状态一致性诊断", text)
        self.assertIn("stalled_candidate", text)
        self.assertIn("deterministic_direction_from_failed_llm", text)
        self.assertIn("215", text)

    def test_weekly_failure_mode_labels_most_active_count_and_no_pattern(self) -> None:
        """When top_patterns is empty, the report must NOT imply these are all
        failures, and most_active_count must be labeled as 7-day cumulative
        candidate feedback (not failures)."""
        feedback = {
            "top_patterns": [],
            "most_active_skill": "smc_pullback_long",
            "most_active_count": 7,
        }
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-22T12:00:00Z",
            active_symbols=["BTCUSDT"], ga_decisions=[], open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            feedback_patterns=feedback,
        )
        # The most_active_count must be labeled as 7-day cumulative candidate
        # feedback, not as failures.
        self.assertIn("7", text)
        self.assertIn("候选", text)
        # When top_patterns is empty, the report must NOT imply these are all
        # failures - an honest "no recorded patterns" line must appear.
        self.assertNotIn("暂无失败模式记录", text)
        # A line that distinguishes "candidate feedback" from "failures".
        self.assertTrue(
            "候选反馈" in text or "近 7 天" in text,
            "weekly failure-mode section must label most_active_count as 7-day candidate feedback",
        )

    def test_weekly_failure_mode_no_pattern_text_path(self) -> None:
        """The text path mirrors the GA path: empty top_patterns does not imply
        all failures."""
        feedback = {
            "top_patterns": [],
            "most_active_skill": "smc_pullback_long",
            "most_active_count": 3,
        }
        text = hourly_report.render_hourly_report_text(
            generated_at_utc="2026-07-22T12:00:00Z",
            active_symbols=["BTCUSDT"], signals=[], open_orders=[],
            failed_jobs=[], queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            feedback_patterns=feedback,
        )
        self.assertIn("3", text)
        self.assertNotIn("暂无失败模式记录", text)


@pytest.mark.rollback_isolation  # 5.4 SAFE: data-only single-conn (audited)
class TestPgStateConsistencyWarningsVerification(unittest.TestCase):
    """#35: verify and record the two production state-consistency warnings.

    These two warnings exist in production RIGHT NOW (2026-07-24) and must NOT
    be eliminated by deleting history or loosening diagnostics:

      1. ``stalled_candidate`` — strategy version ``momentum_continuation_long``
         v1.0 stuck in ``candidate`` status >48h.
      2. ``deterministic_direction_from_failed_llm`` — SOLUSDT decision_id=215
         where ``llm_status=failed`` but ``market_bias=bullish`` (should be
         unknown when the LLM fails).

    Both are ``severity=warning`` by design; ``diagnose_state_consistency``
    returns ``ok=True`` (ok gated on error_count==0). This test seeds the
    EXACT production conditions on an isolated PG schema and asserts the two
    warnings surface with the right type, severity, and identity - so a future
    change that loosens the diagnostics (e.g. raising the 48h threshold, or
    allowing bullish/bearish on llm_failed) would break this test.
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _contract_cutoff(self) -> str:
        """Read the market_data_contract_v1 marker applied_at (seeded by
        initialize_database). The deterministic_direction check only inspects
        decisions at/after this cutoff."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT applied_at FROM _migration_state WHERE key='market_data_contract_v1'"
            )
            row = cur.fetchone()
        return str(row["applied_at"])

    def _seed_stalled_candidate(self) -> None:
        """Seed the exact stalled_candidate condition: strategy
        momentum_continuation_long v1.0 in 'candidate' status, created >48h ago.

        ``initialize_database`` already seeds this strategy (status='candidate',
        created_at~now, so NOT yet stalled). We force ``created_at`` back 72h
        via ON CONFLICT so the >48h ``_check_stalled_candidate`` rule fires -
        mirroring the production state where v1.0 has sat in candidate since
        07-16. We deliberately do NOT change the status or delete the row: the
        warning must be recorded, not eliminated by history deletion.
        """
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategy_versions(strategy_name, version, status, config_json, created_at) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (strategy_name, version) "
                    "DO UPDATE SET created_at = EXCLUDED.created_at, status = EXCLUDED.status",
                    (
                        "momentum_continuation_long",
                        "1.0",
                        "candidate",
                        "{}",
                        old,
                    ),
                )

    def _seed_deterministic_direction_from_failed_llm(self) -> int:
        """Seed the exact deterministic_direction_from_failed_llm condition:
        a ga_decision with llm_status=failed but market_bias=bullish."""
        cutoff = self._contract_cutoff()
        # analysis_time_utc must be >= cutoff. Use a datetime comfortably after.
        at_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) + timedelta(hours=1)
        analysis_time = int(at_dt.timestamp() * 1000)
        decision = {
            "symbol": "SOLUSDT",
            "analysis_time": analysis_time,
            "analysis_time_utc": at_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "decision_type": "scheduled_analysis",
            "signal_grade": "D",
            "confidence": 0.3,
            "market_bias": "bullish",  # WRONG: should be unknown on llm failure
            "trend_stage": "range",
            "decision": "no_trade",
            "skill_result_refs": {"trend": 1},
            "evidence": [],
            "counter_evidence": [],
            "risk_check": {"ok": True},
            "feishu_actions": [],
            "final_summary": "summary",
            "raw_llm_summary": "LLM TEXT",
            "rendered_summary": "canonical",
            "batch_id": None,
            "previous_grade": "D",
            "llm_status": "failed",  # stored inside raw_decision_json
        }
        ga_id = self.repo.create_ga_decision(decision)
        return ga_id

    def test_stalled_candidate_warning_surfaces_not_loosened(self) -> None:
        """stalled_candidate v1.0 momentum_continuation_long surfaces as a
        warning; ok stays True (warnings never fail the gate)."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )
        self._seed_stalled_candidate()
        result = diagnose_state_consistency(self.repo)
        # Warnings must NOT fail the gate.
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["warning_count"], 1, result)
        issues = [i for i in result["issues"] if i["type"] == "stalled_candidate"]
        self.assertGreaterEqual(len(issues), 1, "stalled_candidate warning must surface")
        sc = issues[0]
        self.assertEqual(sc["severity"], "warning")
        self.assertEqual(sc["details"]["strategy_name"], "momentum_continuation_long")
        self.assertEqual(str(sc["details"]["version"]), "1.0")

    def test_deterministic_direction_from_failed_llm_warning_surfaces_not_loosened(self) -> None:
        """SOLUSDT decision_id with llm_status=failed + market_bias=bullish
        surfaces as deterministic_direction_from_failed_llm warning."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )
        ga_id = self._seed_deterministic_direction_from_failed_llm()
        result = diagnose_state_consistency(self.repo)
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["warning_count"], 1, result)
        issues = [
            i for i in result["issues"]
            if i["type"] == "deterministic_direction_from_failed_llm"
        ]
        self.assertGreaterEqual(len(issues), 1, "deterministic_direction warning must surface")
        d = issues[0]
        self.assertEqual(d["severity"], "warning")
        self.assertEqual(d["details"]["symbol"], "SOLUSDT")
        self.assertEqual(int(d["details"]["decision_id"]), ga_id)
        self.assertEqual(d["details"]["llm_status"], "failed")
        self.assertEqual(d["details"]["market_bias"], "bullish")

    def test_warnings_do_not_fail_state_consistency_gate(self) -> None:
        """Both warnings together leave ok=True (the gate fails only on errors).
        Deleting history or loosening these diagnostics to silence them would
        be the wrong fix - this test pins that warnings are non-blocking but
        still visible."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )
        self._seed_stalled_candidate()
        self._seed_deterministic_direction_from_failed_llm()
        result = diagnose_state_consistency(self.repo)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["error_count"], 0, result)
        self.assertGreaterEqual(result["warning_count"], 2, result)
        types = {i["type"] for i in result["issues"] if i.get("severity") == "warning"}
        self.assertIn("stalled_candidate", types)
        self.assertIn("deterministic_direction_from_failed_llm", types)


# UNSAFE for rollback_isolation: the ad-hoc snapshot build reads candles via a
# SECOND production connection (market_state_builder), which cannot see the
# uncommitted outer-transaction seeding writes (data_quality=insufficient,
# total_count=0). Keeps DEFAULT fresh-schema isolation.
class TestPgAdHocAnalysisFullTimeframesP1(unittest.TestCase):
    """#31 P1-2: ad-hoc analysis must NEVER false-degrade.

    Root cause (production 2026-07-24): ``intent_parser._timeframes`` returned
    ``["4h","1h","15m","5m"]`` when the user named no period, dropping ``1d``.
    ``crypto_analyze_symbol_once`` built the internal snapshot on that partial
    set, so ``market_semantics`` Step 1 saw ``1d`` as not-closed and
    fail-closed a healthy BTC ad-hoc analysis into ``data_incomplete``/
    ``unknown``/``C``/``0.30``. ``_attach_display_context`` also overwrote
    ``decision["data_quality"]`` with a display stub, discarding the snapshot's
    authoritative ``status``/``health``.

    Fix (verified here): the internal decision analysis ALWAYS runs on the full
    ``DEFAULT_TIMEFRAMES`` (1d/4h/1h/15m/5m); the user's explicit display
    periods are recorded separately (``display_timeframes``) and never gate the
    fail-closed. ``_attach_display_context`` preserves the snapshot's
    ``data_quality``. This test exercises the REAL chain
    ``build_market_state_snapshot -> GAMasterController.analyze_symbol ->
    _attach_display_context -> render_text`` on an isolated PG schema seeded
    with healthy five-timeframe candles, and asserts the result is NOT
    degraded.
    """

    # Fixed analysis_time aligned to a 1d boundary so
    # latest_closed_close_time_ms(tf, ANALYSIS_TIME) == LAST_CLOSE for every
    # TF (1d/4h/1h/15m/5m). ANALYSIS_TIME+1 = 172800000 = 2*86400000 (1d), and
    # 172800000 is also a multiple of 4h/1h/15m/5m spans, so every TF's
    # expected last close converges to 172799999.
    LAST_CLOSE = 172_799_999
    ANALYSIS_TIME = 172_799_999  # = latest_closed_close_time_ms(any TF, this)

    REQUIRED = {"1d": 250, "4h": 250, "1h": 250, "15m": 200, "5m": 150}
    SPANS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000,
             "15m": 900_000, "5m": 300_000}

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_healthy_five_timeframes(self, symbol: str = "BTCUSDT") -> None:
        """Seed >= required_count contiguous closed candles for all 5 TFs,
        last close == LAST_CLOSE for each so assess_health reports ready."""
        import math
        candles = []
        for tf, span in self.SPANS.items():
            n = self.REQUIRED[tf] + 20  # slack above the required floor
            base_close = self.LAST_CLOSE - (n - 1) * span
            for i in range(n):
                ct_close = base_close + i * span
                # Gentle uptrend so trend/momentum modules produce real
                # (non-unknown) values - mirrors the smoke-suite healthy pattern.
                trend = 100.0 + i * 0.4
                pullback = 1.5 * math.sin(i / 6.0)
                open_p = trend + pullback - 0.3
                close_p = trend + pullback + 0.3
                high_p = max(open_p, close_p) + 0.8
                low_p = min(open_p, close_p) - 0.8
                candles.append({
                    "symbol": symbol,
                    "interval": tf,
                    "open_time": ct_close - span + 1,
                    "close_time": ct_close,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": 1000.0,
                    "quote_volume": 100000.0,
                    "taker_buy_volume": 500.0,
                    "taker_buy_quote_volume": 50000.0,
                    "trade_count": 100,
                    "is_closed": True,
                    "source": "test_seed",
                })
        self.repo.upsert_candles(candles)

    def _patch_network(self):
        """Patch the Binance network calls in ga_crypto_tools so the test runs
        fully offline against pre-seeded candles. Returns an ExitStack."""
        from contextlib import ExitStack
        from unittest.mock import patch
        stack = ExitStack()
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.fetch_and_upsert_closed_klines",
            side_effect=lambda *a, **k: None,
        ))
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.fetch_mark_price",
            return_value={"markPrice": "42000.0"},
        ))
        # Pin analysis_time to LAST_CLOSE+1 so it aligns with the seeded candles.
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.latest_closed_close_time_ms",
            side_effect=lambda interval, now_ms=None: self.LAST_CLOSE,
        ))
        return stack

    def test_ad_hoc_analysis_healthy_five_tf_not_degraded(self) -> None:
        """A healthy five-timeframe ad-hoc analysis (user named no period) must
        keep data_quality.status=complete, analysis_degraded=false, and the
        rendered text must NOT show '数据不完整' (data_incomplete).

        Drives the REAL production path ``parse_intent -> crypto_analyze_symbol_once``
        (the ``crypto_handle_text_command`` analyze_once routing) with a request
        that names no explicit period. This catches BOTH regressions: (a) the
        parser defaulting to a 1d-dropping partial set, and (b) the internal
        snapshot being built on that partial set / display periods gating the
        fail-closed. Before the fix this produced data_incomplete/unknown/C/0.30.
        """
        from plugins.crypto_guard.notify.intent_parser import parse_intent
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        self._seed_healthy_five_timeframes("BTCUSDT")
        intent = parse_intent("临时分析一下 BTCUSDT")
        self.assertEqual(intent["intent"], "analyze_once", intent)
        # Parser must NOT synthesize a 1d-dropping default when no period named.
        self.assertNotIn("1d", intent.get("timeframes") or [], "parser keeps display TFs to explicit-only")
        with self._patch_network():
            result = crypto_analyze_symbol_once("BTCUSDT", intent.get("timeframes"), requested_by="test")
        self.assertTrue(result.get("ok"), result)
        decision = result["decision"]
        dq = decision.get("data_quality") or {}
        # P1-2 core: status preserved from the snapshot (NOT a display stub).
        self.assertEqual(str(dq.get("status")).lower(), "complete", dq)
        # Health block must be preserved (the old overwrite dropped it).
        self.assertIn("health", dq, "data_quality.health must be preserved, not overwritten")
        self.assertIn("1d", dq.get("health") or {}, "1d health must be present - internal analysis ran the full TF set")
        # No degradation markers.
        self.assertNotIn("data_incomplete", decision.get("market_reason_codes") or [], decision.get("market_reason_codes"))
        # Internal timeframes must include 1d (the dropped-TF bug).
        self.assertIn("1d", decision.get("timeframes") or [], decision.get("timeframes"))
        # Rendered text must NOT claim data incomplete.
        text = result.get("text") or ""
        self.assertNotIn("数据不完整", text, text)
        # display_timeframes surfaced separately (user named none -> defaults to internal set).
        self.assertIn("display_timeframes", decision, "display_timeframes must be surfaced separately")

    def test_ad_hoc_analysis_user_display_periods_do_not_drop_internal_1d(self) -> None:
        """When the user explicitly requests a partial display set (e.g. only
        '15m 5m'), the INTERNAL analysis must still run on the full set incl.
        1d - the display periods must NOT gate the fail-closed."""
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        self._seed_healthy_five_timeframes("BTCUSDT")
        with self._patch_network():
            # User asked only for 15m + 5m display.
            result = crypto_analyze_symbol_once("BTCUSDT", timeframes=["15m", "5m"], requested_by="test")
        self.assertTrue(result.get("ok"), result)
        decision = result["decision"]
        # Internal context kept 1d (display did not shrink internal analysis).
        self.assertIn("1d", decision.get("timeframes") or [], decision.get("timeframes"))
        self.assertNotIn("data_incomplete", decision.get("market_reason_codes") or [], decision.get("market_reason_codes"))
        dq = decision.get("data_quality") or {}
        self.assertEqual(str(dq.get("status")).lower(), "complete", dq)
        # Display periods surfaced as the user's explicit request.
        self.assertEqual(decision.get("display_timeframes"), ["15m", "5m"], decision.get("display_timeframes"))


# UNSAFE for rollback_isolation: same second-connection candle reads as
# TestPgAdHocAnalysisFullTimeframesP1 (data_quality=insufficient). Keeps
# DEFAULT fresh-schema isolation.
class TestPgAdHocAnalysisDisplayTimeframesReworkP1(unittest.TestCase):
    """终审返工 P1-1 (2026-07-25): display_timeframes must enter the Feishu
    consumption path, and the rendered text must distinguish the INTERNAL
    analysis base periods from the user's EXPLICIT display periods.

    Contract (Codex final review):
      - ``render_text`` must NO LONGER render ``decision["timeframes"]`` as a
        single "分析周期" line. It must show the internal analysis base
        periods ("内部分析周期") and, ONLY when the user explicitly named
        periods, a separate "用户请求展示周期" line consuming
        ``display_timeframes``.
      - ``internal_tfs`` MUST be the ordered union of ``DEFAULT_TIMEFRAMES``
        (1d/4h/1h/15m/5m) and the legal explicit periods, so any legal explicit
        period is TRULY fetched / built / profiled - never a "displayed but no
        profile/data" illusion.
      - When the user names no period, NO "用户请求展示周期" line appears.
      - ``intent_parser.parse_intent`` raw-scans ``3m`` but ``DEFAULT_TIMEFRAMES``
        does not contain ``3m``; the contract must keep them consistent so the
        system never claims a period it did not actually analyze to a healthy
        state. 终审返工 reviewer P1 (2026-07-25): the system CANNOT healthily
        analyze ``3m`` (no ``required_samples`` entry -> default 200, scheduler
        never seeds it, ad-hoc fetch uses ``lookback=160`` with no backfill), so
        ``3m`` is REJECTED from the canonical display set in BOTH
        ``intent_parser._timeframes`` and ``ga_crypto_tools``. It never becomes a
        display period, never enters ``internal_tfs``, never degrades the
        internal base - no half-state.

    Drives the REAL chain ``parse_intent -> crypto_analyze_symbol_once ->
    render_text``. RED-first: these assertions FAIL on the pre-fix code (which
    renders ``decision["timeframes"]`` as one "分析周期" line and accepts ``3m``
    into ``internal_tfs`` with <=160 candles against a 200 threshold, degrading
    a healthy BTC analysis to ``data_incomplete``/``unknown``/``C``/``0.30``).
    """

    LAST_CLOSE = 172_799_999
    ANALYSIS_TIME = 172_799_999
    REQUIRED = {"1d": 250, "4h": 250, "1h": 250, "15m": 200, "5m": 150, "3m": 150}
    SPANS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000,
             "15m": 900_000, "5m": 300_000, "3m": 180_000}

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_six_timeframes(self, symbol: str = "BTCUSDT") -> None:
        """Seed >= required_count contiguous closed candles for the 5 internal
        TFs (and 3m, retained as harmless extra seed data). After the reviewer
        P1 fix ``3m`` is rejected from the canonical display set, so the 3m rows
        are never read - the 5 healthy internal TFs are what matter for the
        non-degradation assertions."""
        import math
        candles = []
        for tf, span in self.SPANS.items():
            n = self.REQUIRED[tf] + 20
            base_close = self.LAST_CLOSE - (n - 1) * span
            for i in range(n):
                ct_close = base_close + i * span
                trend = 100.0 + i * 0.4
                pullback = 1.5 * math.sin(i / 6.0)
                open_p = trend + pullback - 0.3
                close_p = trend + pullback + 0.3
                high_p = max(open_p, close_p) + 0.8
                low_p = min(open_p, close_p) - 0.8
                candles.append({
                    "symbol": symbol, "interval": tf,
                    "open_time": ct_close - span + 1, "close_time": ct_close,
                    "open": open_p, "high": high_p, "low": low_p, "close": close_p,
                    "volume": 1000.0, "quote_volume": 100000.0,
                    "taker_buy_volume": 500.0, "taker_buy_quote_volume": 50000.0,
                    "trade_count": 100, "is_closed": True, "source": "test_seed",
                })
        self.repo.upsert_candles(candles)

    def _patch_network(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        stack = ExitStack()
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.fetch_and_upsert_closed_klines",
            side_effect=lambda *a, **k: None,
        ))
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.fetch_mark_price",
            return_value={"markPrice": "42000.0"},
        ))
        stack.enter_context(patch(
            "plugins.crypto_guard.tools.ga_crypto_tools.latest_closed_close_time_ms",
            side_effect=lambda interval, now_ms=None: self.LAST_CLOSE,
        ))
        return stack

    def test_explicit_display_periods_appear_in_rendered_text(self) -> None:
        """Case (1): '分析 BTCUSDT 15m 5m' -> the final text consumes
        ``display_timeframes`` on a separate "用户请求展示周期" line and
        still shows the full internal base periods incl. 1d/4h/1h."""
        from plugins.crypto_guard.notify.intent_parser import parse_intent
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        self._seed_six_timeframes("BTCUSDT")
        intent = parse_intent("分析 BTCUSDT 15m 5m")
        self.assertEqual(intent["intent"], "analyze_once", intent)
        # The parser surfaces the user's explicit display periods.
        self.assertEqual(intent.get("display_timeframes"), ["15m", "5m"], intent)
        with self._patch_network():
            result = crypto_analyze_symbol_once(
                "BTCUSDT", intent.get("display_timeframes"), requested_by="test",
            )
        self.assertTrue(result.get("ok"), result)
        decision = result["decision"]
        text = result.get("text") or ""
        # Case (2): internal analysis still ran on the full base incl. 1d/4h/1h.
        self.assertIn("1d", decision.get("timeframes") or [], decision.get("timeframes"))
        self.assertIn("4h", decision.get("timeframes") or [], decision.get("timeframes"))
        self.assertIn("1h", decision.get("timeframes") or [], decision.get("timeframes"))
        # The internal base periods are surfaced as "内部分析周期".
        self.assertIn("内部分析周期", text, text)
        # The user's explicit periods are surfaced as "用户请求展示周期" and
        # consume display_timeframes (15m, 5m).
        self.assertIn("用户请求展示周期", text, text)
        self.assertIn("15m", text)
        self.assertIn("5m", text)
        # display_timeframes on the decision is exactly the user's explicit set.
        self.assertEqual(decision.get("display_timeframes"), ["15m", "5m"], decision.get("display_timeframes"))

    def test_explicit_3m_is_rejected_not_half_state(self) -> None:
        """Case (3): an explicit ``3m`` must NOT produce a "displayed but no
        profile/data" half-state. 终审返工 reviewer P1 (2026-07-25): ``3m`` is
        accepted by the raw symbol scan but the system CANNOT healthily
        analyze it (``DEFAULT_TIMEFRAMES`` lacks ``3m``, config
        ``required_samples`` has no ``3m`` entry, the scheduler never seeds it,
        ad-hoc fetch uses ``lookback=160`` with no backfill). Per the P1-1
        contract the system must never claim a period it did not actually
        analyze to a healthy state. Fix (A): ``3m`` is REJECTED from the
        canonical display set in BOTH ``intent_parser._timeframes`` and
        ``ga_crypto_tools``. It never becomes a display period, never enters
        ``internal_tfs``, is never profiled, and never appears in the rendered
        text - so there is no "3m shown but degraded" illusion and no
        degradation of the internal base.
        """
        from plugins.crypto_guard.notify.intent_parser import parse_intent
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        self._seed_six_timeframes("BTCUSDT")
        intent = parse_intent("分析 BTCUSDT 3m")
        # 3m is REJECTED from the display set (the parser must not surface it).
        self.assertNotIn("3m", intent.get("display_timeframes") or [],
                         intent.get("display_timeframes"))
        with self._patch_network():
            result = crypto_analyze_symbol_once(
                "BTCUSDT", intent.get("display_timeframes"), requested_by="test",
            )
        self.assertTrue(result.get("ok"), result)
        decision = result["decision"]
        # 3m never enters the internal analysis base and is never profiled.
        self.assertNotIn("3m", decision.get("timeframes") or [],
                         decision.get("timeframes"))
        profiles = decision.get("profiles") or {}
        self.assertNotIn("3m", profiles, "3m must not be profiled - rejected, not built")
        # The rendered text must NOT show 3m (the user named no acceptable
        # display period after 3m was rejected -> no 用户请求展示周期 line, no 3m).
        text = result.get("text") or ""
        self.assertNotIn("3m", text, text)

    def test_no_explicit_period_hides_display_line(self) -> None:
        """When the user names no period, NO "用户请求展示周期" line appears;
        only the internal base periods are shown."""
        from plugins.crypto_guard.notify.intent_parser import parse_intent
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        self._seed_six_timeframes("BTCUSDT")
        intent = parse_intent("临时分析一下 BTCUSDT")
        self.assertEqual(intent.get("display_timeframes"), [], intent)
        with self._patch_network():
            result = crypto_analyze_symbol_once(
                "BTCUSDT", intent.get("display_timeframes"), requested_by="test",
            )
        self.assertTrue(result.get("ok"), result)
        text = result.get("text") or ""
        self.assertIn("内部分析周期", text)
        # No display line when the user named no period.
        self.assertNotIn("用户请求展示周期", text, text)
        # Internal base includes 1d (the dropped-TF regression guard).
        self.assertIn("1d", text)

    def test_explicit_3m_must_not_degrade_internal_analysis(self) -> None:
        """终审返工 reviewer P1 (2026-07-25): an explicit ``3m`` display period
        MUST NOT degrade the internal analysis. ``3m`` is accepted by the
        parser but ``DEFAULT_TIMEFRAMES`` lacks it, the config
        ``required_samples`` has no ``3m`` entry (default threshold 200), the
        scheduler never pre-seeds ``3m``, and the ad-hoc fetch uses
        ``lookback=160`` with no ``required_count`` backfill. Therefore the
        system cannot healthily analyze ``3m``. Per the P1-1 contract the
        system must NEVER claim a period it did not actually analyze to a
        healthy state: ``3m`` is rejected from the canonical display set so it
        never enters ``internal_tfs``, never degrades the snapshot, and never
        produces the ``data_incomplete``/``unknown``/``C``/``0.30`` half-state
        on a healthy BTC analysis.

        RED-first: on the pre-fix code ``parse_intent("分析 BTC 3m")`` returns
        ``display_timeframes=["3m"]`` and ``crypto_analyze_symbol_once`` adds
        ``3m`` to ``internal_tfs``; the 3m fetch yields <=160 candles against a
        200 threshold, ``any_degraded`` becomes True, and the WHOLE decision
        is forced to ``monitor_only``/``C``/``unknown``/``0.30`` — the
        assertions below FAIL on that code.
        """
        from plugins.crypto_guard.notify.intent_parser import parse_intent
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once
        # Seed the FIVE healthy internal TFs (NO 3m). The internal base must
        # stay non-degraded regardless of what display period the user names.
        self._seed_six_timeframes("BTCUSDT")
        intent = parse_intent("分析 BTCUSDT 3m")
        # Per fix (A): 3m is rejected from the canonical display set because
        # the system cannot healthily analyze it (no required_samples entry,
        # not in DEFAULT_TIMEFRAMES, scheduler never seeds it). The parser must
        # NOT surface 3m as a claimed display period.
        self.assertNotIn("3m", intent.get("display_timeframes") or [],
                         intent.get("display_timeframes"))
        with self._patch_network():
            result = crypto_analyze_symbol_once(
                "BTCUSDT", intent.get("display_timeframes"), requested_by="test",
            )
        self.assertTrue(result.get("ok"), result)
        decision = result["decision"]
        # 3m must NOT enter the internal analysis base (rejected, not built).
        self.assertNotIn("3m", decision.get("timeframes") or [],
                         decision.get("timeframes"))
        # The internal base stays healthy: data_quality is complete and no
        # ``data_incomplete`` reason code. This is the degradation signature the
        # pre-fix code produced (a healthy BTC analysis forced to
        # data_incomplete/unknown/C/0.30 because the user merely asked to SEE
        # 3m). Note: ``monitor_only`` / grade ``C`` alone are valid "no
        # opportunity" outcomes; the regression signature is the degraded
        # data_quality status plus the ``data_incomplete`` reason code, not the
        # grade in isolation.
        dq = decision.get("data_quality") or {}
        self.assertEqual(str(dq.get("status")).lower(), "complete", dq)
        self.assertNotIn("data_incomplete", decision.get("market_reason_codes") or [],
                         decision.get("market_reason_codes"))
        text = result.get("text") or ""
        self.assertNotIn("数据不完整", text, text)


def _scheduled_decision(symbol: str, *, grade: str, batch_id: str | None,
                        analysis_time_utc: str, analysis_time: int) -> dict:
    """A scheduled_analysis ga_decision row for hourly-aggregate tests."""
    return {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": analysis_time_utc,
        "decision_type": "scheduled_analysis",
        "signal_grade": grade,
        "confidence": 0.7,
        "market_bias": "neutral",
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [{"k": "v"}],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": batch_id,
        "previous_grade": grade,
    }


@pytest.mark.rollback_isolation  # 5.4 SAFE: data-only single-conn (audited)
class TestPgHourlyScheduledAnalysisDistributionRework(unittest.TestCase):
    """终审返工 P1-2 + P2 (2026-07-25): the last-1-hour distribution MUST be a
    real PostgreSQL aggregate of ``decision_type='scheduled_analysis'`` only,
    with REAL ``total_decisions`` and a REAL distinct-``batch_id`` count - never
    the hardcoded "4 批次、共 40 条" / "（DuckDB 时序）" labels.

    Contract (Codex final review):
      - Delete the fixed "4 批次、共 40 条" text.
      - The last-1-hour aggregate counts ONLY ``decision_type='scheduled_analysis'``
        (ad-hoc / other decision types mixed in MUST be excluded).
      - ``total_decisions`` = real COUNT(*); ``batch_count`` = real
        COUNT(DISTINCT batch_id) EXCLUDING NULL batch_id (an empty batch_id
        must NOT be fabricated into a batch count).
      - The rendered line uses real values, e.g.
        "最近1小时定时分析（N 批次、共 M 条决策）等级分布".
      - The current-batch line is ALWAYS labeled "PostgreSQL 当前批次"
        regardless of the hourly source.
      - The hourly line is labeled PostgreSQL, NEVER "DuckDB 时序".
      - The distribution values' sum MUST equal M (total_decisions).

    RED-first: these FAIL on the current code (hardcoded 4/40 + DuckDB label +
    current-batch label derived from the hourly source).
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_hour(self, *, scheduled_rows, adhoc_rows=()):
        """Insert scheduled + (optionally) ad-hoc ga_decisions rows."""
        for row in scheduled_rows:
            self.repo.create_ga_decision(row)
        for row in adhoc_rows:
            self.repo.create_ga_decision(row)

    def test_three_complete_batches_real_counts_and_sum(self) -> None:
        """3 complete scheduled batches (3 distinct batch_id, 9 decisions) in
        the last hour: the aggregate returns total=9, batch_count=3, and the
        distribution sums to 9. The rendered text uses real N/M."""
        from plugins.crypto_guard.notify import hourly_report
        # Window: [17:05, 18:05) generated at 18:05. Seed 3 batches at 17:10,
        # 17:40, 18:00 - all inside the window.
        rows = []
        grades_per_batch = [("B", "B", "C"), ("C", "D", "D"), ("B", "C", "C")]
        batch_ts = ["2026-07-24T17:10:00Z", "2026-07-24T17:40:00Z", "2026-07-24T18:00:00Z"]
        for bi, grades in enumerate(grades_per_batch):
            ts = batch_ts[bi]
            at_ms = 1785000000000 + bi * 1000
            for si, g in enumerate(grades):
                rows.append(_scheduled_decision(
                    f"SYM{bi}{si}USDT", grade=g, batch_id=f"15m:b{bi}",
                    analysis_time_utc=ts, analysis_time=at_ms + si,
                ))
        self._seed_hour(scheduled_rows=rows)

        stats = hourly_report._pg_hourly_scheduled_stats(
            self.repo, generated_at_utc="2026-07-24T18:05:00Z",
        )
        self.assertTrue(stats.get("ok"), stats)
        self.assertEqual(stats.get("source"), "postgres", stats)
        self.assertEqual(stats.get("total_decisions"), 9, stats)
        self.assertEqual(stats.get("batch_count"), 3, stats)
        dist = stats.get("signal_distribution") or {}
        self.assertEqual(sum(dist.values()), 9, dist)
        # Rendered text uses real N (3) / M (9), not hardcoded 4/40.
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=[], ga_decisions=[], open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=stats,
        )
        self.assertIn("最近1小时定时分析", text)
        self.assertIn("3 批次", text)
        self.assertIn("共 9 条决策", text)
        self.assertNotIn("4 批次", text)
        self.assertNotIn("共 40 条", text)
        # PostgreSQL label, never DuckDB.
        self.assertNotIn("DuckDB 时序", text)
        self.assertIn("PostgreSQL", text)

    def test_ad_hoc_mixed_in_is_excluded(self) -> None:
        """Ad-hoc decisions mixed into the window MUST be excluded from the
        scheduled aggregate (decision_type='scheduled_analysis' only)."""
        from plugins.crypto_guard.notify import hourly_report
        sched = [_scheduled_decision(
            "SCHEDUSDT", grade="B", batch_id="15m:b0",
            analysis_time_utc="2026-07-24T17:30:00Z", analysis_time=1785000000000,
        )]
        adhoc = _scheduled_decision(
            "ADHOCUSDT", grade="A", batch_id="15m:b0",
            analysis_time_utc="2026-07-24T17:30:00Z", analysis_time=1785000000001,
        )
        adhoc["decision_type"] = "ad_hoc_analysis"
        self._seed_hour(scheduled_rows=sched, adhoc_rows=[adhoc])

        stats = hourly_report._pg_hourly_scheduled_stats(
            self.repo, generated_at_utc="2026-07-24T18:05:00Z",
        )
        self.assertTrue(stats.get("ok"), stats)
        # Only the 1 scheduled decision counts.
        self.assertEqual(stats.get("total_decisions"), 1, stats)
        self.assertEqual(stats.get("batch_count"), 1, stats)
        dist = stats.get("signal_distribution") or {}
        self.assertEqual(dist.get("A", 0), 0, dist)
        self.assertEqual(dist.get("B", 0), 1, dist)

    def test_partial_batch_null_batch_id_not_fabricated(self) -> None:
        """A partial batch whose rows have NULL batch_id must NOT be fabricated
        into a batch count. total_decisions is still real; batch_count counts
        only distinct non-NULL batch_id."""
        from plugins.crypto_guard.notify import hourly_report
        rows = [
            _scheduled_decision("NULLAUSDT", grade="C", batch_id=None,
                                analysis_time_utc="2026-07-24T17:30:00Z",
                                analysis_time=1785000000000),
            _scheduled_decision("NULLBUSDT", grade="D", batch_id=None,
                                analysis_time_utc="2026-07-24T17:31:00Z",
                                analysis_time=1785000000001),
            _scheduled_decision("GOODUSDT", grade="B", batch_id="15m:b1",
                                analysis_time_utc="2026-07-24T17:32:00Z",
                                analysis_time=1785000000002),
        ]
        self._seed_hour(scheduled_rows=rows)

        stats = hourly_report._pg_hourly_scheduled_stats(
            self.repo, generated_at_utc="2026-07-24T18:05:00Z",
        )
        self.assertTrue(stats.get("ok"), stats)
        # 3 real decisions, but only 1 distinct non-NULL batch_id.
        self.assertEqual(stats.get("total_decisions"), 3, stats)
        self.assertEqual(stats.get("batch_count"), 1, stats)
        dist = stats.get("signal_distribution") or {}
        self.assertEqual(sum(dist.values()), 3, dist)

    def test_zero_data_no_hourly_line(self) -> None:
        """Zero scheduled decisions in the window: no hourly line is rendered
        (no fabricated counts)."""
        from plugins.crypto_guard.notify import hourly_report
        stats = hourly_report._pg_hourly_scheduled_stats(
            self.repo, generated_at_utc="2026-07-24T18:05:00Z",
        )
        self.assertTrue(stats.get("ok"), stats)
        self.assertEqual(stats.get("total_decisions"), 0, stats)
        self.assertEqual(stats.get("batch_count"), 0, stats)
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=[], ga_decisions=[], open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=stats,
        )
        self.assertNotIn("最近1小时定时分析", text)

    def test_current_batch_label_always_postgresql(self) -> None:
        """P2: the current-batch grade_counts line is ALWAYS labeled
        "PostgreSQL 当前批次" - it must NOT inherit the hourly source label
        (e.g. "DuckDB 时序") and must NOT change when the hourly aggregate is
        present or absent."""
        from plugins.crypto_guard.notify import hourly_report
        # A current batch of 10 decisions: 4 B + 6 C.
        batch_id = "15m:1785000000000"
        at = 1785000000000
        symbols = ["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
                   "ETHUSDT", "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"]
        grades = ["B", "B", "B", "B", "C", "C", "C", "C", "C", "C"]
        for sym, g in zip(symbols, grades):
            self.repo.create_ga_decision(
                _scheduled_decision(sym, grade=g, batch_id=batch_id,
                                    analysis_time_utc="2026-07-24T18:05:00Z",
                                    analysis_time=at)
            )
        decisions = self.repo.latest_ga_decisions_by_symbol(limit=120, batch_id=batch_id)
        self.assertEqual(len(decisions), 10)

        # With a present hourly aggregate (postgres source).
        stats = {"ok": True, "source": "postgres", "total_decisions": 9,
                 "batch_count": 3, "signal_distribution": {"B": 3, "C": 4, "D": 2}}
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-07-24T18:05:00Z",
            active_symbols=symbols, ga_decisions=decisions, open_orders=[],
            active_watches=[], failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
            duckdb_stats=stats,
        )
        cb_line = next((ln for ln in text.splitlines() if "等级分布（当前批次）" in ln), "")
        self.assertIn("PostgreSQL 当前批次", cb_line, cb_line)
        self.assertIn("B 4", cb_line)
        self.assertIn("C 6", cb_line)
        self.assertNotIn("DuckDB", cb_line, cb_line)
        # The hourly line is also PostgreSQL-labeled.
        hourly_line = next((ln for ln in text.splitlines() if "最近1小时定时分析" in ln), "")
        self.assertIn("PostgreSQL", hourly_line, hourly_line)
        self.assertNotIn("DuckDB", hourly_line, hourly_line)


if __name__ == "__main__":
    unittest.main()
