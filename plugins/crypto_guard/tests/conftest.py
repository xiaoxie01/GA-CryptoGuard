"""Root conftest for the CryptoGuard PostgreSQL test suite (P9).

Ensures the ``crypto_guard_app`` role + ``crypto_guard_test`` database exist
once per process (idempotent bootstrap) and that ``CRYPTO_GUARD_DATABASE_URL``
points at the app DSN before any test imports ``load_config`` / ``pg_db``.
Per-test schema isolation is provided by ``tests/pg_fixtures.make_repo()`` in
each ``setUp``; this conftest only guarantees the shared role/DB exist.
"""

from __future__ import annotations

import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _rollback_isolation_marker(request: pytest.FixtureRequest) -> None:
    """Step 5.4 opt-in gate: ``@pytest.mark.rollback_isolation`` redirects
    ``make_repo()``/``make_reusable_repo()`` to one shared per-worker schema
    wrapped in a per-test transaction rolled back at teardown.

    Non-marked tests pay a marker lookup only (no imports, no PG traffic); the
    ``pg_fixtures`` module is imported lazily so unit-only runs stay PG-free.
    """
    if request.node.get_closest_marker("rollback_isolation") is None:
        yield
        return
    from plugins.crypto_guard.tests import pg_fixtures as fx

    fx.set_rollback_active(True)
    try:
        yield
    finally:
        fx.set_rollback_active(False)
        # If the test failed before its handle.close(), roll the open checkout
        # back so the next opted-in test still sees the clean baseline. The
        # cleanup must NEVER mask the primary failure (P2-6).
        fx.safe_close_open_rollback_handle(sys.exc_info()[1])


def _ensure_test_db() -> None:
    # The admin password is a test-env secret; never committed. Tests must set
    # it or skip gracefully.
    if not os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD"):
        return
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn


_ensure_test_db()
