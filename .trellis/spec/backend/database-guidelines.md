# Database Guidelines

> PostgreSQL persistence conventions for CryptoGuard.

## Engine And Identity

- CryptoGuard is PostgreSQL-only. Production code must not import `sqlite3`,
  open SQLite files, dual-write, or provide a SQLite fallback.
- Use psycopg 3 and `psycopg_pool` through
  `plugins.crypto_guard.storage.pg_db`.
- Runtime identity is exactly `crypto_guard_app` on database `crypto_guard`.
  Tests use exactly `crypto_guard_test_app` on `crypto_guard_test`.
- Read the DSN only from `CRYPTO_GUARD_DATABASE_URL`. Never log or return the
  raw DSN. Use `database_identity()` for operator output.

## Connections And Transactions

- Rows use `psycopg.rows.dict_row`; access columns by name.
- `pg_db.get_conn()` owns one unit of work: commit an open clean transaction
  on exit, roll back an exceptional or aborted transaction, then return a
  READY connection to the pool.
- Use `pg_db.transaction()` for explicit multi-statement atomic groups.
- Repository write methods use `conn.transaction()`. Inside an already-open
  transaction this is a savepoint, so the caller retains the outer commit.
- Catching `UniqueViolation` requires `pg_db.savepoint()` or `ON CONFLICT`.
  Never catch a PostgreSQL statement error and continue without rollback.

## Query Patterns

- Parameters use `%s`. Do not add a runtime `?` to `%s` translator.
- Inserts that return an identity use `RETURNING id`.
- Idempotent writes use `ON CONFLICT DO NOTHING` or an explicit conflict-target
  update.
- JSON data uses JSONB operators and psycopg's decoded dict/list row shape.
- Queue and batch claims use row locks with `FOR UPDATE SKIP LOCKED`, followed
  by ownership-token and exact-set validation in the same transaction.

## Schema And Health

- Greenfield DDL lives in `storage/schema_postgres.sql`.
- `initialize_database()` runs under a transaction-scoped PostgreSQL advisory
  lock. DDL, seeds, health checks, and contract markers are atomic and
  idempotent.
- `check_schema_health()` resolves `current_schema()` and verifies every
  required table plus required columns, indexes, and constraints. A query
  failure or missing object is unhealthy, never an empty-success result.

## Test Isolation

- PostgreSQL tests use a unique scratch schema per test through
  `tests/pg_fixtures.py`; never drop or recreate shared `public`.
- Concurrency tests use independent PostgreSQL backends routed to the same
  scratch schema.
- Admin credentials are bootstrap-only process environment values and must not
  appear in source, tests, logs, task documents, or command output.

## Common Mistakes

- Returning a pooled connection while it is `INTRANS` or `INERROR`.
- Assuming a nested `conn.transaction()` commits an outer transaction opened
  by an earlier SELECT.
- Catching SQL exceptions in diagnostics and returning an empty issue list.
- Printing `config.database_url` or exception text that contains a DSN.
- Testing against shared `public`, which makes repeated and concurrent test
  runs interfere with each other.
