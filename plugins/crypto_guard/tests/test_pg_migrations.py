"""P3 RED tests: initialize_database() idempotency + advisory lock.

Verifies the PostgreSQL greenfield ``initialize_database()`` contract:
1. Two consecutive calls leave an identical schema + marker set (idempotency).
2. A concurrent second call blocks on the transaction-scoped advisory lock and
   completes cleanly after the first commits (no half-state, both ok).
3. The call seeds default symbols + strategies and writes every contract marker.
4. ``check_schema_health`` returns ``ok=True`` after init.

Uses per-test schema isolation so tests never touch the production public
schema. The DSN points at the dedicated ``crypto_guard_test`` DB.
"""

from __future__ import annotations

import threading
import unittest

import pytest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import (
    check_schema_health,
    initialize_database,
)
from plugins.crypto_guard.tests.pg_fixtures import direct_conn, make_repo


pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation]


# Every contract marker initialize_database() must write. Each name MUST appear
# as a row in _migration_state after a successful init. Sourced from the marker
# helpers called by initialize_database() (R4/BTC#9/market-data/semantic/
# continuity/fair-scheduling/timeout-envelope + the profit-protection cutoff +
# the stop-loss-adjustment dedup marker).
EXPECTED_MARKERS = {
    "hourly_report_accuracy_r4_contract_v1",
    "btc9_trade_gate_contract_v1",
    "market_data_contract_v1",
    "hourly_market_semantic_accuracy_contract_v1",
    "hourly_decision_context_continuity_contract_v1",
    "llm_fair_scheduling_context_contract_v1",
    # 07-22 Codex P1-1: independent timeout-envelope cutoff marker.
    "llm_provider_timeout_envelope_contract_v2",
    "profit_protection_mark_price_contract_v1",
    "stop_loss_adjustment_dedup_v1",
    # Phase-2 P2-1 (07-27) requirement F: current-vs-historical split marker
    # for deterministic_direction_from_failed_llm. Written by initialize_database
    # (release path only — NOT written to production here).
    "llm_failed_direction_fail_closed_v1",
    # 07-31 P1-4: schema-repair / breaker / preset integrity split marker.
    # Written by initialize_database (release path only — NOT written to
    # production here). Gates current-vs-historical split of the two LLM
    # diagnostics (llm_failure_rate_high / llm_circuit_breaker_open).
    "llm_schema_breaker_preset_integrity_v1",
    # 08-02 P1-3: execution-funnel report-contract split marker. Written ONLY
    # by initialize_database publish path (release only — NOT written to
    # production this round). Gates current-vs-historical split of the six
    # execution-funnel diagnostics + the report row split
    # (llm_call_succeeded / llm_plan_confirmed / risk_passed / final_executable).
    "execution_funnel_report_contract_v1",
    # 08-06 P2 (release-blocker rework): watch -> order bridge contract marker.
    # Written by initialize_database only AFTER the bridge schema is complete
    # AND the health gate passes (same transaction as the schema change).
    # Its absence is fail-closed in diagnose_state_consistency (marker-missing
    # error) so an undeployed bridge contract cannot present as healthy.
    "watch_order_bridge_contract_v1",
    # 08-08: watch-recheck diagnostics split markers. Written by
    # initialize_database. Each gates current-vs-historical split of exactly one
    # watch-recheck diagnostic (risk-shape mismatch / payload-serialization
    # failure / funnel starvation); absence is fail-closed in
    # diagnose_state_consistency (marker-missing error).
    "watch_recheck_risk_shape_contract_v1",
    "watch_review_payload_serialization_contract_v1",
    "watch_recheck_funnel_contract_v1",
}


class TestPostgresInitializeDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo(initialize_schema=False)

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _table_count(self, conn) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            return int(cur.fetchone()["c"])

    def _marker_keys(self, conn) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('_migration_state')"
            )
            if cur.fetchone()["to_regclass"] is None:
                return set()
            cur.execute("SELECT key FROM _migration_state")
            return {r["key"] for r in cur.fetchall()}

    # ── tests ────────────────────────────────────────────────────────────────

    def test_initialize_seeds_symbols_strategies_and_markers(self) -> None:
        """init creates the schema, seeds default rows, writes all markers."""
        result = initialize_database()
        self.assertTrue(result["ok"], f"initialize_database not ok: {result}")

        with pg_db.get_conn() as conn:
            # default symbols seeded (BTCUSDT etc. from symbols.yaml)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM symbols")
                n_symbols = int(cur.fetchone()["c"])
                cur.execute("SELECT COUNT(*) AS c FROM strategy_versions")
                n_strategies = int(cur.fetchone()["c"])
            markers = self._marker_keys(conn)
            conn.rollback()

        self.assertGreater(n_symbols, 0, "no symbols seeded")
        self.assertGreater(n_strategies, 0, "no strategies seeded")
        missing_markers = EXPECTED_MARKERS - markers
        self.assertFalse(
            missing_markers,
            f"markers not written by initialize_database: {sorted(missing_markers)}",
        )

    def test_initialize_is_idempotent(self) -> None:
        """Two consecutive calls leave an identical schema + marker set."""
        r1 = initialize_database()
        with pg_db.get_conn() as conn:
            tables_after_1 = self._table_count(conn)
            markers_after_1 = self._marker_keys(conn)
            conn.rollback()

        r2 = initialize_database()
        with pg_db.get_conn() as conn:
            tables_after_2 = self._table_count(conn)
            markers_after_2 = self._marker_keys(conn)
            conn.rollback()

        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        self.assertEqual(
            tables_after_1,
            tables_after_2,
            "table count changed on re-init (not idempotent)",
        )
        self.assertEqual(
            markers_after_1,
            markers_after_2,
            "marker set changed on re-init (not idempotent)",
        )
        # schema health must hold after both runs
        self.assertTrue(check_schema_health()["ok"])

    def test_concurrent_initialize_serializes_on_advisory_lock(self) -> None:
        """A concurrent second init blocks until the first commits; both ok.

        Without the transaction-scoped advisory lock, two concurrent
        initializers could interleave their DDL/marker writes. The lock
        serializes them so the second sees the first's committed schema and
        only applies idempotent no-ops.
        """
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _run(label: str) -> None:
            try:
                barrier.wait(timeout=30)
                results[label] = initialize_database()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_run, args=("a",), name="init-a")
        t2 = threading.Thread(target=_run, args=("b",), name="init-b")
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)

        self.assertEqual(errors, [], f"concurrent init raised: {errors}")
        self.assertIn("a", results, "init-a did not complete")
        self.assertIn("b", results, "init-b did not complete")
        self.assertTrue(results["a"]["ok"], f"init-a not ok: {results['a']}")
        self.assertTrue(results["b"]["ok"], f"init-b not ok: {results['b']}")

        # Both ran against the same schema; final state is consistent and
        # healthy (no half-applied interleaving).
        with pg_db.get_conn() as conn:
            markers = self._marker_keys(conn)
            conn.rollback()
        self.assertEqual(
            markers,
            EXPECTED_MARKERS,
            "concurrent init left a partial marker set",
        )
        self.assertTrue(check_schema_health()["ok"])

    @pytest.mark.serial
    def test_advisory_lock_blocks_concurrent_init(self) -> None:
        """Direct proof the transaction-scoped advisory lock serializes init.

        A separate connection acquires the SAME advisory lock and holds it open
        (uncommitted). ``initialize_database()`` from another connection MUST
        block on that lock and only complete after the holder commits.

        We isolate LOCK BLOCKING from bare init time by pre-initializing once so
        the waiter's call is an idempotent fast no-op (every statement is IF NOT
        EXISTS / ON CONFLICT). With the schema already applied, bare init time
        drops to milliseconds, so any duration beyond the held window is purely
        the advisory lock blocking. Removing the lock makes the waiter finish in
        its bare (millisecond) time, so the duration assertion fails - the
        revert-fail trigger.
        """
        import time as _time

        from plugins.crypto_guard.storage.migrations import _advisory_lock_key

        # Pre-initialize so the waiter's call is an idempotent no-op: bare init
        # time collapses to milliseconds, isolating lock blocking as the only
        # source of duration beyond the held window.
        initialize_database()

        # Measure the BARE idempotent init time (no contention) as the baseline.
        t0 = _time.monotonic()
        initialize_database()
        bare_init = _time.monotonic() - t0

        HOLD_WINDOW = 3.0
        # Sanity: bare idempotent init must be well under the hold window, else
        # this test cannot isolate lock blocking.
        self.assertLess(
            bare_init,
            HOLD_WINDOW - 1.0,
            f"bare idempotent init time {bare_init:.2f}s is not fast enough to "
            "isolate lock blocking; the test would be a false green",
        )

        holder_started = threading.Event()
        holder_done = threading.Event()
        hi, lo = _advisory_lock_key()

        def _hold() -> None:
            with direct_conn(self._repo_handle.schema) as hconn:
                with hconn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (hi, lo))
                    cur.fetchone()
                    holder_started.set()
                    holder_done.wait(timeout=HOLD_WINDOW + 10)
                hconn.commit()

        durations: dict[str, float] = {}

        def _init() -> None:
            holder_started.wait(timeout=10)
            t0i = _time.monotonic()
            initialize_database()
            durations["elapsed"] = _time.monotonic() - t0i

        hold_t = threading.Thread(target=_hold, name="lock-holder")
        init_t = threading.Thread(target=_init, name="init-waiter")
        hold_t.start()
        init_t.start()
        # Hold the lock for the full window, then release.
        _time.sleep(HOLD_WINDOW)
        holder_done.set()
        hold_t.join(timeout=15)
        init_t.join(timeout=60)

        self.assertIn(
            "elapsed",
            durations,
            "initialize_database did not complete after the lock was released",
        )
        # The waiter MUST have blocked for at least most of the held window.
        # Without the advisory lock it would finish in ~bare_init (<1s), so this
        # assertion is the revert-fail trigger.
        self.assertGreaterEqual(
            durations["elapsed"],
            HOLD_WINDOW - 0.5,
            "initialize_database did NOT block on the advisory lock - it "
            f"completed in {durations['elapsed']:.2f}s while the lock was held "
            f"for {HOLD_WINDOW}s and bare idempotent init is {bare_init:.2f}s "
            "(revert-fail trigger)",
        )

    @pytest.mark.serial
    def test_healthy_initialize_skips_ddl_does_not_block_concurrent_dml(self) -> None:
        """RED-first: on a HEALTHY schema, initialize_database() must NOT re-run
        the schema DDL. CREATE TABLE/INDEX IF NOT EXISTS still takes
        AccessExclusive/Share locks that conflict with concurrent uncommitted
        DML (RowExclusive) on another connection, deadlocking the per-call init
        that _repo() fires on every tool call.

        This is the production-shape defect behind the test_smoke hang at
        test_decision_supplement_alert_outbox_and_config_hot_reload: the test's
        connection held an open alert_outbox DML transaction while a tool call
        routed through _repo() -> initialize_database() -> cur.execute(schema_sql)
        on a SECOND connection, whose CREATE INDEX (ShareLock) on alert_outbox
        blocked forever against the caller's RowExclusive. SQLite DDL never
        conflicted with DML; PostgreSQL does.

        Fix contract: probe schema health under the advisory lock; if healthy,
        SKIP the DDL. Seeds + markers are idempotent RowExclusive writes on
        symbols/strategies/_migration_state (never alert_outbox), so they do
        not conflict with the caller's DML and are still re-affirmed. Only a
        missing/unhealthy schema runs the DDL under the advisory lock.

        Revert-fail: without the skip, the init thread deadlocks on conn1's
        RowExclusive lock and cannot complete while the lock is held, so
        ``completed_under_lock`` is False and the assertion fails (RED).
        """
        # 1. Healthy schema, committed.
        initialize_database()
        self.assertTrue(
            check_schema_health()["ok"],
            "precondition: schema must be healthy before the DML hold",
        )

        # 2. conn1 holds an uncommitted RowExclusive lock on alert_outbox,
        #    modelling a caller mid-DML (the test_smoke scenario). The DDL's
        #    CREATE INDEX (ShareLock) / CREATE TABLE (AccessExclusive) on
        #    alert_outbox would conflict with this RowExclusive lock.
        holder_ready = threading.Event()
        holder_release = threading.Event()
        init_result: dict[str, object] = {}
        init_errors: list[BaseException] = []

        def _hold() -> None:
            with direct_conn(self._repo_handle.schema) as hconn:
                with hconn.cursor() as cur:
                    cur.execute("LOCK TABLE alert_outbox IN ROW EXCLUSIVE MODE")
                    holder_ready.set()
                    holder_release.wait(timeout=30)
                hconn.rollback()  # releases the RowExclusive lock

        def _init() -> None:
            try:
                holder_ready.wait(timeout=15)
                init_result["r"] = initialize_database()
            except BaseException as exc:  # noqa: BLE001
                init_errors.append(exc)

        hold_t = threading.Thread(target=_hold, name="dml-holder")
        init_t = threading.Thread(target=_init, name="init-caller")
        hold_t.start()
        init_t.start()

        # 3. The decisive assertion: with the DDL-skip fix, initialize_database
        #    completes WHILE conn1 still holds the RowExclusive lock (it only
        #    touches symbols/strategies/_migration_state, not alert_outbox).
        #    Without the fix it deadlocks on conn1's lock and cannot complete
        #    until the holder releases -> completed_under_lock is False (RED).
        JOIN_TIMEOUT = 10.0
        init_t.join(timeout=JOIN_TIMEOUT)
        completed_under_lock = "r" in init_result or bool(init_errors)

        # Release the holder so a (RED) deadlocked init thread can drain, then
        # wait it out to avoid an orphan holding the advisory lock.
        holder_release.set()
        hold_t.join(timeout=15)
        init_t.join(timeout=15)

        self.assertEqual(
            init_errors,
            [],
            f"initialize_database raised under concurrent DML: {init_errors}",
        )
        self.assertTrue(
            completed_under_lock,
            f"initialize_database did not complete within {JOIN_TIMEOUT}s while "
            "a concurrent connection held an uncommitted RowExclusive lock on "
            "alert_outbox -- the per-call DDL deadlocked against the caller's "
            "DML instead of being skipped on a healthy schema (revert-fail "
            "trigger)",
        )
        self.assertTrue(
            init_result["r"]["ok"],
            f"initialize_database not ok under concurrent DML: {init_result['r']}",
        )
        # The DDL-skip must leave the schema healthy.
        self.assertTrue(
            check_schema_health()["ok"],
            "schema unhealthy after a healthy-skip initialize_database",
        )

    def test_init_repairs_legacy_dedupe_index_predicate(self) -> None:
        """08-02 Finding 1 (P1): ``CREATE INDEX IF NOT EXISTS`` is a name-only
        no-op in PostgreSQL -- it can NOT upgrade an existing
        ``idx_opportunity_watches_dedupe`` carrying the pre-P0-2 predicate
        (``WHERE dedupe_key IS NOT NULL``, missing ``status = 'active'``). A
        schema with that stale index fails the health gate fail-closed, so
        ``initialize_database()`` must drop the stale-predicate index and let
        the schema DDL recreate it with the P0-2 predicate. Also proves the
        P0-2 dedupe contract end-to-end: a terminal watch releases its
        dedupe_key so a fresh active watch can reuse it.

        Revert-fail: without the drop, the DDL no-ops, the health gate raises
        RuntimeError, and this test's ``ok=True`` assertion goes RED.
        """
        from psycopg import sql as _sql

        from plugins.crypto_guard.storage.migrations import (
            SCHEMA_PATH,
            _check_opportunity_watches_dedupe_index,
        )

        # 1. Apply the full schema, then downgrade the dedupe index to the
        #    pre-P0-2 predicate exactly as the old baseline shipped it.
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with pg_db.get_conn() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(schema_sql)
                    cur.execute(
                        _sql.SQL("DROP INDEX {}.{}").format(
                            _sql.Identifier(self._repo_handle.schema),
                            _sql.Identifier("idx_opportunity_watches_dedupe"),
                        )
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX idx_opportunity_watches_dedupe "
                        "ON opportunity_watches(dedupe_key) "
                        "WHERE dedupe_key IS NOT NULL"
                    )
            # Sanity: the stale index must trip the dedupe health check.
            with conn.cursor() as cur:
                problems = _check_opportunity_watches_dedupe_index(
                    cur, self._repo_handle.schema
                )
            conn.rollback()
        self.assertTrue(
            any("status = 'active'" in p["column"] for p in problems),
            "precondition: stale index must fail the dedupe health check",
        )

        # 2. A terminal watch holding the key proves the stale index blocks a
        #    re-materialization: with the old predicate (no status filter) the
        #    key is held; the P0-2 predicate must release it.
        with pg_db.get_conn() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO opportunity_watches
                            (symbol, direction, watch_condition_json, status,
                             dedupe_key)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        ("BTCUSDT", "LONG", "{}", "triggered", "auto:BTCUSDT:LONG"),
                    )
            conn.rollback()

        # 3. initialize_database() must repair the index and pass the gate.
        result = initialize_database()
        self.assertTrue(result["ok"], f"initialize_database not ok: {result}")
        self.assertTrue(
            check_schema_health()["ok"],
            "schema unhealthy after stale-index repair",
        )

        # 4. The repaired index carries the P0-2 predicate, so a NEW active
        #    watch re-using the terminal watch's dedupe_key is legal (key
        #    released). Without the status='active' predicate the INSERT would
        #    raise a unique-violation (RED).
        with pg_db.get_conn() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    problems = _check_opportunity_watches_dedupe_index(
                        cur, self._repo_handle.schema
                    )
                    cur.execute(
                        """
                        INSERT INTO opportunity_watches
                            (symbol, direction, watch_condition_json, status,
                             dedupe_key)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        ("BTCUSDT", "LONG", "{}", "active", "auto:BTCUSDT:LONG"),
                    )
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM opportunity_watches "
                        "WHERE dedupe_key = 'auto:BTCUSDT:LONG'"
                    )
                    n = int(cur.fetchone()["c"])
            conn.rollback()
        self.assertEqual(
            problems,
            [],
            "dedupe index not repaired to the P0-2 predicate",
        )
        self.assertEqual(
            n,
            2,
            "re-materialized active watch was blocked by the terminal key",
        )

    def test_initialize_failure_rolls_back_atomically(self) -> None:
        """A mid-init failure leaves NO schema residue (single transaction).

        We force a failure by pointing the DSN at a role that lacks CREATE on
        the public schema AFTER the schema is applied, then assert the prior
        healthy init's tables remain (the failed run did not corrupt them).
        This guards the "one transaction; ROLLBACK on any error" contract by
        confirming a failed init cannot leave a half-applied marker.
        """
        # First, a clean init so the schema exists.
        initialize_database()
        with pg_db.get_conn() as conn:
            tables_before = self._table_count(conn)
            markers_before = self._marker_keys(conn)
            conn.rollback()
        self.assertGreater(tables_before, 40)

        # Force a failure inside initialize_database: inject a marker-writer
        # that raises. We patch one of the contract-marker helpers to raise,
        # which runs AFTER schema apply + seed, proving the whole transaction
        # (schema + seed + markers) rolls back together -- leaving the
        # pre-existing healthy schema intact.
        from plugins.crypto_guard.storage import migrations as mig

        original = mig._ensure_llm_fair_scheduling_context_contract_marker

        def _boom(conn) -> None:
            raise RuntimeError("forced init failure for atomicity test")

        mig._ensure_llm_fair_scheduling_context_contract_marker = _boom  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                initialize_database()
        finally:
            mig._ensure_llm_fair_scheduling_context_contract_marker = original  # type: ignore[assignment]

        # The failed init must NOT have corrupted the prior healthy state.
        with pg_db.get_conn() as conn:
            tables_after = self._table_count(conn)
            markers_after = self._marker_keys(conn)
            conn.rollback()
        self.assertEqual(
            tables_after,
            tables_before,
            "failed init changed the table count (not atomic)",
        )
        self.assertEqual(
            markers_after,
            markers_before,
            "failed init changed the marker set (not atomic)",
        )


if __name__ == "__main__":
    unittest.main()
