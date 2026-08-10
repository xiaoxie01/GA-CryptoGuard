"""P0 RED tests: PostgreSQL connection layer (pg_db.py).

Verifies the driver/connection contract before any business code migrates:
- a valid DSN yields a dict-row pooled connection that runs SELECT 1;
- a missing DSN fails closed (CryptoGuardDBUnavailable, NO SQLite fallback);
- a bad DSN (unreachable port) fails closed;
- the transaction() helper commits on success and rolls back on exception;
- the pool reliably returns connections across many uses.

These connect to the dedicated ``crypto_guard_test`` DB as
``crypto_guard_test_app`` (never the postgres superuser). The admin password is
used only by ``_pg_bootstrap`` to create the role/DB; it never appears here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import direct_conn, make_repo, scratch_schema


class TestPgConnectionLayer(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch_cm = scratch_schema(initialize_schema=False)
        self.schema = self._scratch_cm.__enter__()

    def tearDown(self) -> None:
        self._scratch_cm.__exit__(None, None, None)

    def test_valid_dsn_connects_and_returns_dict_row(self) -> None:
        health = pg_db.check_connection()
        self.assertTrue(health["ok"], f"expected ok connection, got {health}")
        self.assertEqual(health["engine"], "postgresql")
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS v")
                row = cur.fetchone()
            conn.rollback()
        self.assertIsInstance(row, dict)
        self.assertEqual(row["v"], 1)

    def test_missing_dsn_fails_closed_no_sqlite_fallback(self) -> None:
        pg_db.reset_pool()
        os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
        with self.assertRaises(pg_db.CryptoGuardDBUnavailable):
            pg_db.get_pool()
        # The fail-closed gate never reaches SQLite: resolve_dsn itself raises
        # a RuntimeError naming the DSN, not a SQLite path.
        from plugins.crypto_guard.config.loader import resolve_database_url

        with self.assertRaises(RuntimeError) as cm:
            resolve_database_url()
        self.assertIn("CRYPTO_GUARD_DATABASE_URL", str(cm.exception))
        self.assertIn("SQLite", str(cm.exception))

    def test_bad_port_fails_closed(self) -> None:
        # 5.2 bounded connect: ``?connect_timeout=1`` (libpq) fails each attempt
        # fast, and get_pool()'s eager pool open is bounded, so a dead port must
        # fail closed in well under a few seconds - never block on the endpoint.
        # Password redacted; port is the fault.
        # 终审返工 P1 (08-10): this test sets its OWN explicit 3s open bound and
        # precisely restores the original environment afterwards. It must NOT
        # rely on a global 3s test default - production connection posture is 30s.
        saved_timeout = os.environ.get("CRYPTO_GUARD_POOL_OPEN_TIMEOUT")
        saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
        os.environ["CRYPTO_GUARD_POOL_OPEN_TIMEOUT"] = "3.0"
        try:
            os.environ["CRYPTO_GUARD_DATABASE_URL"] = (
                "postgresql://crypto_guard_test_app:wrong@localhost:1/crypto_guard_test"
                "?connect_timeout=1"
            )
            pg_db.reset_pool()
            t0 = time.monotonic()
            with self.assertRaises(pg_db.CryptoGuardDBUnavailable):
                pg_db.get_pool()
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            if saved_timeout is None:
                os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)
            else:
                os.environ["CRYPTO_GUARD_POOL_OPEN_TIMEOUT"] = saved_timeout
            if saved_url is None:
                os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
            else:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved_url
            pg_db.reset_pool()

    def test_pool_open_timeout_default_is_30(self) -> None:
        # 终审返工 P1 (08-10): the DEFAULT open bound is the production 30s
        # posture when the env var is absent - never a test-driven 3s.
        os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)
        try:
            self.assertEqual(pg_db._pool_open_timeout(), 30.0)
        finally:
            os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)

    def test_pool_open_timeout_explicit_3_override(self) -> None:
        # 终审返工 P1 (08-10): an EXPLICIT 3s override is honored exactly - the
        # fail-fast bound stays available for tests that deliberately use it.
        os.environ["CRYPTO_GUARD_POOL_OPEN_TIMEOUT"] = "3.0"
        try:
            self.assertEqual(pg_db._pool_open_timeout(), 3.0)
        finally:
            os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)

    def test_pool_open_timeout_env_tunable_healthy_open(self) -> None:
        # 5.2/P2-4: the eager pool-open bound is configurable (operator can
        # widen it on a slow machine); a larger bound still opens cleanly
        # against the real test DB (healthy path).
        os.environ["CRYPTO_GUARD_POOL_OPEN_TIMEOUT"] = "5.0"
        try:
            self.assertEqual(pg_db._pool_open_timeout(), 5.0)
            pg_db.reset_pool()
            pool = pg_db.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 AS v")
                    self.assertEqual(cur.fetchone()["v"], 1)
        finally:
            os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)
            pg_db.reset_pool()

    def test_pool_open_timeout_falls_back_on_garbage(self) -> None:
        # 终审返工 P1 (08-10): values <=0, nan, inf, and non-numeric all fall
        # back to the PRODUCTION 30s posture (was 3.0 before the rework).
        for bad in ("not-a-number", "0", "-1", "-2.5", "nan", "inf", "-inf"):
            os.environ["CRYPTO_GUARD_POOL_OPEN_TIMEOUT"] = bad
            try:
                self.assertEqual(pg_db._pool_open_timeout(), 30.0,
                                 f"{bad!r} must fall back to 30.0")
            finally:
                os.environ.pop("CRYPTO_GUARD_POOL_OPEN_TIMEOUT", None)

    def test_runtime_rejects_superuser_dsn_without_leaking_password(self) -> None:
        secret = "do-not-echo-this"
        os.environ["CRYPTO_GUARD_DATABASE_URL"] = (
            f"postgresql://postgres:{secret}@localhost:5432/crypto_guard"
        )
        pg_db.reset_pool()
        with self.assertRaises(pg_db.CryptoGuardDBUnavailable) as cm:
            pg_db.get_pool()
        self.assertNotIn(secret, str(cm.exception))
        self.assertIn("dedicated", str(cm.exception))

    def test_database_identity_is_password_free(self) -> None:
        dsn = os.environ["CRYPTO_GUARD_DATABASE_URL"]
        password = dsn.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        identity = pg_db.database_identity()
        self.assertNotIn(password, identity)
        self.assertIn("crypto_guard_test_app@", identity)
        self.assertTrue(identity.endswith("/crypto_guard_test"))

    def test_connected_identity_rejects_dangerous_actual_role(self) -> None:
        """Role attributes returned by PostgreSQL override a safe-looking DSN."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.side_effect = [
            {
                "current_user": "crypto_guard_app",
                "session_user": "crypto_guard_app",
                "database_name": "crypto_guard",
                "rolsuper": True,
                "rolcreatedb": False,
                "rolcreaterole": False,
                "rolreplication": False,
                "rolbypassrls": False,
                "can_create_db_object": False,
                "can_create_public": False,
            },
            {"inherited_dangerous": False},
        ]
        with self.assertRaises(pg_db.CryptoGuardDBUnavailable):
            pg_db._validate_connected_identity(conn)

    def test_pool_checkout_error_is_normalized_and_redacted(self) -> None:
        fake_pool = MagicMock()
        fake_pool.getconn.side_effect = RuntimeError("checkout-secret")
        with patch.object(pg_db, "get_pool", return_value=fake_pool):
            with self.assertRaises(pg_db.CryptoGuardDBUnavailable) as cm:
                with pg_db.get_conn():
                    pass
        self.assertNotIn("checkout-secret", str(cm.exception))
        self.assertIn("checkout failed", str(cm.exception))

    def test_transaction_commits_on_success(self) -> None:
        # Create a temp table, insert, commit -> row visible on a fresh conn.
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS _pg_p0_tcx")
                cur.execute("CREATE TABLE _pg_p0_tcx (id INT, val TEXT)")
                cur.execute("INSERT INTO _pg_p0_tcx (id, val) VALUES (%s, %s)", (1, "a"))
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT val FROM _pg_p0_tcx WHERE id=1")
                row = cur.fetchone()
            conn.rollback()
        self.assertEqual(row["val"], "a")
        # cleanup
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS _pg_p0_tcx")
            conn.commit()

    def test_transaction_rolls_back_on_exception(self) -> None:
        with self.assertRaises(ValueError):
            with pg_db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS _pg_p0_rbx")
                    cur.execute("CREATE TABLE _pg_p0_rbx (id INT)")
                    cur.execute("INSERT INTO _pg_p0_rbx (id) VALUES (1)")
                raise ValueError("force rollback")
        # The insert must not have survived the rollback.
        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('_pg_p0_rbx') AS reg"
                )
                row = cur.fetchone()
            conn.rollback()
        # Table creation rolled back too (DDL is transactional in PG).
        self.assertIsNone(row["reg"])

    def test_pool_reuses_connections_across_many_uses(self) -> None:
        for _ in range(20):
            with pg_db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 AS v")
                    cur.fetchone()
                conn.rollback()
        # If connections leaked, the pool (max_size=8) would block here; reaching
        # this assertion means all 20 were returned.

    def test_read_then_nested_write_persists_after_get_conn_exit(self) -> None:
        """A clean outer read transaction must not discard a nested write."""
        with pg_db.transaction() as conn:
            conn.execute("CREATE TABLE _pg_nested_write (id INT PRIMARY KEY)")

        with pg_db.get_conn() as conn:
            conn.execute("SELECT COUNT(*) AS c FROM _pg_nested_write").fetchone()
            with conn.transaction():
                conn.execute("INSERT INTO _pg_nested_write(id) VALUES (1)")

        with direct_conn(self.schema) as observer:
            row = observer.execute(
                "SELECT COUNT(*) AS c FROM _pg_nested_write WHERE id=1"
            ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    def test_make_repo_initialization_failure_cleans_schema_and_globals(self) -> None:
        """Fixture setup failure cannot leak a schema or process configuration."""
        from plugins.crypto_guard.tests import pg_fixtures

        leaked_schema = "test_fixture_init_failure"
        saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
        saved_search_path = pg_db.get_test_search_path()
        with patch.object(pg_fixtures, "_new_schema_name", return_value=leaked_schema), patch.object(
            pg_fixtures, "initialize_database", side_effect=RuntimeError("forced init failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "forced init failure"):
                make_repo()
        self.assertEqual(os.environ.get("CRYPTO_GUARD_DATABASE_URL"), saved_url)
        self.assertEqual(pg_db.get_test_search_path(), saved_search_path)
        with pg_db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pg_namespace WHERE nspname=%s",
                (leaked_schema,),
            ).fetchone()
            conn.rollback()
        self.assertEqual(int(row["c"]), 0)


if __name__ == "__main__":
    unittest.main()
