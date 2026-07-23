"""P8-3 gate: feedback_ttl TTL/decay transitions on real PostgreSQL.

Guards the SQLite->PG cutover of ``diagnostics/feedback_ttl.py``. The old code
used SQLite-dialect constructs that BREAK on psycopg3 / PostgreSQL:

  FT-1: ``datetime(created_at) < datetime(?)`` wrappers are invalid PG syntax
        on a TIMESTAMPTZ column; the migrated form compares ``created_at < %s``
        directly (PG casts the ISO-8601 param). If left, every TTL UPDATE raises
        a syntax error -> ``apply_feedback_ttl`` returns ``ok:False`` / no rows
        transition.
  FT-2: ``id NOT IN (SELECT value FROM json_each(?))`` is a SQLite JSON-table
        function with no PG equivalent as written. The migrated form uses a PG
        array ``NOT (id = ANY(%s::bigint[]))``. If left, the protected-feedback
        exclusion raises -> protected feedback gets archived (data loss).
  FT-3: ``LIMIT ?`` placeholder style -> ``LIMIT %s`` (psycopg ``%s``).
  FT-4: ``repo.conn.commit()`` on a pooled autocommit=False conn inside the
        helper must be replaced by a single ``with repo.conn.transaction():``
        wrapping the three UPDATEs (atomic TTL transition). A bare ``commit()``
        on a conn already in a caller's transaction would error / mis-scope.

The contract: a fresh feedback (35 days old) -> decayed; a decayed feedback
(100 days old) -> archived; a fresh feedback referenced by an active
strategy_patch (protected) -> NOT archived even at 100 days; ``get_feedback_
with_ttl_weight`` returns ttl_weight per status.

NOT a mock; uses a real pooled conn on an isolated schema.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from plugins.crypto_guard.diagnostics import feedback_ttl
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


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class TestPgFeedbackTtlP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_feedback(self, *, skill: str, status: str, days_ago: int) -> int:
        """Insert a skill_feedback_memory row at a fixed age; return its id."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO skill_feedback_memory"
                    "(skill_name, skill_version, feedback_type, source_type, finding, "
                    " pattern_type, status, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (skill, "v1", "negative", "test", "finding", "p_" + skill,
                     status, _days_ago_iso(days_ago)),
                )
                return int(cur.fetchone()["id"])

    def _seed_protecting_patch(self, feedback_id: int) -> None:
        """Insert an active strategy_patch that references feedback_id (protects it)."""
        patch = {
            "feedback_ids": [feedback_id],
            "change": {"scope": "test"},
        }
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategy_patches"
                    "(strategy_name, from_version, candidate_version, patch_json, "
                    " evidence_json, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    ("probe_strat", "v1", "v2", json.dumps(patch),
                     json.dumps({"feedback_id": feedback_id}), "active"),
                )

    def _status_of(self, feedback_id: int) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM skill_feedback_memory WHERE id=%s",
                (feedback_id,),
            )
            row = cur.fetchone()
        return row["status"] if row else None

    # ── FT-1 + FT-2: age-based transitions + protected exclusion ───────────

    def test_apply_ttl_transitions_and_protects(self) -> None:
        """FT-1/FT-2: fresh->decayed (35d), decayed->archived (100d), protected stays.

        FT-2 is a GENUINE A/B test (not a false green): two ``decayed`` rows at
        100d, identical except one is protected by an active strategy_patch. The
        unprotected one MUST archive (decayed_to_archived path); the protected
        one MUST stay ``decayed``. A protected ``fresh``+100d row would NOT be
        archived by any of the three UPDATEs anyway (fresh_to_decayed only
        matches the 30-90d window; the other two never touch ``fresh``), so it
        would pass trivially without exercising the ANY() exclusion - which is
        exactly the false-green the standing directive forbids.
        """
        # 35 days old, status fresh -> should become decayed (within 30-90d window).
        fresh_id = self._seed_feedback(skill="fresh", status="fresh", days_ago=35)
        # 100 days old, status decayed, UNPROTECTED -> should become archived (>90d).
        decayed_id = self._seed_feedback(skill="decayed", status="decayed", days_ago=100)
        # 100 days old, status decayed, BUT protected by an active patch -> stays
        # decayed (NOT archived). Without the json_each->ANY() exclusion this row
        # would archive together with decayed_id - proving the protection works.
        protected_id = self._seed_feedback(skill="prot", status="decayed", days_ago=100)
        self._seed_protecting_patch(protected_id)
        # A genuinely fresh (5 days) row stays fresh.
        recent_id = self._seed_feedback(skill="recent", status="fresh", days_ago=5)

        out = feedback_ttl.apply_feedback_ttl(self.repo)
        self.assertTrue(out.get("ok"), out)
        # FT-1: the age-based transitions fired.
        self.assertEqual(out["transitions"]["fresh_to_decayed"], 1, out)
        # fresh (35d) transitioned; recent (5d) did NOT.
        self.assertEqual(self._status_of(fresh_id), "decayed")
        self.assertEqual(self._status_of(recent_id), "fresh")
        # decayed (100d, unprotected) -> archived.
        self.assertEqual(self._status_of(decayed_id), "archived")
        # FT-2: protected decayed (100d, would-be archived) is NOT archived.
        self.assertEqual(self._status_of(protected_id), "decayed")
        self.assertGreaterEqual(out["transitions"]["protected"], 1, out)
        # Exactly one decayed->archived transition (the unprotected one only).
        self.assertEqual(out["transitions"]["decayed_to_archived"], 1, out)

    # ── FT-3: get_feedback_with_ttl_weight LIMIT %s + ttl_weight ───────────

    def test_get_feedback_with_ttl_weight(self) -> None:
        """FT-3: LIMIT %s works; ttl_weight mapped per status; archived excluded by default."""
        self._seed_feedback(skill="f", status="fresh", days_ago=5)
        self._seed_feedback(skill="d", status="decayed", days_ago=40)
        arch_id = self._seed_feedback(skill="a", status="archived", days_ago=200)
        rows = feedback_ttl.get_feedback_with_ttl_weight(self.repo, limit=100)
        self.assertGreaterEqual(len(rows), 2)  # fresh + decayed (archived excluded)
        statuses = {r["status"] for r in rows}
        self.assertNotIn("archived", statuses)  # default excludes archived
        weights = {r["status"]: r["ttl_weight"] for r in rows}
        self.assertEqual(weights.get("fresh"), 1.0)
        self.assertEqual(weights.get("decayed"), 0.5)
        # include_archived=True surfaces the archived row with weight 0.0.
        rows_all = feedback_ttl.get_feedback_with_ttl_weight(self.repo, limit=100, include_archived=True)
        arch_row = next((r for r in rows_all if r["id"] == arch_id), None)
        self.assertIsNotNone(arch_row)
        self.assertEqual(arch_row["ttl_weight"], 0.0)

    def test_get_feedback_with_ttl_weight_limit(self) -> None:
        """FT-3: LIMIT %s bounds the result set."""
        for i in range(5):
            self._seed_feedback(skill=f"lim{i}", status="fresh", days_ago=1)
        rows = feedback_ttl.get_feedback_with_ttl_weight(self.repo, limit=2)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
