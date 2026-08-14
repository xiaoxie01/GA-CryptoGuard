# -*- coding: utf-8 -*-
"""08-10 Step 3 P2: dirty-DB migration repair for entry_confirmation_events.

design.md §5.2 / implement.md Step 3. The entry_confirmation_events audit
table is brand new this deployment, so no legitimate business row can pre-exist
outside the 08-10 code. The migration must therefore repair only interrupted /
partial states, never auto-delete real rows:

  - full canonical shape (12 columns + 2 FKs) + exact UNIQUE NON-partial
    ``idx_entry_confirmation_events_fingerprint`` + lifecycle marker after a
    plain ``initialize_database()`` (baseline);
  - a partial EMPTY table (missing column, e.g. an interrupted first
    deployment) is DROPPED and recreated by the schema DDL -- columns + FKs +
    index return to the exact contract;
  - a partial table WITH rows aborts fail-closed (RuntimeError; never
    auto-delete business data) and the rows survive;
  - a missing / wrong-shaped (non-unique) fingerprint index is rebuilt to the
    exact contract (``CREATE UNIQUE INDEX IF NOT EXISTS`` is a name-only no-op
    and must NOT hide a wrong-shaped index);
  - IDENTICAL duplicate fingerprints (interrupted-batch artifact -- the live
    ``ON CONFLICT DO NOTHING`` can never produce two rows sharing a
    fingerprint, so a second identical row only exists because the UNIQUE index
    was dropped mid-run) are deduped keep-lowest-id;
  - CONFLICTING duplicates (same fingerprint, different business fields =
    tampered rows) abort fail-closed and nothing is deleted;
  - two concurrent re-initializers on a dirty schema both converge: the
    transaction-scoped advisory lock serializes them, the first repairs, the
    second sees a healthy schema and no-ops.

Every test starts from a fully initialized scratch schema (seeds + markers
present), mutates ONLY the entry_confirmation_events structure or rows, then
re-runs ``initialize_database()`` -- the exact release-path re-initialization.
The one exception is the fail-closed test, which starts from a DDL-only
(UNINITIALIZED) schema: proving "marker stays ABSENT on a failed init" requires
a schema where the marker was never written.
"""
from __future__ import annotations

import threading

import pytest

