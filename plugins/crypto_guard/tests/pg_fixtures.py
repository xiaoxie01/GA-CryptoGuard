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
import sys
import atexit
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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


def _initialize_isolated_test_schema() -> None:
    """Build isolated DDL in parallel, then run the real initializer gates.

    Production must serialize greenfield DDL with its database-wide advisory
    lock. Test schemas are unique per worker/test, so applying the identical
    schema file before entering ``initialize_database`` avoids serializing the
    expensive DDL while preserving real seed, marker, advisory-lock, and health
    behavior on the subsequent initializer call.
    """

    from plugins.crypto_guard.storage.migrations import SCHEMA_PATH

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with pg_db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    initialize_database()


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
            _initialize_isolated_test_schema()
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


@dataclass
class _ReusableRepoHandle:
    """One checkout from the process-local reusable test schema."""

    conn: psycopg.Connection
    repo: CryptoGuardRepository
    schema: str
    _saved_url: str | None
    _saved_redis_disabled: str | None
    _saved_search_path: str | None
    _cm: "contextmanager[psycopg.Connection]"
    _lock: threading.RLock
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        checkout_error: BaseException | None = None
        try:
            if self.conn.info.transaction_status != TransactionStatus.IDLE:
                self.conn.rollback()
            self._cm.__exit__(None, None, None)
        except BaseException as exc:
            checkout_error = exc
        finally:
            pg_db.set_test_search_path(self._saved_search_path)
            pg_db.reset_pool()
            if self._saved_url is not None:
                os.environ["CRYPTO_GUARD_DATABASE_URL"] = self._saved_url
            else:
                os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
            if self._saved_redis_disabled is not None:
                os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = self._saved_redis_disabled
            else:
                os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
            self._lock.release()
        if checkout_error is not None:
            raise checkout_error


_REUSABLE_SCHEMA_LOCK = threading.RLock()
_REUSABLE_SCHEMA: str | None = None
_REUSABLE_SCHEMA_FINGERPRINT: str | None = None
_REUSABLE_TABLE_ORDER: tuple[str, ...] = ()
_REUSABLE_BASELINE_ROWS: dict[
    str,
    tuple[
        tuple[str, ...],
        frozenset[str],
        tuple[tuple[object, ...], ...],
    ],
] = {}
_REUSABLE_SEQUENCES: tuple[tuple[str, str, str], ...] = ()


def _activate_test_schema(schema: str) -> None:
    pg_db.set_test_search_path(schema)
    pg_db.reset_pool()


def _schema_fingerprint(schema: str) -> str:
    from plugins.crypto_guard.storage.migrations import _schema_catalog_fingerprint

    with pg_db.get_conn() as conn:
        with conn.cursor() as cur:
            value = _schema_catalog_fingerprint(cur, schema)
        conn.rollback()
    return value


def _create_reusable_schema() -> str:
    global _REUSABLE_SCHEMA, _REUSABLE_SCHEMA_FINGERPRINT
    global _REUSABLE_TABLE_ORDER, _REUSABLE_BASELINE_ROWS
    global _REUSABLE_SEQUENCES

    schema = f"test_reuse_{uuid.uuid4().hex}".lower()
    _create_scratch_schema(schema)
    try:
        _activate_test_schema(schema)
        _initialize_isolated_test_schema()
        _REUSABLE_SCHEMA = schema
        _REUSABLE_SCHEMA_FINGERPRINT = _schema_fingerprint(schema)
        (
            _REUSABLE_TABLE_ORDER,
            _REUSABLE_BASELINE_ROWS,
            _REUSABLE_SEQUENCES,
        ) = _capture_reusable_baseline(schema)
        return schema
    except BaseException:
        pg_db.reset_pool()
        _drop_scratch_schema(schema)
        _REUSABLE_SCHEMA = None
        _REUSABLE_SCHEMA_FINGERPRINT = None
        _REUSABLE_TABLE_ORDER = ()
        _REUSABLE_BASELINE_ROWS = {}
        _REUSABLE_SEQUENCES = ()
        raise


