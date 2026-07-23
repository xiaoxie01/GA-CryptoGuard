"""Linchpin probe: does ``conn.transaction()`` self-persist + nest correctly?

The entire repository.py rewrite rests on this: every write method will wrap
its body in ``with self.conn.transaction():``. This must:
  1. PERSIST a write when there is NO outer transaction (caller didn't wrap)
     -> conn.transaction() does BEGIN+COMMIT, like SQLite autocommit.
  2. NEST as a SAVEPOINT when an outer transaction exists (caller wrapped)
     -> atomic sub-group, commits with the caller.
  3. ROLL BACK the local write on exception WITHOUT killing an outer txn.

If any of these fail, the whole "self-wrap in conn.transaction()" strategy
collapses and I must use a different commit model.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.concurrency]

import unittest

from psycopg.errors import UniqueViolation

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestConnTransactionLinchpin(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _count(self, symbol: str) -> int:
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol=%s", (symbol,))
                n = int(cur.fetchone()["c"])
            conn.rollback()
        return n

    def test_self_wrapped_write_persists_without_outer_txn(self) -> None:
        """L1: ``with conn.transaction(): execute(insert)`` persists on a bare conn.

        This is the SQLite-autocommit replacement: a repo method self-wraps and
        the write is durable on return, with NO caller transaction.
        """
        with pg_db.get_conn() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO symbols(symbol, base_asset) VALUES ('L1', 'L') "
                        "ON CONFLICT (symbol) DO NOTHING"
                    )
            # No explicit commit - conn.transaction() committed on clean exit.
            # Return conn to pool.
        self.assertEqual(self._count("L1"), 1, "self-wrapped write did not persist")

    def test_self_wrapped_write_nests_as_savepoint_in_outer_txn(self) -> None:
        """L2: nested conn.transaction() inside an outer txn = SAVEPOINT.

        Caller wraps in pg_db.transaction() (outer). Repo method self-wraps in
        conn.transaction() (inner = savepoint). The inner write commits with
        the outer; an unrelated write also in the outer commits too.
        """
        with pg_db.transaction() as conn:
            # Inner self-wrap (simulates a repo method): SAVEPOINT.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO symbols(symbol, base_asset) VALUES ('L2A', 'L') "
                        "ON CONFLICT (symbol) DO NOTHING"
                    )
            # Another write in the SAME outer txn.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('L2B', 'L') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
        # Outer committed -> both persist.
        self.assertEqual(self._count("L2A"), 1)
        self.assertEqual(self._count("L2B"), 1)

    def test_inner_exception_rolls_back_savepoint_not_outer(self) -> None:
        """L3: an exception in the inner conn.transaction() rolls back only it.

        Outer write L3A -> inner conn.transaction() does a duplicate insert that
        raises UniqueViolation -> inner savepoint rolls back -> BUT the inner
        context manager re-raises, so we catch OUTSIDE the inner with. The outer
        write L3A must survive (outer txn NOT aborted). Then outer write L3B
        succeeds and the whole outer commits.
        """
        # Seed L3A first so the inner duplicate collides.
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('L3A', 'L') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
        with pg_db.get_conn() as conn:
            # Outer-ish: we are NOT in an outer txn here (get_conn, autocommit=False,
            # first statement opens a txn). Simulate the seal shape: outer write,
            # then a self-wrapped inner that fails, then outer write, then commit.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('L3B', 'L') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            # Inner self-wrap that will fail (duplicate L3A) -> savepoint rollback.
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO symbols(symbol, base_asset) VALUES ('L3A', 'L')"
                        )
            except UniqueViolation:
                pass  # inner savepoint rolled back; outer txn still alive
            # Outer write after the recovered failure.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('L3C', 'L') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            conn.commit()
        self.assertEqual(self._count("L3A"), 1)
        self.assertEqual(self._count("L3B"), 1)
        self.assertEqual(self._count("L3C"), 1)


if __name__ == "__main__":
    unittest.main()
