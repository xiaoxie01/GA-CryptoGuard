"""PostgreSQL connection layer for CryptoGuard.

Replaces ``sqlite_db.connect_db``. CryptoGuard now runs on PostgreSQL only -
there is NO SQLite fallback (fail-closed). Connections come from a process-wide
``psycopg_pool.ConnectionPool`` so connections are reliably returned to the
pool (no leaks). Rows are dict-like (``psycopg.rows.dict_row``), matching the
shape callers expected from ``sqlite3.Row``. The connection timezone is UTC.

Configuration: the DSN is read from ``CRYPTO_GUARD_DATABASE_URL`` via
``config.loader.resolve_database_url``. The DSN is never hard-coded; the
application password is supplied only via that env var (no plaintext in the
repo). The runtime NEVER uses the ``postgres`` superuser - the DSN points at the
dedicated ``crypto_guard_app`` role on the ``crypto_guard`` database.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class CryptoGuardDBUnavailable(RuntimeError):
    """Raised when PostgreSQL is unreachable / misconfigured.

    Callers gate analysis and orders on this: a missing DB must block trading,
    emit an explicit alarm, and NEVER fall back to SQLite.
    """


_DSN_LOCK = threading.Lock()
_POOL: ConnectionPool | None = None
_POOL_DSN: str | None = None
# Test isolation: when a per-test schema override is active, the pool opens
# connections with ``search_path`` set to that schema so tests never touch the
# public/production schema. ``None`` means "use the DSN's default search_path".
_SEARCH_PATH_OVERRIDE: str | None = None
_SEARCH_PATH_LOCK = threading.Lock()


def resolve_dsn() -> str:
    """Resolve the PostgreSQL DSN, fail-closed if absent.

    Delegates to ``config.loader.resolve_database_url`` so the DSN source is a
    single place (env ``CRYPTO_GUARD_DATABASE_URL``). Raises
    ``CryptoGuardDBUnavailable`` when unset - NO SQLite fallback.
    """
    from plugins.crypto_guard.config.loader import resolve_database_url

    try:
        return resolve_database_url()
    except RuntimeError as exc:
        raise CryptoGuardDBUnavailable(str(exc)) from exc


def database_identity(dsn: str | None = None) -> str:
    """Return a password-free PostgreSQL identity for logs and status output."""
    try:
        values = conninfo_to_dict(dsn or resolve_dsn())
    except Exception:
        return "postgresql://<unavailable>"
    user = str(values.get("user") or "<unknown-user>")
    host = str(values.get("host") or "localhost")
    port = str(values.get("port") or "5432")
    dbname = str(values.get("dbname") or "<unknown-db>")
    return f"postgresql://{user}@{host}:{port}/{dbname}"


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use."""
    global _POOL, _POOL_DSN
    dsn = resolve_dsn()
    with _DSN_LOCK:
        if _POOL is not None and _POOL_DSN == dsn:
            return _POOL
        if _POOL is not None:
            # DSN changed (e.g. test switched DB) - close the old pool.
            try:
                _POOL.close()
            except Exception:
                pass
            _POOL = None
            _POOL_DSN = None
        try:
            # ``open=False`` is passed to the constructor explicitly to suppress
            # the psycopg_pool deprecation warning (the default for ``open`` will
            # become ``False``). We then call ``pool.open(wait=True)`` ourselves
            # so the pool eagerly establishes its ``min_size`` connections and a
            # bad DSN (wrong port, unreachable host) fails fast HERE, at pool
            # creation - not deferred to the first ``getconn()``. Fail-fast at
            # ``get_pool`` is the fail-closed contract callers rely on.
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=8,
                timeout=30.0,
                configure=_build_conn_kwargs_for_pool,
                open=False,
            )
            pool.open(wait=True)
        except Exception as exc:  # noqa: BLE001 - any pool/open failure is fatal-but-catchable
            raise CryptoGuardDBUnavailable(
                f"PostgreSQL pool unavailable ({type(exc).__name__})"
            ) from exc
        _POOL = pool
        _POOL_DSN = dsn
        return pool