def _drop_reusable_schema() -> None:
    global _REUSABLE_SCHEMA, _REUSABLE_SCHEMA_FINGERPRINT
    global _REUSABLE_TABLE_ORDER, _REUSABLE_BASELINE_ROWS
    global _REUSABLE_SEQUENCES

    with _REUSABLE_SCHEMA_LOCK:
        schema = _REUSABLE_SCHEMA
        _REUSABLE_SCHEMA = None
        _REUSABLE_SCHEMA_FINGERPRINT = None
        _REUSABLE_TABLE_ORDER = ()
        _REUSABLE_BASELINE_ROWS = {}
        _REUSABLE_SEQUENCES = ()
        if schema is None:
            return
        pg_db.reset_pool()
        try:
            _drop_scratch_schema(schema)
        finally:
            if pg_db.get_test_search_path() == schema:
                pg_db.set_test_search_path(None)
            pg_db.reset_pool()


def _capture_reusable_baseline(
    schema: str,
) -> tuple[
    tuple[str, ...],
    dict[
        str,
        tuple[
            tuple[str, ...],
            frozenset[str],
            tuple[tuple[object, ...], ...],
        ],
    ],
    tuple[tuple[str, str, str], ...],
]:
    """Capture the production initializer's seed rows for fast restoration."""

    with pg_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname=%s ORDER BY tablename",
                (schema,),
            )
            tables = [str(row["tablename"]) for row in cur.fetchall()]
            if not tables:
                raise RuntimeError("reusable PostgreSQL test schema has no tables")
            cur.execute(
                """
                SELECT child.relname AS child_table,
                       parent.relname AS parent_table
                FROM pg_catalog.pg_constraint fk
                JOIN pg_catalog.pg_class child ON child.oid=fk.conrelid
                JOIN pg_catalog.pg_namespace child_ns
                  ON child_ns.oid=child.relnamespace
                JOIN pg_catalog.pg_class parent ON parent.oid=fk.confrelid
                JOIN pg_catalog.pg_namespace parent_ns
                  ON parent_ns.oid=parent.relnamespace
                WHERE fk.contype='f'
                  AND child_ns.nspname=%s
                  AND parent_ns.nspname=%s
                """,
                (schema, schema),
            )
            edges = {
                (str(row["child_table"]), str(row["parent_table"]))
                for row in cur.fetchall()
                if row["child_table"] != row["parent_table"]
            }
            insert_order = tuple(reversed(_foreign_key_delete_order(tables, edges)))
            baseline: dict[
                str,
                tuple[
                    tuple[str, ...],
                    frozenset[str],
                    tuple[tuple[object, ...], ...],
                ],
            ] = {}
            for table in insert_order:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s "
                    "AND data_type='jsonb'",
                    (schema, table),
                )
                jsonb_columns = frozenset(
                    str(row["column_name"]) for row in cur.fetchall()
                )
                cur.execute(
                    sql.SQL("SELECT * FROM {}").format(
                        sql.Identifier(schema, table)
                    )
                )
                columns = tuple(str(col.name) for col in (cur.description or ()))
                rows = tuple(
                    tuple(row[column] for column in columns)
                    for row in cur.fetchall()
                )
                baseline[table] = (columns, jsonb_columns, rows)
            cur.execute(
                """
                SELECT sequence.relname AS sequence_name,
                       owner_table.relname AS table_name,
                       owner_column.attname AS column_name
                FROM pg_catalog.pg_class sequence
                JOIN pg_catalog.pg_namespace sequence_ns
                  ON sequence_ns.oid=sequence.relnamespace
                JOIN pg_catalog.pg_depend dependency
                  ON dependency.objid=sequence.oid
                 AND dependency.deptype IN ('a', 'i')
                JOIN pg_catalog.pg_class owner_table
                  ON owner_table.oid=dependency.refobjid
                JOIN pg_catalog.pg_attribute owner_column
                  ON owner_column.attrelid=owner_table.oid
                 AND owner_column.attnum=dependency.refobjsubid
                WHERE sequence_ns.nspname=%s AND sequence.relkind='S'
                ORDER BY sequence.relname
                """,
                (schema,),
            )
            sequences = tuple(
                (
                    str(row["sequence_name"]),
                    str(row["table_name"]),
                    str(row["column_name"]),
                )
                for row in cur.fetchall()
            )
        conn.rollback()
    return insert_order, baseline, sequences


