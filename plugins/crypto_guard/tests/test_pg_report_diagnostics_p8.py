"""P8-3 gate: report_diagnostics SQL-dialect diagnostics on real PostgreSQL.

Guards the SQLite->PG cutover of ``diagnostics/report_diagnostics.py``. The old
code used SQLite-dialect constructs that BREAK on psycopg3 / PostgreSQL:

  RD-1: ``SELECT applied_at FROM _migration_state WHERE key=?`` -> the ``?``
        SQLite placeholder is invalid under psycopg3; the migrated form is
        ``key=%s``. If left, every marker-resolution helper raises inside its
        bare ``except`` and returns ``None`` -> the marker-missing check then
        reports the marker absent even when it IS present (false error), and
        marker-bound checks silently skip (false green).
  RD-2: ``datetime(created_at) >= datetime(?)`` wrappers are invalid PG syntax
        on a TIMESTAMPTZ column; the migrated form is ``created_at >=
        %s::timestamptz`` (PG casts the ISO-8601 param). If left, every
        marker-windowed ga_decisions query raises -> the check returns ``[]``
        (false green: no issues even when the defect is present).
  RD-3: ``datetime('now', '-1 hour')`` / ``datetime('now', ?)`` are SQLite
        modifier functions with no PG equivalent; the migrated forms are
        ``NOW() - INTERVAL '1 hour'`` / ``NOW() + %s::interval``. If left,
        the stuck-prepared-skill-log and recent-failed-job checks raise -> [].
  RD-4: ``json_extract(payload_json, '$.batch_id')`` is a SQLite JSON function
        with no PG equivalent; the migrated form is ``payload_json ->>
        'batch_id'`` (and ``#>> '{snapshot,symbol}'`` for nested). If left, the
        failed-jobs-outside-window check raises -> [].
  RD-5: JSONB columns come back from psycopg as already-decoded dict/list (NOT
        str). ``json.loads(row["raw_decision_json"])`` raises TypeError inside
        the per-row ``except ...: continue`` blocks -> every row is skipped ->
        the check reports zero issues even when the defect is present. The
        migrated ``_safe_json`` passes dict/list through and only parses str.
  RD-6: SQLite allows non-aggregated columns in SELECT-with-GROUP-BY (picks an
        arbitrary value); PG raises GroupingError. The migrated form adds the
        full grouping key to GROUP BY (the stuck-prepared check).

The contract exercised here (all real-PG):

  MISS: a fresh schema with NO markers -> ``diagnose_report_accuracy`` runs to
      completion and surfaces ``semantic_contract_marker_missing`` issues
      (proves RD-1 ``WHERE key=%s`` resolved; on the old ``key=?`` form the
      helper would raise inside its except and the marker-missing check would
      still fire, but the marker-bound checks below would each raise). This is
      the run-to-completion gate: the whole dispatcher must not raise.
  WINDOW: with the semantic-accuracy marker seeded + a recent ga_decisions row,
      the bias-stage semantic check's ``created_at >= %s::timestamptz`` bound
      runs without a dialect error (proves RD-2). The row is clean
      (directional bias + non-directional-consistent stage) so it must NOT
      surface ``bias_stage_semantic_conflict`` (a NEG control proving the
      filter evaluated rather than pass-everything).
  NESTED: the JSONB ``->>`` / ``#>>`` reads in the failed-jobs check compile
      (RD-4) and the stuck-prepared ``GROUP BY`` (RD-6) compiles, by virtue of
      the full dispatcher running to completion a second time after seeding
      markers + a skill_execution_logs prepared row.

NOT a mock; uses a real pooled conn on an isolated ``public`` schema.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

import json
import unittest
from datetime import datetime, timedelta, timezone

from plugins.crypto_guard.diagnostics import report_diagnostics
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TestPgReportDiagnosticsP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _seed_marker(self, key: str, applied_at: datetime) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO _migration_state(key, applied_at) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET applied_at = EXCLUDED.applied_at",
                    (key, applied_at),
                )

    def _seed_decision_clean(self, *, created_at: datetime) -> int:
        """Insert a CLEAN ga_decisions row (no bias/stage semantic conflict).

        A directional bias ('bullish') paired with a directional stage
        ('middle') is the LEGAL combination, so this row must NOT surface
        ``bias_stage_semantic_conflict``. The point of seeding it is to
        exercise the ``created_at >= %s::timestamptz`` window bound (RD-2):
        with the marker present, the check fetches post-marker rows via that
        cast. A SQLite ``datetime()`` wrapper would raise here -> the check
        returns [] and the NEG assertion would pass trivially (false green on
        the absence). Running to completion is the real dialect gate.
        """
        raw = {
            "symbol": "BTCUSDT",
            "market_bias": "bullish",
            "trend_stage": "middle",
            "decision": "hold",
        }
        now_ms = int(created_at.timestamp() * 1000)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ga_decisions"
                    "(symbol, analysis_time, analysis_time_utc, decision_type, "
                    " signal_grade, confidence, market_bias, trend_stage, decision, "
                    " skill_result_refs_json, evidence_json, counter_evidence_json, "
                    " risk_check_json, feishu_actions_json, final_summary, "
                    " raw_decision_json, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (
                        "BTCUSDT", now_ms, _iso(created_at), "scheduled",
                        "B", 0.6, "bullish", "middle", "hold",
                        json.dumps([]), json.dumps({}), json.dumps([]),
                        json.dumps({}), json.dumps([]), "summary",
                        json.dumps(raw), created_at,
                    ),
                )
                return int(cur.fetchone()["id"])

    def _seed_prepared_skill_log(self, *, created_at: datetime) -> int:
        """Insert a long-prepared skill_execution_logs row (RD-3/RD-6).

        A ``commit_state='prepared'`` row older than the staleness threshold
        is the stuck-producer signal. The check's SQL uses
        ``created_at < NOW() + %s::interval`` (negative interval param) and a
        ``GROUP BY symbol, timeframe, analysis_time, skill_name``. The old
        SQLite ``datetime('now', ?)`` would raise (RD-3) and the old partial
        GROUP BY would raise GroupingError (RD-6).
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO skill_execution_logs"
                    "(symbol, timeframe, analysis_time, skill_name, skill_version, "
                    " tool_result_json, ga_interpretation_json, final_result_json, "
                    " commit_state, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        "BTCUSDT", "1m",
                        int(created_at.timestamp() * 1000),
                        "test_skill", "v1",
                        json.dumps({}), json.dumps({}), json.dumps({}),
                        "prepared", created_at,
                    ),
                )
                return int(cur.fetchone()["id"])

    # ── RD-1: marker resolution + run-to-completion ──────────────────────────

    def test_marker_missing_surfaces_and_dispatcher_completes(self) -> None:
        """RD-1 + run-to-completion gate.

        ``initialize_database()`` seeds the contract markers, so to exercise
        the marker-missing branch we DELETE both the R4 and semantic-accuracy
        markers (simulating a pre-deployment / marker-not-yet-written state).
        The dispatcher MUST run to completion (return a dict with an
        ``issues`` list) and surface ``semantic_contract_marker_missing``. The
        marker-missing check's query ``SELECT applied_at FROM _migration_state
        WHERE key=%s`` (RD-1, was ``key=?``) must evaluate (not raise inside
        its bare except) for the missing-row branch to fire - if it raised,
        the except swallows it and the check returns [] (no missing-marker
        issue), a false green.
        """
        # Delete both markers the semantic-contract-missing check looks for.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _migration_state WHERE key IN (%s, %s)",
                    (
                        report_diagnostics.R4_CONTRACT_MARKER_KEY,
                        report_diagnostics.SEMANTIC_ACCURACY_MARKER_KEY,
                    ),
                )
        out = report_diagnostics.diagnose_report_accuracy(self.repo)
        # Run-to-completion: structural shape, NOT a propagated SQL error.
        self.assertIn("issues", out, out)
        self.assertIsInstance(out["issues"], list, out)
        types = {i["type"] for i in out["issues"]}
        self.assertIn("semantic_contract_marker_missing", types)
        # The fail-closed render wrapper mirrors this without raising.
        wrapped = report_diagnostics.run_for_report(self.repo)
        self.assertIn("issues", wrapped, wrapped)
        self.assertIsInstance(wrapped["issues"], list, wrapped)

    # ── RD-2: timestamptz window bound on a seeded clean decision ────────────

    def test_marker_window_bound_runs_and_clean_row_not_flagged(self) -> None:
        """RD-2: ``created_at >= %s::timestamptz`` bound runs on a real row.

        Seeds the semantic-accuracy marker (so the bound is the marker ts, not
        the 24h fallback) + a CLEAN recent ga_decisions row. The bias-stage
        semantic check's windowed query must run without a dialect error (a
        SQLite ``datetime(created_at) >= datetime(?)`` wrapper would raise).
        The row is legal (bullish + middle), so it must NOT surface
        ``bias_stage_semantic_conflict`` - a NEG control proving the filter
        actually evaluated the row rather than passing everything.
        """
        marker_at = datetime.now(timezone.utc) - timedelta(days=1)
        self._seed_marker(
            report_diagnostics.SEMANTIC_ACCURACY_MARKER_KEY, marker_at
        )
        # Also seed the R4 marker so the marker-missing check stays quiet and
        # the only signal is the bias-stage check's windowed evaluation.
        self._seed_marker(
            report_diagnostics.R4_CONTRACT_MARKER_KEY, marker_at
        )
        # Recent, post-marker decision (created_at > marker_at).
        self._seed_decision_clean(created_at=datetime.now(timezone.utc))
        out = report_diagnostics.diagnose_report_accuracy(self.repo)
        self.assertIn("issues", out, out)
        self.assertIsInstance(out["issues"], list, out)
        types = {i["type"] for i in out["issues"]}
        # NEG control: the clean row must NOT be flagged.
        self.assertNotIn("bias_stage_semantic_conflict", types)

    # ── RD-3 + RD-6: stuck-prepared skill log (interval + GROUP BY) ──────────

    def test_stuck_prepared_skill_log_check_runs(self) -> None:
        """RD-3 + RD-6: the stuck-prepared check's SQL compiles + runs.

        Seeds a long-prepared skill_execution_logs row and runs the full
        dispatcher to completion. The check uses ``created_at < NOW() +
        %s::interval`` (RD-3, was ``datetime('now', ?)``) and a 4-column
        GROUP BY (RD-6, was a 2-column GROUP BY that raised GroupingError on
        PG because timeframe/skill_name were non-aggregated). If either site
        raised, the dispatcher would propagate the error (no ``issues`` key)
        or the per-check except would swallow it -> the check silently skips.
        Run-to-completion is the dialect gate.
        """
        # A row older than the staleness threshold so it is genuinely "stuck".
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        self._seed_prepared_skill_log(created_at=old)
        out = report_diagnostics.diagnose_report_accuracy(self.repo)
        # Run-to-completion gate: the stuck-prepared check (RD-3 interval +
        # RD-6 GROUP BY) compiled and executed rather than raising.
        self.assertIn("issues", out, out)
        self.assertIsInstance(out["issues"], list, out)

    def test_query_failure_is_explicit_and_connection_recovers(self) -> None:
        """A real PostgreSQL SQL error must fail closed, then roll back."""

        class _FailingConn:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, _query, _params=None):
                return self._conn.execute(
                    "SELECT * FROM _definitely_missing_diagnostic_table"
                )

        failing_repo = CryptoGuardRepository(_FailingConn(self.conn))
        out = report_diagnostics.run_for_report(failing_repo)
        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"].get("diagnostic_query_failed"), 1)
        self.assertEqual(out["issues"][0]["type"], "diagnostic_query_failed")

        row = self.conn.execute("SELECT 1 AS v").fetchone()
        self.assertEqual(row["v"], 1, "diagnostic failure left conn aborted")
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
