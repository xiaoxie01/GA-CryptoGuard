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

import json
import os
import unittest

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

    # ── HR-4: report labels say PostgreSQL (no SQLite) ─────────────────────

    def test_report_labels_say_postgresql(self) -> None:
        """HR-4: the engine labels are PostgreSQL, not SQLite."""
        # _distribution_source_label: fallback label for in-memory/sqlite_fallback.
        self.assertEqual(
            hourly_report._distribution_source_label("in_memory_fallback", None),
            "PostgreSQL 实时等级统计（DuckDB 未启用）",
        )
        self.assertEqual(
            hourly_report._distribution_source_label("sqlite_fallback", None),
            "PostgreSQL 实时等级统计（DuckDB 未启用）",
        )
        # The default (unknown source) label is also PostgreSQL.
        self.assertEqual(
            hourly_report._distribution_source_label("", None),
            "PostgreSQL 实时等级统计（DuckDB 未启用）",
        )
        # DuckDB-present path unchanged.
        self.assertEqual(
            hourly_report._distribution_source_label("duckdb", {"ok": True}),
            "DuckDB 时序",
        )

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
            issue.get("type") == "diagnostic_query_failed"
            for issue in state.get("issues", [])
        ))
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


if __name__ == "__main__":
    unittest.main()