def _reset_reusable_schema(schema: str) -> None:
    """Restore captured initializer rows without re-running global init locks."""

    expected = _REUSABLE_SCHEMA_FINGERPRINT
    actual = _schema_fingerprint(schema)
    if expected is None or actual != expected:
        raise RuntimeError(
            "reusable PostgreSQL test schema drifted; use fresh make_repo() "
            "for schema-mutating tests"
        )

    tables = _REUSABLE_TABLE_ORDER
    if not tables:
        raise RuntimeError("reusable PostgreSQL test baseline is empty")
    with pg_db.get_conn() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # TRUNCATE on the complete 46-table graph takes AccessExclusive
                # locks and costs several seconds even when almost every table
                # is empty. Child-first DELETE is transactionally equivalent
                # for this isolated schema and sequence state is restored below.
                for table in reversed(tables):
                    cur.execute(
                        sql.SQL("DELETE FROM {}").format(
                            sql.Identifier(schema, table)
                        )
                    )
                for table in tables:
                    columns, jsonb_columns, rows = _REUSABLE_BASELINE_ROWS[table]
                    if not rows:
                        continue
                    adapted_rows = tuple(
                        tuple(
                            Jsonb(value)
                            if column in jsonb_columns and value is not None
                            else value
                            for column, value in zip(columns, row)
                        )
                        for row in rows
                    )
                    cur.executemany(
                        sql.SQL(
                            "INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE VALUES ({})"
                        ).format(
                            sql.Identifier(schema, table),
                            sql.SQL(", ").join(map(sql.Identifier, columns)),
                            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                        ),
                        adapted_rows,
                    )
                for sequence, table, column in _REUSABLE_SEQUENCES:
                    columns, _jsonb_columns, rows = _REUSABLE_BASELINE_ROWS[table]
                    column_index = columns.index(column)
                    values = [row[column_index] for row in rows if row[column_index] is not None]
                    if values:
                        cur.execute(
                            "SELECT pg_catalog.setval(%s::regclass, %s, true)",
                            (f'"{schema}"."{sequence}"', max(values)),
                        )
                    else:
                        cur.execute(
                            "SELECT pg_catalog.setval(%s::regclass, 1, false)",
                            (f'"{schema}"."{sequence}"',),
                        )


def _foreign_key_delete_order(
    tables: list[str], edges: set[tuple[str, str]],
) -> list[str]:
    """Return child-before-parent DELETE order for one PostgreSQL schema."""

    nodes = set(tables)
    outgoing: dict[str, set[str]] = {table: set() for table in nodes}
    indegree: dict[str, int] = {table: 0 for table in nodes}
    for child, parent in edges:
        if child not in nodes or parent not in nodes or parent in outgoing[child]:
            continue
        outgoing[child].add(parent)
        indegree[parent] += 1

    ready = sorted(table for table, count in indegree.items() if count == 0)
    result: list[str] = []
    while ready:
        table = ready.pop(0)
        result.append(table)
        for parent in sorted(outgoing[table]):
            indegree[parent] -= 1
            if indegree[parent] == 0:
                ready.append(parent)
                ready.sort()
    if len(result) != len(nodes):
        cycle = sorted(nodes.difference(result))
        raise RuntimeError(
            "reusable PostgreSQL test schema has an FK cycle; use fresh "
            f"make_repo() isolation for these tables: {cycle}"
        )
    return result