from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
    canonical_confirmation_fingerprint,
)
from plugins.crypto_guard.storage.migrations import (
    CONFIRMATION_FINGERPRINT_INDEX_NAME,
    SCHEMA_PATH,
    _confirmation_fingerprint_index_is_exact,
    _introspect_confirmation_fingerprint_index,
    initialize_database,
)
from plugins.crypto_guard.tests import pg_fixtures as fx
from plugins.crypto_guard.tests.test_pg_08_10_confirmation_lifecycle_p1 import (
    _ANALYSIS,
    _canonical_confirmation,
    _count_events,
    _persist_source_event,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_MARKER_KEY = "entry_confirmation_lifecycle_contract_v1"


class TestConfirmationEventsMigration:
    """design.md §5.2: dirty-DB upgrade/repair for entry_confirmation_events."""

    # ── helpers ──────────────────────────────────────────────────────────────

    def _open_schema(self):
        """UNINITIALIZED schema: the exact greenfield DDL applied, but NO seeds
        or markers (``initialize_database`` never ran). Used by the fail-closed
        test so the 08-10 marker is provably ABSENT before any healthy init."""
        h = fx.make_repo(initialize_schema=False)
        try:
            schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
            with h.conn.cursor() as cur:
                cur.execute(schema_sql)
            h.conn.commit()
        except BaseException:
            h.conn.rollback()
            raise
        return h

    def _marker_present(self, h) -> bool:
        with h.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM _migration_state WHERE key=%s", (_MARKER_KEY,)
            )
            return cur.fetchone() is not None

    def _index_facts(self, h):
        with h.conn.cursor() as cur:
            return _introspect_confirmation_fingerprint_index(cur, h.schema)

    def _event_columns(self, h) -> set[str]:
        with h.conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'entry_confirmation_events'"
            )
            return {r["column_name"] for r in cur.fetchall()}

    def _event_foreign_keys(self, h) -> set[str]:
        with h.conn.cursor() as cur:
            cur.execute(
                """
                SELECT con.conname FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE ns.nspname = %s AND cls.relname = 'entry_confirmation_events'
                  AND con.contype = 'f'
                """,
                (h.schema,),
            )
            return {r["conname"] for r in cur.fetchall()}

    def _drop_fingerprint_index(self, h) -> None:
        with h.conn.cursor() as cur:
            cur.execute(f"DROP INDEX {CONFIRMATION_FINGERPRINT_INDEX_NAME}")
        h.conn.commit()

    def _sql_insert_event_copy(self, h, *, override_source: str | None = None) -> None:
        """Clone the first event row via raw SQL (requires the UNIQUE index to
        be absent, otherwise the copy collides with the live ON CONFLICT path).

        ``override_source`` produces a DIFFERENT business field (source is NOT
        covered by the fingerprint) while keeping the fingerprint identical --
        the exact "re-observation via another module" case the live insert
        dedupes, and therefore a tamper signature when it appears as a second
        row sharing one fingerprint.
        """
        source_expr = "%s" if override_source is not None else "source"
        params = (override_source,) if override_source is not None else ()
        with h.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO entry_confirmation_events
                    (symbol, side, event_type, timeframe, direction,
                     event_close_time, event_price, source,
                     source_snapshot_id, source_decision_id, event_fingerprint)
                SELECT symbol, side, event_type, timeframe, direction,
                       event_close_time, event_price, {source_expr},
                       source_snapshot_id, source_decision_id, event_fingerprint
                FROM entry_confirmation_events
                ORDER BY id LIMIT 1
                """,
                params,
            )
        h.conn.commit()

    # ── tests ────────────────────────────────────────────────────────────────

    def test_full_shape_and_marker_after_initialize(self) -> None:
        """Baseline: the canonical table + index + FKs + marker all exist."""
        h = fx.make_repo()
        try:
            columns = self._event_columns(h)
            assert {
                "id", "symbol", "side", "event_type", "timeframe", "direction",
                "event_close_time", "event_price", "source",
                "source_snapshot_id", "source_decision_id",
                "event_fingerprint", "created_at",
            } <= columns, columns
            assert len(self._event_foreign_keys(h)) == 2
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()

    def test_missing_column_empty_table_recreated(self) -> None:
        """A partial EMPTY table (interrupted deployment) is dropped and
        recreated to the exact canonical shape by the schema DDL."""
        h = fx.make_repo()
        try:
            with h.conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE entry_confirmation_events "
                    "DROP COLUMN event_fingerprint"
                )
            h.conn.commit()
            # pre-health fails (missing column + auto-dropped index) -> the
            # DDL branch runs the 08-10 migration, which drops the empty table
            # so schema_postgres.sql recreates columns + FKs + index.
            result = initialize_database()
            assert result["ok"], result
            assert "event_fingerprint" in self._event_columns(h)
            assert len(self._event_foreign_keys(h)) == 2
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()

    def test_partial_table_with_rows_aborts_fail_closed(self) -> None:
        """A partial table that already holds rows NEVER gets auto-deleted:
        the migration raises and the whole init transaction rolls back --
        the row survives, the partial shape stays, and the lifecycle marker is
        NOT written (proves "marker registered only after health succeeds":
        the marker is the LAST write, after the health gate, so a failed init
        must leave it absent)."""
        h = self._open_schema()  # UNINITIALIZED: schema DDL only, no markers
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            assert _count_events(h) == 1
            with h.conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE entry_confirmation_events "
                    "DROP COLUMN event_fingerprint"
                )
            h.conn.commit()
            with pytest.raises(RuntimeError, match="refusing to auto-delete"):
                initialize_database()
            # fail-closed: the row survives, the partial shape stays, and the
            # whole init transaction (schema repair + seeds + markers) rolled
            # back together -- the lifecycle marker is absent.
            assert _count_events(h) == 1
            assert "event_fingerprint" not in self._event_columns(h)
            assert not self._marker_present(h)
            # After the schema is repaired the SAME re-init succeeds and writes
            # the marker (proves the marker gate is health-gated, not absent
            # permanently). Repair = restore the NOT NULL column with the
            # canonical fingerprint (the row must keep its data; the unique
            # index is gone, so the re-init rebuilds it exactly).
            with h.conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE entry_confirmation_events "
                    "ADD COLUMN event_fingerprint TEXT"
                )
                cur.execute(
                    "UPDATE entry_confirmation_events SET event_fingerprint = %s",
                    (canonical_confirmation_fingerprint(conf),),
                )
                cur.execute(
                    "ALTER TABLE entry_confirmation_events "
                    "ALTER COLUMN event_fingerprint SET NOT NULL"
                )
            h.conn.commit()
            result = initialize_database()
            assert result["ok"], result
            assert self._marker_present(h)
        finally:
            h.close()

    def test_missing_index_rebuilt(self) -> None:
        """A missing fingerprint index is rebuilt to the exact contract."""
        h = fx.make_repo()
        try:
            self._drop_fingerprint_index(h)
            assert self._index_facts(h) is None
            result = initialize_database()
            assert result["ok"], result
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()

    def test_nonunique_index_rebuilt_exact(self) -> None:
        """A same-name NON-unique index is a wrong-shaped index: the migration
        must introspect pg_index and rebuild it UNIQUE (IF NOT EXISTS is a
        name-only no-op and would silently keep the wrong index)."""
        h = fx.make_repo()
        try:
            self._drop_fingerprint_index(h)
            with h.conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE INDEX {CONFIRMATION_FINGERPRINT_INDEX_NAME}
                        ON entry_confirmation_events(event_fingerprint)
                    """
                )
            h.conn.commit()
            facts = self._index_facts(h)
            assert facts is not None and not facts["indisunique"]
            result = initialize_database()
            assert result["ok"], result
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()

    def test_identical_duplicates_deduped_keep_lowest_id(self) -> None:
        """IDENTICAL duplicate fingerprints (interrupted-batch artifact) are
        deduped keep-lowest-id so the unique index can be rebuilt."""
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            self._drop_fingerprint_index(h)
            # Without the UNIQUE index the raw SQL copy creates a second row
            # with the same fingerprint AND the same business fields.
            self._sql_insert_event_copy(h)
            assert _count_events(h) == 2
            result = initialize_database()
            assert result["ok"], result
            assert _count_events(h) == 1
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()

    def test_conflicting_duplicates_abort_fail_closed(self) -> None:
        """CONFLICTING duplicates (same fingerprint, different business fields)
        are corruption -- the live ON CONFLICT path can never produce them --
        so the migration aborts and deletes nothing."""
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            self._drop_fingerprint_index(h)
            # Same fingerprint (source is NOT a fingerprint field) but a
            # different ``source`` business field -> 2 DISTINCT variants.
            self._sql_insert_event_copy(h, override_source="smc")
            assert _count_events(h) == 2
            with pytest.raises(RuntimeError, match="conflicting duplicate"):
                initialize_database()
            # fail-closed: both rows survive, nothing was deleted.
            assert _count_events(h) == 2
        finally:
            h.close()

    def test_concurrent_reinit_converges_on_dirty_schema(self) -> None:
        """Two concurrent re-initializers on a dirty schema (missing index)
        both succeed: the transaction-scoped advisory lock serializes them --
        the first repairs the schema, the second sees a healthy schema and
        applies only idempotent no-ops."""
        h = fx.make_repo()
        try:
            self._drop_fingerprint_index(h)
            results: dict[str, object] = {}
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def _run(label: str) -> None:
                try:
                    barrier.wait(timeout=30)
                    results[label] = initialize_database()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=_run, args=("a",), name="reinit-a")
            t2 = threading.Thread(target=_run, args=("b",), name="reinit-b")
            t1.start()
            t2.start()
            t1.join(timeout=120)
            t2.join(timeout=120)

            assert errors == [], f"concurrent re-init raised: {errors}"
            assert len(results) == 2, results
            ok, reason = _confirmation_fingerprint_index_is_exact(
                self._index_facts(h))
            assert ok, reason
            assert self._marker_present(h)
        finally:
            h.close()
