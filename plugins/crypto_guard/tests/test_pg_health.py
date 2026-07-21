"""P4 RED test: check_schema_health() on PostgreSQL.

Verifies the production schema-health probe:
1. On a FRESH (empty) schema, ``check_schema_health`` returns ``ok=False`` with
   the required tables/columns/indexes reported missing.
2. After ``initialize_database()``, ``check_schema_health`` returns ``ok=True``
   with an empty ``missing_columns`` list.
3. Dropping a required column flips ``ok=False`` and names that column.
4. Dropping a required index flips ``ok=False`` and names that index.
5. Revert-fail: neutralize both the focused constraint check and the complete
   catalog fingerprint -> a deliberately-broken schema reports ``ok=True``.
   Either production guard alone must catch the defect.

Uses per-test schema isolation against the dedicated ``crypto_guard_test`` DB.
"""

from __future__ import annotations

import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import (
    check_schema_health,
    initialize_database,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestPostgresSchemaHealth(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo(initialize_schema=False)

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    # ── tests ────────────────────────────────────────────────────────────────

    def test_fresh_empty_schema_is_not_healthy(self) -> None:
        """No schema applied -> ok=False with required objects missing."""
        result = check_schema_health()
        self.assertFalse(result["ok"], "empty schema reported healthy")
        # The required tables must be among the missing objects.
        missing_blob = " ".join(
            f"{m['table']}.{m['column']}" for m in result["missing_columns"]
        )
        self.assertIn("analysis_batches", missing_blob)
        self.assertIn("batch_symbol_status", missing_blob)
        self.assertIn("backfill_progress", missing_blob)
        self.assertIn("_analysis_attempt_counter", missing_blob)

    def test_healthy_after_initialize(self) -> None:
        """After initialize_database(), health is ok with no missing objects."""
        initialize_database()
        result = check_schema_health()
        self.assertTrue(
            result["ok"],
            f"schema not healthy after init: {result['missing_columns']}",
        )
        self.assertEqual(result["missing_columns"], [])

    def test_dropped_column_flips_unhealthy(self) -> None:
        """Dropping a required column -> ok=False naming that column."""
        initialize_database()
        # sanity: healthy before
        self.assertTrue(check_schema_health()["ok"])
        # Drop a required column that the health spec checks.
        self._exec("ALTER TABLE paper_orders DROP COLUMN last_processed_candle_time")
        result = check_schema_health()
        self.assertFalse(result["ok"], "missing column not detected")
        cols = [
            (m["table"], m["column"]) for m in result["missing_columns"]
        ]
        self.assertIn(
            ("paper_orders", "last_processed_candle_time"),
            cols,
            "dropped column not reported in missing_columns",
        )

    def test_dropped_index_flips_unhealthy(self) -> None:
        """Dropping a required index -> ok=False naming that index."""
        initialize_database()
        self.assertTrue(check_schema_health()["ok"])
        self._exec("DROP INDEX idx_paper_trade_logs_dedupe_key")
        result = check_schema_health()
        self.assertFalse(result["ok"], "missing index not detected")
        cols = [
            (m["table"], m["column"]) for m in result["missing_columns"]
        ]
        self.assertIn(
            ("(index)", "idx_paper_trade_logs_dedupe_key"),
            cols,
            "dropped index not reported in missing_columns",
        )

    def test_dropped_core_table_flips_unhealthy(self) -> None:
        """Every greenfield table is part of the health contract."""
        initialize_database()
        self.assertTrue(check_schema_health()["ok"])
        self._exec("DROP TABLE candles CASCADE")
        result = check_schema_health()
        self.assertFalse(result["ok"], "missing core table not detected")
        self.assertIn(
            ("candles", "(table)"),
            [(m["table"], m["column"]) for m in result["missing_columns"]],
        )

    def test_catalog_fingerprint_catches_unlisted_core_columns(self) -> None:
        """The complete catalog contract covers columns beyond legacy lists."""
        initialize_database()
        self._exec("ALTER TABLE candles DROP COLUMN close")
        result = check_schema_health()
        self.assertFalse(result["ok"])
        self.assertIn(
            ("(schema_contract)", "catalog fingerprint mismatch"),
            [(m["table"], m["column"]) for m in result["missing_columns"]],
        )

    def test_catalog_fingerprint_catches_same_name_wrong_index(self) -> None:
        """An index name alone is insufficient; its definition is contracted."""
        initialize_database()
        self._exec("DROP INDEX idx_paper_trade_logs_dedupe_key")
        self._exec(
            "CREATE UNIQUE INDEX idx_paper_trade_logs_dedupe_key "
            "ON paper_trade_logs(id)"
        )
        result = check_schema_health()
        self.assertFalse(result["ok"])
        self.assertIn(
            ("(schema_contract)", "catalog fingerprint mismatch"),
            [(m["table"], m["column"]) for m in result["missing_columns"]],
        )

    def test_health_accepts_external_conn(self) -> None:
        """check_schema_health(conn=...) runs on a caller-owned connection."""
        initialize_database()
        with pg_db.get_conn() as conn:
            result = check_schema_health(conn=conn)
        self.assertTrue(
            result["ok"],
            f"conn-mode health not ok: {result['missing_columns']}",
        )

    def test_health_resolves_non_public_scratch_schema(self) -> None:
        """check_schema_health must introspect the connection's actual schema,
        not a hard-coded ``public``.

        This is the P9 test-isolation contract: the smoke suite's ``make_repo()``
        fixture applies ``initialize_database()`` into a per-test scratch schema
        ``test_<uuid>`` (via ``search_path`` override) - NOT ``public``. If the
        health probe hard-codes ``WHERE table_schema='public'`` it sees nothing
        and reports every required object as missing, so ``initialize_database``
        raises ``RuntimeError: schema health check failed after init`` and the
        whole scratch-schema test path is dead. The probe must resolve the
        target schema from the connection (``current_schema()``) so it works
        both in production (``public``) and in isolated tests (``test_<uuid>``).
        """
        initialize_database()
        with pg_db.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT current_schema() AS s").fetchone()["s"],
                self._repo_handle.schema,
                "test setup did not route search_path at the scratch schema",
            )
            result = check_schema_health(conn=conn)
        self.assertTrue(
            result["ok"],
            f"health did not resolve the scratch schema: {result['missing_columns']}",
        )
        self.assertEqual(result["missing_columns"], [])

    def test_revert_fail_neutralized_checks_hide_defect(self) -> None:
        """Revert-fail: neutralize both schema-contract guards.

        The focused checker and full-catalog fingerprint are independent
        defenses. A missing CHECK can report healthy only when both are
        neutralized; restoring either one makes the broken schema unhealthy.

        The CHECK is an inline column constraint, so its name is auto-generated
        by PostgreSQL (``batch_symbol_status_status_check``); we look it up
        dynamically via pg_constraint rather than hard-coding the name.
        """
        from plugins.crypto_guard.storage import migrations as mig

        initialize_database()
        # sanity: healthy with the constraint present
        self.assertTrue(check_schema_health()["ok"])

        # Find the auto-generated CHECK constraint name on batch_symbol_status.
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.conname
                    FROM pg_constraint c
                    JOIN pg_class k ON c.conrelid = k.oid
                    JOIN pg_namespace n ON k.relnamespace = n.oid
                    WHERE n.nspname = current_schema()
                      AND k.relname = 'batch_symbol_status'
                      AND c.contype = 'c'
                    """
                )
                rows = cur.fetchall()
            conn.rollback()
        self.assertGreaterEqual(
            len(rows), 1, "no CHECK constraint found on batch_symbol_status"
        )
        conname = rows[0]["conname"]

        # Break the schema: drop the CHECK constraint on batch_symbol_status.
        self._exec(f"ALTER TABLE batch_symbol_status DROP CONSTRAINT {conname}")
        # With the real checker, the broken schema must be unhealthy.
        real = check_schema_health()
        self.assertFalse(
            real["ok"],
            "dropped CHECK constraint not detected (real checker missed it)",
        )

        original_check = mig._check_batch_symbol_status_constraint
        original_fingerprint = mig._schema_catalog_fingerprint
        mig._check_batch_symbol_status_constraint = lambda cur, schema: []  # type: ignore[assignment]
        mig._schema_catalog_fingerprint = (  # type: ignore[assignment]
            lambda cur, schema: mig._EXPECTED_SCHEMA_FINGERPRINT
        )
        try:
            neutralized = check_schema_health()
        finally:
            mig._check_batch_symbol_status_constraint = original_check  # type: ignore[assignment]
            mig._schema_catalog_fingerprint = original_fingerprint  # type: ignore[assignment]

        self.assertTrue(
            neutralized["ok"],
            "neutralized guards still caught the defect - revert-fail did "
            "not isolate the two schema-contract defenses",
        )


if __name__ == "__main__":
    unittest.main()