def make_reusable_repo() -> _ReusableRepoHandle:
    """Return a clean repo backed by one process-local reusable PG schema.

    This is for data-only tests. Tests that alter tables, indexes, constraints,
    migrations, ownership, or pool/process topology must keep using
    :func:`make_repo`, whose fresh per-test schema remains the default.
    """

    global _REUSABLE_SCHEMA

    if rollback_active():
        return rollback_repo()

    _REUSABLE_SCHEMA_LOCK.acquire()
    saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    saved_redis_disabled = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
    saved_search_path = pg_db.get_test_search_path()
    try:
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        set_test_dsn()
        schema = _REUSABLE_SCHEMA
        created = schema is None
        if schema is None:
            schema = _create_reusable_schema()
        else:
            _activate_test_schema(schema)
        try:
            if not created:
                _reset_reusable_schema(schema)
        except BaseException:
            _drop_reusable_schema()
            raise
        cm = pg_db.get_conn()
        conn = cm.__enter__()
        return _ReusableRepoHandle(
            conn=conn,
            repo=CryptoGuardRepository(conn),
            schema=schema,
            _saved_url=saved_url,
            _saved_redis_disabled=saved_redis_disabled,
            _saved_search_path=saved_search_path,
            _cm=cm,
            _lock=_REUSABLE_SCHEMA_LOCK,
        )
    except BaseException:
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
        _REUSABLE_SCHEMA_LOCK.release()
        raise


def make_repo_for_test(
    test_case: object, *, fresh_tests: set[str] | frozenset[str],
) -> _RepoHandle | _ReusableRepoHandle:
    """Choose an explicit isolation tier for one unittest method.

    Mixed legacy classes keep a small, reviewable set of DDL/pool/process tests
    on the original fresh-schema path while data-only methods share the fast
    process-local schema. No source inspection or name heuristic runs here.
    """

    method_name = str(getattr(test_case, "_testMethodName", ""))
    if method_name in fresh_tests:
        return make_repo()
    return make_reusable_repo()


def reset_reusable_schema_manager() -> None:
    """Test-only cleanup for fixture contract and fault-injection tests."""

    _drop_reusable_schema()


# --- rollback isolation (08-08 Step 5.4; EXPLICIT OPT-IN) --------------------
#
# DEFAULT: ``make_repo()`` creates a fresh per-test schema (unchanged). A test
# marked ``@pytest.mark.rollback_isolation`` redirects ``make_repo()`` /
# ``make_reusable_repo()`` to ONE shared per-worker schema and wraps the test in
# a transaction that ``close()`` rolls back, so the expensive schema DDL runs
# once per xdist worker instead of once per test. Opting in is a contract: the
# test must not exercise COMMIT semantics, multi-connection, threads, advisory
# locks, or schema mutation/DDL (those keep the default fresh-schema isolation,
# and per 5.5 the advisory-lock/concurrency tests stay serial with their own
# DDL). The conftest autouse fixture drives ``set_rollback_active()`` from the
# marker; ``rollback_repo()`` is also callable directly for contract tests.

_ROLLBACK_SCHEMA_LOCK = threading.RLock()
_ROLLBACK_SCHEMA: str | None = None
_ROLLBACK_ACTIVE = threading.local()   # .value True only inside an opted-in test
_ROLLBACK_HANDLE = threading.local()   # .value = the open rollback handle, if any

# Dedicated per-xdist-worker pool (08-09 PoolTimeout fix). Each xdist worker is
# its own process, so these module globals ARE the per-worker state. The pool is
# created lazily on the first rollback checkout and ONLY closed at worker
# teardown -- NEVER per test -- so a worker pays ``pool.open()`` (env-tunable
# via ``pg_db._pool_open_timeout()``; default 30s production posture, P1 08-10)
# once instead of on every rollback test (the per-test ``pg_db.reset_pool()``
# churn that occasionally blew the pool-open timeout under parallel load).
_ROLLBACK_POOL_LOCK = threading.Lock()
_ROLLBACK_POOL: ConnectionPool | None = None
_ROLLBACK_POOL_DSN: str | None = None
_ROLLBACK_POOL_SCHEMA: str | None = None


def set_rollback_active(active: bool) -> None:
    """Route ``make_repo()``/``make_reusable_repo()`` at the rollback fast path."""
    _ROLLBACK_ACTIVE.value = bool(active)


def rollback_active() -> bool:
    return bool(getattr(_ROLLBACK_ACTIVE, "value", False))


