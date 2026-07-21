"""Root conftest for the CryptoGuard PostgreSQL test suite (P9).

Ensures the ``crypto_guard_app`` role + ``crypto_guard_test`` database exist
once per process (idempotent bootstrap) and that ``CRYPTO_GUARD_DATABASE_URL``
points at the app DSN before any test imports ``load_config`` / ``pg_db``.
Per-test schema isolation is provided by ``tests/pg_fixtures.make_repo()`` in
each ``setUp``; this conftest only guarantees the shared role/DB exist.
"""

from __future__ import annotations

import os


def _ensure_test_db() -> None:
    # The admin password is a test-env secret; never committed. Tests must set
    # it or skip gracefully.
    if not os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD"):
        return
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn


_ensure_test_db()
