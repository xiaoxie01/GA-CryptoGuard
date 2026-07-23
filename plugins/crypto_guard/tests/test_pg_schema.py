"""P2 RED test: greenfield schema_postgres.sql creates every table.

Verifies the schema DDL is valid PostgreSQL and creates all expected tables
when applied to a fresh schema. Uses per-test schema isolation so tests never
touch the production public schema.

This is a P0/P2 contract test (no business logic) - it only exercises the DDL
file and the information_schema introspection.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation]

import os
import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


# The full set of tables the greenfield schema must create. Every name here
# MUST appear in information_schema.tables after schema_postgres.sql runs.
# Sourced from storage/schema.sql + the added _analysis_attempt_counter.
EXPECTED_TABLES = {
    "symbols", "candles", "market_profiles", "market_snapshots",
    "module_analysis_results", "analysis_states", "skill_execution_logs",
    "skill_feedback_memory", "ga_decisions", "analysis_batches",
    "batch_symbol_status", "signals", "ad_hoc_analyses",
    "opportunity_watches", "paper_accounts", "paper_orders", "paper_trades",
    "paper_positions", "paper_trade_logs", "paper_equity_snapshots",
    "trade_reviews", "strategy_versions", "strategy_evaluations",
    "strategy_patches", "shadow_test_results", "historical_replay_results",
    "self_evolution_runs", "evolution_triggers", "strategy_memory",
    "daily_review_reports", "scheduler_runs", "agent_jobs", "task_locks",
    "feishu_events", "alert_outbox", "_migration_state",
    "_service_ownership", "_analysis_attempt_counter", "alert_failure_log",
    "config_hot_reload", "parquet_archive_runs", "runtime_config",
    "user_feedback", "sop_definitions", "shadow_virtual_trades",
    "backfill_progress",
}


SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage", "schema_postgres.sql",
)


def _read_schema_sql() -> str:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return f.read()


class TestPostgresGreenfieldSchema(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo(initialize_schema=False)

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _tables_in_db(self, conn) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            return {r["table_name"] for r in cur.fetchall()}

    def test_schema_applies_without_error(self) -> None:
        """Applying schema_postgres.sql in a transaction succeeds (valid DDL).

        PG DDL is transactional, so applying the full script inside a
        transaction and rolling back proves the DDL is syntactically valid and
        every statement succeeds - a bad statement raises before commit. No
        residue is left because we roll back.
        """
        schema_sql = _read_schema_sql()
        # Apply inside a transaction; on clean exit transaction() commits, then
        # we roll the changes back via a second transaction's DROP so the test
        # leaves no residue. Simpler: apply inside get_conn and rollback.
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.rollback()
        # Reaching here means every DDL statement executed without raising.
        with pg_db.get_conn() as conn:
            tables = self._tables_in_db(conn)
            conn.rollback()
        self.assertEqual(
            tables, set(), "rollback did not clear the applied schema",
        )

    def test_schema_creates_all_expected_tables_committed(self) -> None:
        """Committed-then-check variant: apply, commit, read tables.

        A separate test from the rollback variant so we can assert the FULL
        expected table set without the forced-rollback trick.
        """
        schema_sql = _read_schema_sql()
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            # commit happens on clean exit of transaction()
        # Re-open a fresh conn and verify every expected table exists.
        with pg_db.get_conn() as conn:
            tables = self._tables_in_db(conn)
            conn.rollback()
        missing = EXPECTED_TABLES - tables
        self.assertFalse(
            missing,
            f"schema_postgres.sql did not create these tables: {sorted(missing)}",
        )
        # And no extra unexpected tables (catches accidental extras).
        extra = tables - EXPECTED_TABLES
        self.assertFalse(
            extra,
            f"unexpected extra tables created: {sorted(extra)}",
        )

    def test_schema_is_idempotent(self) -> None:
        """Two consecutive applications leave the table set identical (IF NOT EXISTS).

        Idempotency is required so initialize_database() can re-run safely.
        """
        schema_sql = _read_schema_sql()
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        with pg_db.get_conn() as conn:
            tables = self._tables_in_db(conn)
            conn.rollback()
        missing = EXPECTED_TABLES - tables
        self.assertFalse(missing, f"second run dropped tables: {sorted(missing)}")

    def test_jsonb_columns_are_jsonb(self) -> None:
        """The *_json columns must be JSONB (not TEXT) so -> / ->> work.

        Spot-checks a representative set across the type-mapped columns.
        """
        schema_sql = _read_schema_sql()
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND column_name LIKE '%_json' "
                    "ORDER BY table_name, column_name"
                )
                rows = cur.fetchall()
            conn.rollback()
        # Every *_json column must be JSONB.
        non_jsonb = [
            (r["table_name"], r["column_name"], r["data_type"])
            for r in rows
            if r["data_type"].upper() != "JSONB"
        ]
        self.assertFalse(
            non_jsonb,
            f"these *_json columns are not JSONB: {non_jsonb}",
        )
        # And there must be a non-trivial number of them (sanity: the schema
        # has many JSON columns; 0 means the rename missed everything).
        self.assertGreater(len(rows), 20, f"too few json columns found: {len(rows)}")

    def test_boolean_columns_are_boolean(self) -> None:
        """The 0/1 SQLite boolean flags must be BOOLEAN in PG.

        Spot-checks the key flags named in the design.
        """
        schema_sql = _read_schema_sql()
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        expected_bool = {
            ("symbols", "enabled"),
            ("candles", "is_closed"),
            ("analysis_states", "opportunity_watch_recommended"),
            ("analysis_states", "paper_trade_allowed"),
            ("ad_hoc_analyses", "has_trade_plan"),
            ("paper_orders", "risk_check_passed"),
            ("strategy_evaluations", "is_shadow"),
            ("evolution_triggers", "evolution_allowed"),
            ("daily_review_reports", "pushed_to_feishu"),
            ("opportunity_watches", "created_by_user_action"),
            ("config_hot_reload", "confirmation_required"),
            ("config_hot_reload", "confirmed"),
            ("trade_reviews", "evolution_trigger_allowed"),
        }
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND data_type='boolean'"
                )
                actual_bool = {
                    (r["table_name"], r["column_name"]) for r in cur.fetchall()
                }
            conn.rollback()
        missing_bool = expected_bool - actual_bool
        self.assertFalse(
            missing_bool,
            f"these columns should be BOOLEAN but are not: {sorted(missing_bool)}",
        )

    def test_identity_columns_are_bigint_identity(self) -> None:
        """id columns are BIGINT ... GENERATED BY DEFAULT AS IDENTITY.

        Verifies via pg_attribute + pg_get_serial (identity sequence) that
        the id columns carry an identity sequence, not a plain BIGINT.
        """
        schema_sql = _read_schema_sql()
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                # attidentity <> '' means it is an identity column. Restrict to
                # ordinary tables (relkind='r') so the JOIN does not also pull in
                # index entries that share the same ``id`` column reference.
                cur.execute(
                    "SELECT a.attname, a.attidentity "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON a.attrelid = c.oid "
                    "JOIN pg_namespace n ON c.relnamespace = n.oid "
                    "WHERE n.nspname=current_schema() AND a.attname='id' "
                    "AND c.relkind = 'r' "
                    "AND a.attnum > 0 AND NOT a.attisdropped"
                )
                rows = cur.fetchall()
            conn.rollback()
        # Every id column must be an identity column (attidentity in 'ad').
        non_identity = [r["attname"] + ":" + str(r["attidentity"]) for r in rows if not r["attidentity"]]
        # attidentity is '' for plain columns, 'a' ALWAYS, 'd' BY DEFAULT.
        non_identity = [
            f"table_id_col:{r['attidentity'] or 'none'}"
            for r in rows
            if r["attidentity"] not in ("a", "d")
        ]
        self.assertFalse(
            non_identity,
            f"these id columns are not GENERATED AS IDENTITY: {non_identity}",
        )
        # Sanity: there must be many id columns (one per table with an id pk).
        self.assertGreater(len(rows), 35, f"too few id columns: {len(rows)}")


if __name__ == "__main__":
    unittest.main()