def _ensure_rollback_schema() -> str:
    """Create + initialize the shared per-worker rollback schema once."""
    global _ROLLBACK_SCHEMA
    with _ROLLBACK_SCHEMA_LOCK:
        if _ROLLBACK_SCHEMA is not None:
            return _ROLLBACK_SCHEMA
        schema = _new_schema_name()
        # The initializer is routed through the GLOBAL pool via the
        # search_path override; restore that override once the schema exists so
        # later non-rollback tests in this worker never inherit the rollback
        # binding (the dedicated rollback pool carries its own).
        saved_search_path = pg_db.get_test_search_path()
        _create_scratch_schema(schema)
        try:
            _activate_test_schema(schema)
            _initialize_isolated_test_schema()
            _ROLLBACK_SCHEMA = schema
            pg_db.set_test_search_path(saved_search_path)
            pg_db.reset_pool()
            return schema
        except BaseException:
            pg_db.reset_pool()
            try:
                _drop_scratch_schema(schema)
            finally:
                pg_db.set_test_search_path(saved_search_path)
                pg_db.reset_pool()
                _ROLLBACK_SCHEMA = None
            raise


def _build_rollback_conn_kwargs_for_pool(conn: psycopg.Connection) -> None:
    """Pool configure hook: bind every rollback-pool conn to the shared schema.

    Mirrors ``pg_db._build_conn_kwargs_for_pool`` (dict_row + UTC +
    search_path + principal validation) but the search_path is FIXED to the
    rollback schema captured at pool creation - the pool is per-worker and is
    never re-routed, so the binding lives on the connections themselves instead
    of the global ``pg_db`` search-path override. Connections are left READY
    (autocommit=False, no open txn) for the pool.
    """
    conn.row_factory = dict_row
    schema = _ROLLBACK_POOL_SCHEMA
    was_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET TimeZone=UTC")
            if schema:
                safe = schema.replace("'", "''")
                cur.execute(f"SET search_path={safe}")
        pg_db._validate_connected_identity(conn)
    finally:
        conn.autocommit = was_autocommit


def _get_rollback_pool() -> ConnectionPool:
    """Return the per-xdist-worker rollback pool, opening it exactly once.

    The dedicated pool is keyed on the DSN like ``pg_db.get_pool`` but lives
    entirely in the test layer and is bound to the shared rollback schema. It
    is created lazily on the first ``rollback_repo()`` and ONLY closed at
    worker teardown (``_drop_rollback_schema``) - never per test - so
    consecutive rollback tests reuse one already-open pool instead of paying
    ``pool.open()`` (env-tunable via ``pg_db._pool_open_timeout()``; default 30s
    production posture, P1 08-10) on every test.
    """
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    global _ROLLBACK_POOL, _ROLLBACK_POOL_DSN, _ROLLBACK_POOL_SCHEMA
    with _ROLLBACK_POOL_LOCK:
        dsn = app_dsn()
        if _ROLLBACK_POOL is not None and _ROLLBACK_POOL_DSN == dsn:
            return _ROLLBACK_POOL
        if _ROLLBACK_POOL is not None:
            try:
                _ROLLBACK_POOL.close()
            except Exception:
                pass
            _ROLLBACK_POOL = None
            _ROLLBACK_POOL_DSN = None
            _ROLLBACK_POOL_SCHEMA = None
        schema = _ROLLBACK_SCHEMA
        if schema is None:
            raise RuntimeError(
                "rollback schema must be created before the rollback pool opens"
            )
        pool = None
        try:
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=8,
                timeout=30.0,
                configure=_build_rollback_conn_kwargs_for_pool,
                open=False,
            )
            # MUST assign before pool.open(): the configure hook reads
            # _ROLLBACK_POOL_SCHEMA while the pool creates its min_size
            # connections, and those connections carry the schema binding.
            _ROLLBACK_POOL = pool
            _ROLLBACK_POOL_DSN = dsn
            _ROLLBACK_POOL_SCHEMA = schema
            pool.open(wait=True, timeout=pg_db._pool_open_timeout())
        except Exception as exc:  # noqa: BLE001 - any pool/open failure is fatal
            if pool is not None:
                try:
                    pool.close()
                except Exception:
                    pass
            _ROLLBACK_POOL = None
            _ROLLBACK_POOL_DSN = None
            _ROLLBACK_POOL_SCHEMA = None
            raise pg_db.CryptoGuardDBUnavailable(
                f"rollback pool unavailable ({type(exc).__name__})"
            ) from exc
        return pool


