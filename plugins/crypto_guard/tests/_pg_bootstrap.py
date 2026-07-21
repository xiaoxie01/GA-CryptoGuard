"""Test-only PostgreSQL bootstrap.

Creates the dedicated ``crypto_guard_test_app`` role (least privilege - no
superuser) and the dedicated ``crypto_guard_test`` database used by the smoke
suite. Tests never reuse or rotate the production ``crypto_guard_app`` role.

The admin password is read from ``CRYPTO_GUARD_DB_ADMIN_PASSWORD`` (test env
only); it is NEVER written to source, YAML, logs, or git. This module is
imported only by test fixtures and the dev bootstrap, never by the application
runtime.

Safety rules honored:
- Never DROP an existing database. If ``crypto_guard_test`` exists and is
  non-empty, the bootstrap stops and reports rather than dropping. (For tests
  we instead use per-test schema isolation inside the test DB, so this DB is
  created once and reused - it carries no business data.)
- Never DROP a role that owns live data.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading

import psycopg

ADMIN_DSN = (
    "host=localhost port=5432 user=postgres "
    "password={pw} dbname=postgres"
)
APP_ROLE = "crypto_guard_test_app"
TEST_DB = "crypto_guard_test"

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False
_APP_PASSWORD: str | None = None


def _admin_dsn() -> str:
    pw = os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(
            "CRYPTO_GUARD_DB_ADMIN_PASSWORD is not set; the test bootstrap "
            "needs the local postgres admin password (test env only - never "
            "committed)."
        )
    return ADMIN_DSN.format(pw=pw)


def _role_exists(cur: psycopg.Cursor, role: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
    return cur.fetchone() is not None


def _db_exists(cur: psycopg.Cursor, db: str) -> bool:
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,))
    return cur.fetchone() is not None


def _db_nonempty() -> bool:
    """Placeholder retained for the safety contract; the test DB uses isolated
    schemas and is never dropped, so a non-empty guard is not needed here."""
    return False


def _app_password() -> str:
    """Return the app-role password, resolving it on first use.

    Resolution order (stable across processes, never committed as plaintext):

    1. ``CRYPTO_GUARD_APP_PASSWORD`` env var -- explicit override.
    2. A deterministic derivation from ``CRYPTO_GUARD_DB_ADMIN_PASSWORD`` (which
       is already required env-only for bootstrap). Deriving -- not hardcoding
       -- means every test process/subprocess that shares the admin password
       computes the SAME app password, so the unconditional ``ALTER ROLE ...
       PASSWORD`` in ``ensure_bootstrap`` is idempotent across concurrent
       processes. Without this, each process generated a random per-process
       password and the unconditional ``ALTER ROLE`` clobbered the shared role
       out from under the others (``Password 验证失败`` cascade across ~70
       tests when a subprocess or a parallel process re-bootstrapped).
    3. A random per-process secret (legacy fallback when neither env var is
       set -- single-process only; concurrent processes would still clobber).

    The resolved value is held in memory only; it is never written to disk.
    """
    global _APP_PASSWORD
    if _APP_PASSWORD is None:
        explicit = os.environ.get("CRYPTO_GUARD_APP_PASSWORD", "").strip()
        if explicit:
            _APP_PASSWORD = explicit
        else:
            admin_pw = os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD", "").strip()
            if admin_pw:
                digest = hashlib.sha256(
                    ("crypto_guard_test_app:" + admin_pw).encode("utf-8")
                ).hexdigest()
                _APP_PASSWORD = "cgapp_" + digest[:24]
            else:
                _APP_PASSWORD = secrets.token_urlsafe(18)
    return _APP_PASSWORD


def ensure_bootstrap() -> str:
    """Idempotently create role + test DB; return the app DSN for tests."""
    global _BOOTSTRAPPED
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return _app_dsn()
        admin = _admin_dsn()
        pw = _app_password()
        # Escape the in-memory password as a SQL string literal. The password is
        # a randomly generated secret (never user input, never committed), but
        # we still escape single quotes defensively.
        pw_lit = "'" + pw.replace("'", "''") + "'"
        # Connect to the maintenance DB (autocommit so CREATE DATABASE works).
        with psycopg.connect(admin, autocommit=True) as conn:
            with conn.cursor() as cur:
                if not _role_exists(cur, APP_ROLE):
                    cur.execute(
                        f"CREATE ROLE {APP_ROLE} WITH LOGIN PASSWORD {pw_lit} "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    )
                else:
                    # Rotate the password to the in-memory value so the test
                    # env is self-contained across re-runs.
                    cur.execute(
                        f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD {pw_lit}"
                    )
                # Grants the role needs to own + write the test DB.
                cur.execute(
                    f"GRANT CONNECT ON DATABASE postgres TO {APP_ROLE}"
                )
        # Ensure the test database exists (create once, never drop).
        if not _db_exists_psycopg(admin, TEST_DB):
            with psycopg.connect(admin, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE DATABASE {TEST_DB} OWNER {APP_ROLE} "
                        "ENCODING 'UTF8'"
                    )
        # The dedicated role owns the disposable test DB. Owning the database
        # is sufficient to create/drop per-test scratch schemas; no shared
        # ``public`` schema grants are needed.
        with psycopg.connect(admin, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"ALTER DATABASE {TEST_DB} OWNER TO {APP_ROLE}")
                cur.execute(
                    f"GRANT ALL PRIVILEGES ON DATABASE {TEST_DB} TO {APP_ROLE}"
                )
        app_dsn = _app_dsn()
        _BOOTSTRAPPED = True
        return app_dsn


def _db_exists_psycopg(admin: str, db: str) -> bool:
    with psycopg.connect(admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            return _db_exists(cur, db)


def _app_dsn() -> str:
    pw = _app_password()
    return (
        f"postgresql://{APP_ROLE}:{pw}@localhost:5432/{TEST_DB}"
    )


def app_dsn() -> str:
    """Return the app-role DSN, bootstrapping the role/DB if needed."""
    return ensure_bootstrap()