def _build_conn_kwargs_for_pool(conn: psycopg.Connection) -> None:
    """Pool configure hook: apply row factory + UTC + search_path to each conn.

    The pool requires the configure function to return the connection in
    READY state (not inside an open transaction). ``SET`` on a non-autocommit
    connection opens a transaction (INTRANS), which the pool rejects and
    discards. So we flip to autocommit for the SETs, then back to the default
    transactional mode, leaving the connection READY for the pool.
    """
    conn.row_factory = dict_row
    # Each GUC needs its own SET statement: PostgreSQL ``SET`` takes a single
    # assignment (``SET name = value``), not a comma-list of assignments.
    # Combining them as ``SET TimeZone=UTC, search_path=...`` is a syntax
    # error near the comma. Issue them as separate statements so the
    # ``search_path`` override (per-test scratch-schema isolation) actually
    # applies.
    with _SEARCH_PATH_LOCK:
        schema = _SEARCH_PATH_OVERRIDE
    statements = ["SET TimeZone=UTC"]
    if schema:
        safe = schema.replace("'", "''")
        statements.append(f'SET search_path={safe},public')
    was_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        _validate_connected_identity(conn)
    finally:
        conn.autocommit = was_autocommit


def _validate_connected_identity(conn: psycopg.Connection) -> None:
    """Verify the authenticated PostgreSQL principal, not merely DSN text."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_user AS current_user, session_user AS session_user,
                   current_database() AS database_name,
                   r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                   r.rolreplication, r.rolbypassrls,
                   has_database_privilege(current_user, current_database(), 'CREATE') AS can_create_db_object,
                   has_schema_privilege(current_user, 'public', 'CREATE') AS can_create_public
            FROM pg_roles r WHERE r.rolname=current_user
            """
        )
        row = cur.fetchone()
        if not row:
            raise CryptoGuardDBUnavailable("PostgreSQL runtime identity is unavailable")
        current = str(row["current_user"])
        session = str(row["session_user"])
        database = str(row["database_name"])
        allowed = {
            ("crypto_guard_app", "crypto_guard"),
            ("crypto_guard_test_app", "crypto_guard_test"),
        }
        dangerous = any(
            bool(row[key])
            for key in (
                "rolsuper", "rolcreatedb", "rolcreaterole",
                "rolreplication", "rolbypassrls",
            )
        )
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_roles parent
                WHERE pg_has_role(current_user, parent.oid, 'member')
                  AND parent.rolname <> current_user
                  AND (parent.rolsuper OR parent.rolcreatedb OR parent.rolcreaterole
                       OR parent.rolreplication OR parent.rolbypassrls)
            ) AS inherited_dangerous
            """
        )
        inherited_dangerous = bool(cur.fetchone()["inherited_dangerous"])
    if (
        current != session
        or (current, database) not in allowed
        or dangerous
        or inherited_dangerous
    ):
        raise CryptoGuardDBUnavailable(
            "PostgreSQL connected principal violates the dedicated-role contract"
        )
    if current == "crypto_guard_app" and (
        bool(row["can_create_db_object"]) or bool(row["can_create_public"])
    ):
        raise CryptoGuardDBUnavailable(
            "PostgreSQL runtime role has forbidden DDL privileges"
        )


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection; guarantee return to the pool in READY state.

    Opens with ``autocommit=False``. A clean scope commits any pending outer
    transaction before pool return; this is required by production
    read-then-nested-write paths, where an early SELECT opened the outer
    transaction and a repository method used a savepoint for its write. An
    exceptional or aborted scope rolls back and never leaks locks or an
    ``idle in transaction`` backend into the pool.
    """
    pool = get_pool()
    try:
        conn = pool.getconn()
    except Exception as exc:
        raise CryptoGuardDBUnavailable(
            f"PostgreSQL connection checkout failed ({type(exc).__name__})"
        ) from exc
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        finally:
            pool.putconn(conn)
        raise
    else:
        try:
            status = conn.info.transaction_status
            if status == TransactionStatus.INERROR:
                conn.rollback()
                raise CryptoGuardDBUnavailable(
                    "PostgreSQL work unit ended in an aborted transaction"
                )
            if status == TransactionStatus.INTRANS:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            finally:
                pool.putconn(conn)
            raise
        pool.putconn(conn)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection wrapped in a transaction.

    Commits on clean exit, rolls back on any exception. Use for the atomic
    multi-statement groups (batch seal/claim, ownership CAS, attempt_id).
    """
    with get_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def savepoint(conn: psycopg.Connection, name: str = "sp") -> Iterator[psycopg.Connection]:
    """Nested savepoint: roll back ONLY the local statement on error.

    PostgreSQL aborts the whole transaction when a statement fails (e.g. a
    UNIQUE violation), so a bare ``except UniqueViolation`` followed by another
    query would hit "current transaction is aborted". A full ``conn.rollback()``
    would discard the caller's outer transaction. This context manager wraps the
    guarded statements in a SAVEPOINT/RELEASE/ROLLBACK TO block via psycopg's
    native nested-transaction support (``conn.transaction()``), so on error only
    the statements inside this block are rolled back and the outer transaction
    is left intact and usable.

    Use this ONLY when an exception is expected to be caught and recovered from
    in place (e.g. INSERT-may-collide fallback). For normal upserts prefer
    ``ON CONFLICT ... DO UPDATE ... RETURNING`` directly - no exception, no
    savepoint, no aborted-transaction window.
    """
    with conn.transaction():
        yield conn


def set_test_search_path(schema: str | None) -> None:
    """Test-only: route pool connections to an isolated schema.

    When ``schema`` is non-None, every pooled connection sets
    ``search_path=<schema>,public`` so test fixtures never touch the production
    schema. Pass ``None`` to restore the default. Existing pool connections
    pick up the new search_path on their next checkout via the configure hook
    re-SET; to be safe, tests should reset the pool (``reset_pool``) after
    changing the schema so no stale connection retains the old search_path.
    """
    global _SEARCH_PATH_OVERRIDE
    with _SEARCH_PATH_LOCK:
        _SEARCH_PATH_OVERRIDE = schema


def get_test_search_path() -> str | None:
    """Return the active test-only search-path override."""
    with _SEARCH_PATH_LOCK:
        return _SEARCH_PATH_OVERRIDE


def reset_pool() -> None:
    """Close and clear the cached pool (used when the DSN/search_path changes)."""
    global _POOL, _POOL_DSN
    with _DSN_LOCK:
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
        _POOL = None
        _POOL_DSN = None


def check_connection() -> dict[str, Any]:
    """Open one pooled connection, run ``SELECT 1``, return a health dict.

    Used by ``/status`` and the fail-closed startup gate. Never raises to
    callers - returns ``{"ok": False, "error": ...}`` so the UI can render an
    explicit "PostgreSQL unavailable" alarm.
    """
    try:
        with get_conn() as conn:
            server_version = conn.info.server_version
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS v")
                cur.fetchone()
            conn.rollback()
        return {
            "ok": True,
            "engine": "postgresql",
            "server_version": server_version,
        }
    except CryptoGuardDBUnavailable as exc:
        return {"ok": False, "engine": "postgresql", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "engine": "postgresql", "error": str(exc)}


__all__ = [
    "CryptoGuardDBUnavailable",
    "check_connection",
    "database_identity",
    "get_conn",
    "get_pool",
    "get_test_search_path",
    "resolve_dsn",
    "reset_pool",
    "savepoint",
    "set_test_search_path",
    "transaction",
]
