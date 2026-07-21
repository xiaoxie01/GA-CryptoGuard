"""PostgreSQL test fixtures for the smoke suite (P9).

Provides the per-test schema-isolation pattern mandated by design §8:

  - ``set_test_dsn()``: ensure the ``crypto_guard_test_app`` role + ``crypto_guard_test``
    DB exist (via ``_pg_bootstrap``) and point ``CRYPTO_GUARD_DATABASE_URL`` at
    the app DSN. Idempotent.
  - ``scratch_schema()``: a context manager that creates a unique schema
    ``test_<uuid>``, routes the connection pool's ``search_path`` at it, runs
    ``initialize_database()`` into that fresh schema, and yields a pooled
    ``psycopg.Connection`` + ``CryptoGuardRepository`` bound to it. On exit the
    scratch schema is dropped and the pool reset, so tests never touch the
    production/``public`` schema and never leak schemas across tests.
  - ``make_repo()``: the ``setUp`` helper replacing the legacy
    ``CRYPTO_GUARD_DB=<tmp>.sqlite3`` + ``connect_db`` pattern. Returns a
    ``_RepoHandle`` whose ``conn`` / ``repo`` attributes the test uses, and
    whose ``close()`` is the ``tearDown``.

Each ``setUp`` calls ``self._h = make_repo()``; ``tearDown`` calls
``self._h.close()``. The handle also re-applies the broker market-data-health
patch and the ``CRYPTO_GUARD_LLM_ANALYSIS=0`` env that the legacy ``setUp``
set, so migrated classes keep their existing assertions.

This is real PostgreSQL (NOT a mock, NOT a SQLite shim). Every SQL site in the
suite that hit this connection is hand-converted to PostgreSQL placeholders
(``%s``) and dict-row access; there is no dynamic ``?``->``%s`` translator at
runtime - the conversion is done by hand in the test source.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def set_test_dsn() -> str:
    """Ensure the role/test DB exist and set ``CRYPTO_GUARD_DATABASE_URL``.

    Idempotent: safe to call from every ``setUp``. Returns the app DSN.
    """
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    return dsn


def _new_schema_name() -> str:
    return f"test_{uuid.uuid4().hex}".lower()


def _create_scratch_schema(schema: str) -> None:
    """Create an empty scratch schema owned by the app role.

    Uses a dedicated (non-pooled) connection so the CREATE SCHEMA lands outside
    any ``search_path`` override; the pool is then reset so subsequent
    ``get_conn()`` checkouts pick up the new ``search_path`` via the configure
    hook.
    """
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')


def _drop_scratch_schema(schema: str) -> None:
    """Drop a scratch schema (CASCADE); cleanup failure is a test failure."""
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@contextmanager
def scratch_schema(*, initialize_schema: bool = True) -> Iterator[str]:
    """Route the process pool at one isolated schema without holding a conn.

    Connection-layer tests need to reset or deliberately break the pool while
    retaining schema isolation. Holding the ``make_repo`` checkout during such
    a reset would conflate pool-lifecycle behavior with fixture cleanup, so
    this lighter context yields only the schema name.
    """
    saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    saved_redis_disabled = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
    saved_search_path = pg_db.get_test_search_path()
    schema: str | None = None
    try:
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        set_test_dsn()
        schema = _new_schema_name()
        _create_scratch_schema(schema)
        pg_db.set_test_search_path(schema)
        pg_db.reset_pool()
        if initialize_schema:
            initialize_database()
        yield schema
    finally:
        pg_db.reset_pool()
        try:
            if schema is not None:
                _drop_scratch_schema(schema)
        finally:
            pg_db.set_test_search_path(saved_search_path)
            pg_db.reset_pool()
            if saved_url is not None:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved_url
            else:
                os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
            if saved_redis_disabled is not None:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved_redis_disabled
            else:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)


@dataclass
class _RepoHandle:
    """Holds the per-test pooled conn + repo + the scratch schema name.

    ``close()`` is the tearDown: drop the scratch schema, reset the pool (so
    no checkout retains the now-dropped search_path), and restore the saved
    ``CRYPTO_GUARD_DATABASE_URL``.
    """

    conn: psycopg.Connection
    repo: CryptoGuardRepository
    schema: str
    _saved_url: str | None
    _saved_redis_disabled: str | None
    _saved_search_path: str | None
    _cm: "contextmanager[psycopg.Connection]"  # the get_conn() CM we entered

    def close(self) -> None:
        schema = self.schema
        saved = self._saved_url
        saved_redis = self._saved_redis_disabled
        # Return the pooled connection first (its search_path points at the
        # scratch schema we are about to drop; that is fine - we reset the
        # pool after).
        checkout_error: BaseException | None = None
        try:
            self._cm.__exit__(None, None, None)
        except BaseException as exc:
            checkout_error = exc
        try:
            _drop_scratch_schema(schema)
        finally:
            pg_db.set_test_search_path(self._saved_search_path)
            pg_db.reset_pool()
            if saved is not None:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved
            else:
                os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
            # Restore the Redis-disabled flag (see make_repo for why it is set).
            if saved_redis is not None:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved_redis
            else:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
        if checkout_error is not None:
            raise checkout_error


def make_repo(*, initialize_schema: bool = True) -> _RepoHandle:
    """Create a fresh isolated PG schema + repo for one test.

    Replaces the legacy ``os.environ["CRYPTO_GUARD_DB"]=<tmp>.sqlite3`` +
    ``connect_db`` + ``initialize_database()`` setUp sequence. Each test gets
    its own schema so tests never interfere; the schema is dropped in
    ``close()``.
    """
    saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    # 07-16 cutover (Redis test isolation): under SQLite the legacy test DB
    # lived in tempfile.gettempdir(), so redis_adapter.should_use_redis_for_path
    # (<tmp path>) returned False via its "db under tempdir" branch -- i.e.
    # Redis was DISABLED for every test, and production alert_delivery.
    # send_markdown_alert never touched the shared Redis. Under PostgreSQL there
    # is no DB *file path* (alert_delivery calls should_use_redis_for_path(None)),
    # and should_use_redis_for_path(None) returns True, so without this guard
    # send_markdown_alert would hit the real shared Redis: the first run SETs a
    # quiet key (TTL 300s) and the second run within that window is silenced
    # early (alert_delivery.py ~line 64), writes NO alert_outbox row, and the
    # test's outbox assertion hits None -> nondeterministic cross-run flakiness
    # (the test_smoke #21 run-2 line-856 failure). Setting
    # CRYPTO_GUARD_REDIS_DISABLED=1 makes should_use_redis_for_path(None) return
    # False (redis_adapter.py:16-17), restoring the SQLite-era test-time
    # behavior the suite was written against -- without weakening any
    # assertion. Tests that exercise real Redis behavior (the R6/R7
    # wake-up/future-delivery/CAS cases) patch should_use_redis_for_path to
    # return True AND install a _FakeRedis, so the env flag does not affect
    # them (the patched function ignores the env var).
    saved_redis_disabled = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
    saved_search_path = pg_db.get_test_search_path()
    schema: str | None = None
    try:
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        set_test_dsn()
        schema = _new_schema_name()
        _create_scratch_schema(schema)
        # Route the pool at the scratch schema, then reset so checkouts re-SET.
        pg_db.set_test_search_path(schema)
        pg_db.reset_pool()
        if initialize_schema:
            initialize_database()
        cm = pg_db.get_conn()
        conn = cm.__enter__()
        repo = CryptoGuardRepository(conn)
        return _RepoHandle(
            conn=conn, repo=repo, schema=schema,
            _saved_url=saved_url,
            _saved_redis_disabled=saved_redis_disabled,
            _saved_search_path=saved_search_path, _cm=cm,
        )
    except BaseException:
        pg_db.reset_pool()
        try:
            if schema is not None:
                _drop_scratch_schema(schema)
        finally:
            pg_db.set_test_search_path(saved_search_path)
            pg_db.reset_pool()
            if saved_url is not None:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved_url
            else:
                os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
            if saved_redis_disabled is not None:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved_redis_disabled
            else:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
        raise


def direct_conn(schema: str) -> psycopg.Connection:
    """A NON-pooled direct psycopg connection routed at ``schema``.

    For tests that simulate a *second process* (e.g. ``sm_module_conn`` in the
    service-ownership lease tests). A pooled checkout would share the pool's
    single ``search_path`` and ``.close()`` would only return it to the pool
    (leaving the schema reference dangling); a direct ``psycopg.connect`` is an
    independent backend whose ``.close()`` truly closes it, so the test can
    ``conn_b.close()`` without disturbing the pooled ``self.conn``.

    The connection is configured to MIRROR the pool (pg_db
    ``_build_conn_kwargs_for_pool``): ``dict_row`` row factory + ``TimeZone=UTC``
    + ``search_path`` routed at the scratch schema (then ``public``). Matching
    the pool's row shape is mandatory, not cosmetic: without ``dict_row`` a
    direct conn returns TUPLE rows, so production code that does ``row["col"]``
    (e.g. ``acquire_service_ownership`` reading ``row["pid"]``) raises
    ``TypeError: tuple indices must be integers or slices, not str``.

    The connection is returned idle (autocommit=False, no open transaction) so
    the caller's first ``with conn.transaction():`` starts a clean txn. Caller
    owns the lifecycle: ``conn.close()`` when done.
    """
    from psycopg.rows import dict_row
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    conn = psycopg.connect(app_dsn())
    conn.row_factory = dict_row
    # Issue the GUC SETs in autocommit mode so they do not leave an open
    # transaction (mirrors the pool configure hook), then flip back to the
    # default transactional mode so the conn is idle/READY for the caller.
    was_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET TimeZone=UTC")
            cur.execute(f'SET search_path TO "{schema}", public')
    finally:
        conn.autocommit = was_autocommit
    return conn


__all__ = [
    "set_test_dsn",
    "scratch_schema",
    "make_repo",
    "direct_conn",
    "_RepoHandle",
]
