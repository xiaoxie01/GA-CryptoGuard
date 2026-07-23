"""Behavioral contracts for the fast reusable PostgreSQL test fixture."""

from __future__ import annotations

import unittest

import pytest

from plugins.crypto_guard.tests.pg_fixtures import (
    make_reusable_repo,
    reset_reusable_schema_manager,
)


pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation]


class ReusablePostgresFixtureTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_reusable_schema_manager()

    def test_rows_sequences_and_markers_are_reset(self) -> None:
        first = make_reusable_repo()
        schema = first.schema
        try:
            schemas = first.conn.execute(
                "SELECT current_schemas(false) AS schemas"
            ).fetchone()["schemas"]
            self.assertEqual(schemas, [schema])
            first.conn.execute(
                "INSERT INTO agent_jobs(job_type, source, session_id, payload_json, status, priority) "
                "VALUES ('fixture_probe', 'fixture_test', 'fixture:one', "
                "'{}'::jsonb, 'pending', 1)"
            )
            first.conn.commit()
            row = first.conn.execute(
                "SELECT id FROM agent_jobs WHERE job_type='fixture_probe'"
            ).fetchone()
            self.assertEqual(int(row["id"]), 1)
            baseline_markers = {
                row["key"]: row["applied_at"]
                for row in first.conn.execute(
                    "SELECT key, applied_at FROM _migration_state"
                ).fetchall()
            }
            self.assertGreater(len(baseline_markers), 0)
            removed_marker = sorted(baseline_markers)[0]
            first.conn.execute(
                "DELETE FROM _migration_state WHERE key=%s", (removed_marker,)
            )
            first.conn.execute(
                "INSERT INTO _migration_state(key, applied_at) "
                "VALUES ('fixture_rogue_marker', NOW())"
            )
            first.conn.execute("SET application_name='crypto_guard_fixture_poison'")
            first.conn.commit()
        finally:
            first.close()

        second = make_reusable_repo()
        try:
            self.assertEqual(second.schema, schema)
            self.assertIsNone(
                second.conn.execute(
                    "SELECT id FROM agent_jobs WHERE job_type='fixture_probe'"
                ).fetchone()
            )
            row = second.conn.execute(
                "INSERT INTO agent_jobs(job_type, source, session_id, payload_json, status, priority) "
                "VALUES ('fixture_probe_2', 'fixture_test', 'fixture:two', "
                "'{}'::jsonb, 'pending', 1) "
                "RETURNING id"
            ).fetchone()
            self.assertEqual(int(row["id"]), 1)
            restored_markers = {
                row["key"]: row["applied_at"]
                for row in second.conn.execute(
                    "SELECT key, applied_at FROM _migration_state"
                ).fetchall()
            }
            self.assertEqual(restored_markers, baseline_markers)
            application_name = second.conn.execute(
                "SHOW application_name"
            ).fetchone()["application_name"]
            self.assertNotEqual(application_name, "crypto_guard_fixture_poison")
        finally:
            second.close()

    def test_uncommitted_transaction_cannot_leak(self) -> None:
        first = make_reusable_repo()
        try:
            first.conn.execute(
                "INSERT INTO agent_jobs(job_type, source, session_id, payload_json, status, priority) "
                "VALUES ('uncommitted_probe', 'fixture_test', 'fixture:uncommitted', "
                "'{}'::jsonb, 'pending', 1)"
            )
        finally:
            first.close()

        second = make_reusable_repo()
        try:
            self.assertIsNone(
                second.conn.execute(
                    "SELECT id FROM agent_jobs WHERE job_type='uncommitted_probe'"
                ).fetchone()
            )
        finally:
            second.close()

    def test_schema_drift_fails_closed_and_retires_schema(self) -> None:
        first = make_reusable_repo()
        poisoned_schema = first.schema
        try:
            first.conn.execute("ALTER TABLE agent_jobs DROP COLUMN priority")
            first.conn.commit()
        finally:
            first.close()

        with self.assertRaisesRegex(RuntimeError, "schema drifted"):
            make_reusable_repo()

        replacement = make_reusable_repo()
        try:
            self.assertNotEqual(replacement.schema, poisoned_schema)
            cols = replacement.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='agent_jobs' "
                "AND column_name='priority'",
                (replacement.schema,),
            ).fetchall()
            self.assertEqual(len(cols), 1)
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