def _close_rollback_pool() -> None:
    """Worker-teardown: close the dedicated rollback pool, if any."""
    global _ROLLBACK_POOL, _ROLLBACK_POOL_DSN, _ROLLBACK_POOL_SCHEMA
    with _ROLLBACK_POOL_LOCK:
        if _ROLLBACK_POOL is not None:
            try:
                _ROLLBACK_POOL.close()
            except Exception:
                pass
        _ROLLBACK_POOL = None
        _ROLLBACK_POOL_DSN = None
        _ROLLBACK_POOL_SCHEMA = None


def _drop_rollback_schema() -> None:
    """Drop the shared rollback schema (atexit + explicit test cleanup)."""
    global _ROLLBACK_SCHEMA
    with _ROLLBACK_SCHEMA_LOCK:
        schema = _ROLLBACK_SCHEMA
        _ROLLBACK_SCHEMA = None
        # Close the dedicated per-worker pool BEFORE dropping the schema its
        # connections are bound to.
        _close_rollback_pool()
        if schema is None:
            return
        pg_db.reset_pool()
        try:
            _drop_scratch_schema(schema)
        finally:
            if pg_db.get_test_search_path() == schema:
                pg_db.set_test_search_path(None)
            pg_db.reset_pool()


@dataclass
class _RollbackRepoHandle:
    """One checkout from the dedicated per-worker rollback pool.

    The checkout holds an outer transaction open for the whole test; repo writes
    become nested savepoints (psycopg3 ``conn.transaction()`` on an INTRANS
    connection). ``close()`` rolls the transaction back and returns the
    connection to the dedicated pool - NO ``pg_db.reset_pool()``, NO schema
    drop, NO per-test churn (the 08-09 PoolTimeout fix). The worker pool is
    closed only at worker teardown.
    """

    conn: psycopg.Connection
    repo: CryptoGuardRepository
    schema: str
    _saved_url: str | None
    _saved_redis_disabled: str | None
    _pool: ConnectionPool
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if getattr(_ROLLBACK_HANDLE, "value", None) is self:
            _ROLLBACK_HANDLE.value = None
        checkout_error: BaseException | None = None
        try:
            # Undo every write since BEGIN (incl. all nested savepoints). Roll
            # back FIRST so the pool receives an IDLE connection (putconn does
            # not re-run the configure hook; the fixed search_path binding
            # already lives on the connection).
            if self.conn.info.transaction_status != TransactionStatus.IDLE:
                self.conn.rollback()
        except BaseException as exc:
            checkout_error = exc
        try:
            self._pool.putconn(self.conn)
        except BaseException as exc:
            checkout_error = checkout_error or exc
        if self._saved_url is not None:
            os.environ["CRYPTO_GUARD_DATABASE_URL"] = self._saved_url
        else:
            os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
        if self._saved_redis_disabled is not None:
            os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = self._saved_redis_disabled
        else:
            os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
        if checkout_error is not None:
            raise checkout_error


def rollback_repo() -> _RollbackRepoHandle:
    """Explicit Step 5.4 fast path (same call shape as ``make_repo()``).

    One shared per-worker schema; the checkout's writes are rolled back in
    ``close()``. The DEFAULT independent per-test schema is UNCHANGED for
    non-opted-in tests.

    Per-worker pool (08-09 PoolTimeout fix): the checkout comes from the
    dedicated ``_ROLLBACK_POOL`` bound to the shared rollback schema. The pool
    is opened ONCE per xdist worker and NEVER reset between tests, so a worker
    pays the pool-open cost once instead of once per test (the per-test
    ``pg_db.reset_pool()`` churn behind the flake). No global search_path is
    touched - the binding lives on the pool's connections.
    """
    schema = _ensure_rollback_schema()
    saved_url = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    saved_redis_disabled = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
    try:
        os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = "1"
        set_test_dsn()
        pool = _get_rollback_pool()
        conn = pool.getconn()
        try:
            # Open the outer transaction EXPLICITLY. The pool is
            # autocommit=False but a bare connection would let the first repo
            # write auto-BEGIN its own txn and auto-COMMIT on putconn; with
            # INTRANS already set, every repo write becomes a nested savepoint
            # that close() rolls back wholesale.
            conn.execute("BEGIN")
        except BaseException:
            pool.putconn(conn)
            raise
        handle = _RollbackRepoHandle(
            conn=conn, repo=CryptoGuardRepository(conn), schema=schema,
            _saved_url=saved_url,
            _saved_redis_disabled=saved_redis_disabled,
            _pool=pool,
        )
        _ROLLBACK_HANDLE.value = handle
        return handle
    except BaseException:
        if saved_url is not None:
            os.environ["CRYPTO_GUARD_DATABASE_URL"] = saved_url
        else:
            os.environ.pop("CRYPTO_GUARD_DATABASE_URL", None)
        if saved_redis_disabled is not None:
            os.environ["CRYPTO_GUARD_REDIS_DISABLED"] = saved_redis_disabled
        else:
            os.environ.pop("CRYPTO_GUARD_REDIS_DISABLED", None)
        raise


