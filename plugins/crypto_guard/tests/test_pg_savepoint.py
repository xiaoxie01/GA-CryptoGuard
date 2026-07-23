"""P5 RED test: psycopg savepoint must NOT roll back the outer transaction.

The user's P5 correction: ``conn.rollback()`` on a caught ``UniqueViolation``
would discard the caller's outer transaction. The fix is a nested savepoint
(``pg_db.savepoint``) so only the local failing statement is rolled back.

This test proves the contract on real PostgreSQL:
1. Inside an outer transaction, do a successful write, then a savepoint-guarded
   statement that raises a UNIQUE violation, recover, then do ANOTHER successful
   write on the SAME outer transaction. Commit. Both outer writes survive; the
   failed inner statement left no residue.
2. Revert-fail: replacing the savepoint with a bare ``conn.rollback()`` would
   discard the first outer write on the violation. We assert this by simulating
   the BAD pattern and confirming the first write is lost (proving the savepoint
   is what preserves it).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg

import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestPostgresSavepoint(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_savepoint_preserves_outer_transaction_on_violation(self) -> None:
        """A UNIQUE violation inside savepoint does not abort the outer txn.

        Outer write A_TEST -> savepoint-guarded duplicate INSERT of A_TEST that
        violates UNIQUE -> catch + recover -> outer write C_TEST on the SAME
        outer transaction -> commit. A_TEST and C_TEST both survive; no duplicate
        A_TEST row leaked (the violating insert was rolled back to the savepoint
        but the outer transaction stayed alive and committable).
        """
        from psycopg.errors import UniqueViolation

        with pg_db.get_conn() as conn:
            # Outer write A: a fresh symbol.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('A_TEST', 'A') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            # Savepoint-guarded duplicate of A_TEST -> MUST violate UNIQUE(symbol).
            # psycopg's conn.transaction() creates a SAVEPOINT; on the violation
            # it does ROLLBACK TO SAVEPOINT (local only) and re-raises, so we
            # catch UniqueViolation HERE. The outer transaction is still alive.
            try:
                with pg_db.savepoint(conn):
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO symbols(symbol, base_asset) "
                            "VALUES ('A_TEST', 'A')"
                        )
            except UniqueViolation:
                pass  # recovered locally; outer txn still usable
            # Outer write C: the outer transaction is STILL alive (not aborted).
            # A bare conn.rollback() above would have made this raise
            # "current transaction is aborted" or silently discarded A_TEST.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('C_TEST', 'C') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            conn.commit()

        # After commit: A_TEST and C_TEST both present; no duplicate A_TEST.
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM symbols "
                    "WHERE symbol IN ('A_TEST','C_TEST') ORDER BY symbol"
                )
                syms = [r["symbol"] for r in cur.fetchall()]
                cur.execute(
                    "SELECT COUNT(*) AS c FROM symbols WHERE symbol='A_TEST'"
                )
                dup = int(cur.fetchone()["c"])
            conn.rollback()
        self.assertEqual(syms, ["A_TEST", "C_TEST"])
        self.assertEqual(
            dup, 1, "duplicate A_TEST row leaked from the violating insert"
        )

    def test_savepoint_nested_two_levels_preserve_outer(self) -> None:
        """Two nested savepoints: the inner violation rolls back only itself.

        Outer write B_TEST -> outer savepoint S1 (clean) -> inner savepoint S2
        with a UNIQUE violation (caught) -> inner recovery -> S1 still usable ->
        outer write D_TEST -> commit. B_TEST, D_TEST survive. Proves nesting does
        not poison the outer transaction.
        """
        from psycopg.errors import UniqueViolation

        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('B_TEST', 'B') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            with pg_db.savepoint(conn):  # S1
                try:
                    with pg_db.savepoint(conn):  # S2 nested in S1
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO symbols(symbol, base_asset) "
                                "VALUES ('B_TEST', 'B')"
                            )
                except UniqueViolation:
                    pass
                # S1 is still alive after S2's local rollback.
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO symbols(symbol, base_asset) "
                        "VALUES ('D_TEST', 'D') "
                        "ON CONFLICT (symbol) DO NOTHING"
                    )
            conn.commit()

        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM symbols "
                    "WHERE symbol IN ('B_TEST','D_TEST') ORDER BY symbol"
                )
                syms = [r["symbol"] for r in cur.fetchall()]
            conn.rollback()
        self.assertEqual(syms, ["B_TEST", "D_TEST"])

    def test_revert_fail_bare_rollback_loses_outer_write(self) -> None:
        """Revert-fail: a bare ``conn.rollback()`` discards the outer write.

        Simulating the BAD pattern (rollback on violation) proves the outer
        write is lost -- which is exactly why we use savepoint instead. This
        asserts the savepoint is the mechanism that preserves the outer txn.
        """
        from psycopg.errors import UniqueViolation

        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO symbols(symbol, base_asset) VALUES ('E_TEST', 'E') "
                    "ON CONFLICT (symbol) DO NOTHING"
                )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO symbols(symbol, base_asset) "
                        "VALUES ('E_TEST', 'E')"
                    )
            except UniqueViolation:
                # BAD pattern: rollback the whole transaction.
                conn.rollback()
            # The rollback discarded E_TEST. Confirm the outer write is gone.
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol='E_TEST'")
                self.assertEqual(int(cur.fetchone()["c"]), 0)
            conn.commit()

        # And it is gone after the (now-empty) commit too.
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol='E_TEST'")
                self.assertEqual(int(cur.fetchone()["c"]), 0)
            conn.rollback()


if __name__ == "__main__":
    unittest.main()
