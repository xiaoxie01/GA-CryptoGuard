"""Empirical probe: psycopg3 transaction semantics on a pooled connection.

This is NOT a behavioral test of the app. It exists to settle the ONE
architectural question that gates the entire repository.py rewrite:

  SQLite used ``isolation_level=None`` (autocommit) - every ``execute``
  auto-persisted. repository.py's ~100 write methods rely on that.
  The psycopg pool defaults to ``autocommit=False``. So what actually
  happens to an uncommitted write on a pooled connection?

We answer four questions empirically on real PostgreSQL:
  1. Does an uncommitted INSERT persist after the conn is returned to the
     pool WITHOUT an explicit commit?  (Determines whether the rewrite must
     add explicit commits everywhere.)
  2. Does an uncommitted INSERT persist after an explicit ``conn.commit()``?
     (Sanity - the expected happy path.)
  3. When two pooled connections each hold an open (uncommitted) transaction,
     can one read the other's uncommitted writes?  (Determines isolation
     assumptions for the concurrency claim path.)
  4. Does ``pg_db.savepoint(conn)`` recover a UNIQUE violation while leaving
     the outer transaction committable, on a REAL repo-style call sequence?
"""

from __future__ import annotations

import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestPsycopgTransactionModel(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _count(self, symbol: str) -> int:
        """Count rows on a FRESH pooled connection (cannot see others' uncommitted)."""
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol=%s", (symbol,))
                n = int(cur.fetchone()["c"])
            conn.rollback()  # read-only; discard the implicit txn
        return n

    def test_clean_get_conn_scope_commits_pending_write(self) -> None:
        """Q1: a clean ``get_conn`` scope commits its pending transaction.

        Production has read-then-write paths whose write is performed by a
        nested repository transaction after an earlier SELECT opened the outer
        transaction. The connection boundary must commit that clean outer unit
        of work instead of silently rolling it back on pool return.
        """
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('Q1_NOCOMMIT', 'Q')"
                )
            # No explicit commit: clean get_conn exit owns the unit-of-work
            # boundary and commits before pool return.
        self.assertEqual(
            self._count("Q1_NOCOMMIT"),
            1,
            "clean get_conn exit silently lost a pending write",
        )

    def test_committed_write_persists(self) -> None:
        """Q2: an INSERT followed by conn.commit() persists across connections."""
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('Q2_COMMIT', 'Q')"
                )
            conn.commit()
        self.assertEqual(self._count("Q2_COMMIT"), 1)

    def test_concurrent_uncommitted_writes_are_isolated(self) -> None:
        """Q3: two open transactions cannot see each other's uncommitted writes.

        This confirms READ COMMITTED isolation: conn B (own transaction) does
        NOT see conn A's uncommitted INSERT. The concurrency claim path
        (claim_next_job / claim_next_batch) must rely on FOR UPDATE SKIP LOCKED
        over COMMITTED rows, never on cross-transaction visibility of uncommitted
        data.
        """
        with pg_db.get_conn() as conn_a:
            with conn_a.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('Q3_A_ONLY', 'Q')"
                )
            # conn_a's transaction is OPEN and uncommitted.
            with pg_db.get_conn() as conn_b:
                with conn_b.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM symbols WHERE symbol='Q3_A_ONLY'"
                    )
                    seen_by_b = int(cur.fetchone()["c"])
                conn_b.rollback()
            self.assertEqual(
                seen_by_b,
                0,
                "conn_b saw conn_a's uncommitted write - isolation broken",
            )
            conn_a.commit()  # now it's visible
        self.assertEqual(self._count("Q3_A_ONLY"), 1)

    def test_savepoint_recovers_violation_and_outer_commits(self) -> None:
        """Q4: savepoint recovers a UNIQUE violation; outer txn still commits.

        Mirrors the repo's IntegrityError catch-and-recover pattern: write A,
        a savepoint-guarded statement that collides (caught), then write B, all
        on ONE outer transaction that commits. A and B survive; the collision
        left no residue. This is the shape the 5 IntegrityError sites must take.
        """
        from psycopg.errors import UniqueViolation

        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('Q4_A', 'Q') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            try:
                with pg_db.savepoint(conn):
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO symbols(symbol, base_asset) VALUES ('Q4_A', 'Q')"
                        )
            except UniqueViolation:
                pass
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('Q4_B', 'Q') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            conn.commit()
        self.assertEqual(self._count("Q4_A"), 1)
        self.assertEqual(self._count("Q4_B"), 1)


if __name__ == "__main__":
    unittest.main()