def close_open_rollback_handle() -> None:
    """Fixture-teardown safety net: roll back a handle a failed test left open."""
    handle = getattr(_ROLLBACK_HANDLE, "value", None)
    if handle is not None:
        handle.close()


def safe_close_open_rollback_handle(primary_error: BaseException | None) -> None:
    """Teardown safety net that NEVER masks a primary test failure (P2-6).

    Rolls back a checkout a failed test left open so the next opted-in test
    still sees the clean baseline. If that cleanup itself raises while the
    test body ALREADY failed, the cleanup exception is suppressed (logged to
    stderr) so the reported failure stays the primary one. When the test body
    was clean a cleanup failure surfaces - it IS the real error.
    """
    handle = getattr(_ROLLBACK_HANDLE, "value", None)
    if handle is None:
        return
    try:
        handle.close()
    except BaseException as exc:  # noqa: BLE001 - cleanup must not mask primary
        if primary_error is not None:
            # P2 (redaction): surface only the exception TYPE, never repr(exc) —
            # a cleanup-failure body can carry DSN / connection text.
            sys.stderr.write(
                f"rollback teardown cleanup suppressed after test failure: "
                f"{type(exc).__name__}\n")
            return
        raise


# P2-6: BOTH shared schemas must be dropped at process exit. The rollback
# schema is per-worker; the reusable schema (make_reusable_repo sites) must not
# accumulate across runs either. Each drop is lock-guarded and no-ops when its
# schema was never created, so the two registrations are independent and safe.
atexit.register(_drop_rollback_schema)
atexit.register(_drop_reusable_schema)


def make_repo(*, initialize_schema: bool = True) -> _RepoHandle:
    """Create a fresh isolated PG schema + repo for one test.

    Replaces the legacy ``os.environ["CRYPTO_GUARD_DB"]=<tmp>.sqlite3`` +
    ``connect_db`` + ``initialize_database()`` setUp sequence. Each test gets
    its own schema so tests never interfere; the schema is dropped in
    ``close()``.

    Under ``@pytest.mark.rollback_isolation`` (Step 5.4 opt-in) this resolves to
    :func:`rollback_repo`: one shared per-worker schema + per-test rolled-back
    transaction. The DEFAULT fresh per-test schema is UNCHANGED.
    """
    if rollback_active():
        return rollback_repo()
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
            _initialize_isolated_test_schema()
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
    + ``search_path`` routed at the scratch schema only (no ``public`` fallback).
    Matching the pool's row shape is mandatory, not cosmetic: without
    ``dict_row`` a direct conn returns TUPLE rows, so production code that does
    ``row["col"]`` (e.g. ``acquire_service_ownership`` reading ``row["pid"]``)
    raises ``TypeError: tuple indices must be integers or slices, not str``.

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
            cur.execute(f'SET search_path TO "{schema}"')
    finally:
        conn.autocommit = was_autocommit
    return conn


__all__ = [
    "set_test_dsn",
    "scratch_schema",
    "make_repo",
    "make_reusable_repo",
    "make_repo_for_test",
    "reset_reusable_schema_manager",
    "direct_conn",
    "_RepoHandle",
    "set_rollback_active",
    "rollback_active",
    "rollback_repo",
    "close_open_rollback_handle",
    "safe_close_open_rollback_handle",
]
