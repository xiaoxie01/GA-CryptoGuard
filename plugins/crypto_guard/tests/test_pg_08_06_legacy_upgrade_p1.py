"""P1/P2 RED tests: legacy pre-08-04 production schema auto-upgrade.

Release-blocker rework (08-06). A real pre-08-04 production schema already has
``paper_orders`` / ``opportunity_watches`` (created by the greenfield cutover),
so the schema DDL's ``CREATE TABLE IF NOT EXISTS`` no-ops on them and the
standalone ``CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_trigger_watch_once
... WHERE trigger_watch_id ...`` raises UndefinedColumn (42703). ``initialize_database``
MUST therefore auto-run the additive 08-04 bridge migration first, inside the
SAME advisory-lock-guarded transaction, before the schema DDL.

These tests build the faithful pre-08-04 catalog from
``fixtures/schema_pre_08_04.sql`` (extracted verbatim from commit 6912c0d, the
last pre-08-04 commit touching the schema) and then call ``initialize_database()``
ONCE - they never call ``apply_08_04_watch_order_bridge_migration`` (requirement 2).

The ten requirements from the release-blocker directive:
  R1  legacy schema + a single initialize_database() succeeds (no 42703) and
      yields the 4 bridge columns + index + full marker set.
  R2  the test never pre-calls the migration helper.
  R3  reverting the auto-wiring reproduces the 42703 release blocker
      (fail-closed RuntimeError), with no residue.
  R4  fresh greenfield init still succeeds (helper safe no-ops there).
  R5  upgraded-schema second init is an idempotent no-op.
  R6  a mid-bridge failure rolls back columns/index/marker/seed atomically.
  R7  advisory-lock concurrent init produces no half-migration.
  R8  the bridge marker appears only after the bridge schema is complete.
  R9  check_schema_health verifies the 4 columns + unique partial index with
      the correct predicate.
  R10 legacy business rows (symbol/signal/paper_order/watch) survive the
      upgrade with counts + associations preserved.

``initialize_database()`` here is the ``allow_ddl=False`` test-owner call: it
connects via the pool (never the production migrator DSN, which is forbidden)
and enters the IDENTICAL DDL branch because ``is_test_owner`` is true. The
auto-wiring under test is the same code path production runs with
``allow_ddl=True`` (the only differences are the migrator role grant and the
migrator-only ``_grant_runtime_privileges`` call).
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from plugins.crypto_guard.storage import migrations as mig
from plugins.crypto_guard.storage.migrations import (
    check_schema_health,
    initialize_database,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_migrations import EXPECTED_MARKERS


pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation]

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
LEGACY_SCHEMA_SQL = (_FIXTURES_DIR / "schema_pre_08_04.sql").read_text(encoding="utf-8")


class TestLegacy08_04AutoUpgrade(unittest.TestCase):
    def setUp(self) -> None:
        # Fresh isolated scratch schema, NO schema applied yet.
        self._h = make_repo(initialize_schema=False)

    def tearDown(self) -> None:
        self._h.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _apply_legacy_schema(self) -> None:
        """Build the faithful pre-08-04 production catalog in the scratch schema."""
        with self._h.conn.cursor() as cur:
            cur.execute(LEGACY_SCHEMA_SQL)
        self._h.conn.commit()

    def _seed_legacy_rows(self) -> tuple[int, int, int, int]:
        """Insert legacy business rows; return (symbol_id, signal_id, watch_id, order_id)."""
        with self._h.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO symbols(symbol, base_asset) VALUES ('TESTUSDT','TEST') RETURNING id"
            )
            sym_id = int(cur.fetchone()["id"])
            cur.execute(
                "INSERT INTO signals(symbol, direction, strategy_name, status) "
                "VALUES ('TESTUSDT','long','momentum_v1','created') RETURNING id"
            )
            sig_id = int(cur.fetchone()["id"])
            cur.execute(
                "INSERT INTO opportunity_watches(symbol, direction, watch_condition_json, status) "
                "VALUES ('TESTUSDT','long',%s,'active') RETURNING id",
                (Jsonb({"condition": "test"}),),
            )
            watch_id = int(cur.fetchone()["id"])
            cur.execute(
                "INSERT INTO paper_orders(signal_id, symbol, side, order_type, status, risk_check_passed) "
                "VALUES (%s,'TESTUSDT','buy','limit','pending',true) RETURNING id",
                (sig_id,),
            )
            po_id = int(cur.fetchone()["id"])
        self._h.conn.commit()
        return sym_id, sig_id, watch_id, po_id

    def _bridge_columns_present(self) -> dict[str, set[str] | bool]:
        """Return which 08-04 bridge columns / index exist in the scratch schema."""
        with self._h.conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='paper_orders' "
                "AND column_name='trigger_watch_id'"
            )
            has_trigger = cur.fetchone() is not None
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='opportunity_watches' "
                "AND column_name=ANY(%s)",
                (["recheck_status", "recheck_order_id", "last_recheck_at"],),
            )
            opportunity_cols = {r["column_name"] for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() "
                "AND indexname='idx_paper_orders_trigger_watch_once'"
            )
            has_index = cur.fetchone() is not None
        return {
            "paper_orders.trigger_watch_id": has_trigger,
            "opportunity_watches.recheck": opportunity_cols,
            "bridge_index": has_index,
        }

    def _marker_keys(self) -> set[str]:
        with self._h.conn.cursor() as cur:
            cur.execute("SELECT key FROM _migration_state")
            return {r["key"] for r in cur.fetchall()}

    def _table_count(self) -> int:
        with self._h.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema=current_schema()"
            )
            return int(cur.fetchone()["c"])

    def _assert_legacy_rows_preserved(
        self, sym_id: int, sig_id: int, watch_id: int, po_id: int
    ) -> None:
        """R10: symbol/signal/watch survive, paper_order intact with NULL bridge col."""
        with self._h.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol='TESTUSDT'")
            self.assertEqual(1, int(cur.fetchone()["c"]), "symbol row lost in upgrade")
            cur.execute("SELECT COUNT(*) AS c FROM signals WHERE id=%s", (sig_id,))
            self.assertEqual(1, int(cur.fetchone()["c"]), "signal row lost in upgrade")
            cur.execute(
                "SELECT id, signal_id, symbol, status, trigger_watch_id FROM paper_orders WHERE id=%s",
                (po_id,),
            )
            po = cur.fetchone()
            self.assertIsNotNone(po, "paper_order row lost in upgrade")
            self.assertEqual(sig_id, po["signal_id"], "paper_order->signal link broken")
            self.assertEqual("pending", po["status"], "paper_order status changed")
            self.assertIsNone(
                po["trigger_watch_id"], "legacy paper_order got a non-NULL bridge column"
            )
            cur.execute(
                "SELECT COUNT(*) AS c FROM opportunity_watches "
                "WHERE id=%s AND status='active'",
                (watch_id,),
            )
            self.assertEqual(
                1, int(cur.fetchone()["c"]), "opportunity_watch row lost in upgrade"
            )

    def _assert_legacy_rows_intact(
        self, sig_id: int, watch_id: int, po_id: int
    ) -> None:
        """R10 (rollback variant): legacy rows survive with the bridge column ABSENT."""
        with self._h.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM symbols WHERE symbol='TESTUSDT'")
            self.assertEqual(1, int(cur.fetchone()["c"]), "symbol row lost")
            cur.execute("SELECT COUNT(*) AS c FROM signals WHERE id=%s", (sig_id,))
            self.assertEqual(1, int(cur.fetchone()["c"]), "signal row lost")
            cur.execute(
                "SELECT id, signal_id, symbol, status FROM paper_orders WHERE id=%s",
                (po_id,),
            )
            po = cur.fetchone()
            self.assertIsNotNone(po, "paper_order row lost")
            self.assertEqual(sig_id, po["signal_id"], "paper_order->signal link broken")
            self.assertEqual("pending", po["status"], "paper_order status changed")
            cur.execute(
                "SELECT COUNT(*) AS c FROM opportunity_watches "
                "WHERE id=%s AND status='active'",
                (watch_id,),
            )
            self.assertEqual(
                1, int(cur.fetchone()["c"]), "opportunity_watch row lost"
            )

    # ── tests ────────────────────────────────────────────────────────────────

    def test_legacy_upgrade_single_init_upgrades_schema_and_seeds(self) -> None:
        """R1+R2+R10: one initialize_database() on a real pre-08-04 schema."""
        self._apply_legacy_schema()
        sym_id, sig_id, watch_id, po_id = self._seed_legacy_rows()

        result = initialize_database()
        self.assertTrue(result["ok"], f"legacy upgrade init not ok: {result}")

        present = self._bridge_columns_present()
        self.assertTrue(present["paper_orders.trigger_watch_id"])
        self.assertEqual(
            {"recheck_status", "recheck_order_id", "last_recheck_at"},
            present["opportunity_watches.recheck"],
        )
        self.assertTrue(present["bridge_index"])

        markers = self._marker_keys()
        missing = EXPECTED_MARKERS - markers
        self.assertFalse(
            missing, f"legacy upgrade missing markers: {sorted(missing)}"
        )
        self.assertIn("watch_order_bridge_contract_v1", markers)

        health = check_schema_health(conn=self._h.conn)
        self.assertTrue(health["ok"], f"legacy-upgraded schema not healthy: {health}")

        self._assert_legacy_rows_preserved(sym_id, sig_id, watch_id, po_id)

    def test_fresh_greenfield_init_creates_bridge_schema(self) -> None:
        """R4: helper safe no-ops on greenfield; schema SQL builds the bridge."""
        result = initialize_database()
        self.assertTrue(result["ok"], f"greenfield init not ok: {result}")

        present = self._bridge_columns_present()
        self.assertTrue(present["paper_orders.trigger_watch_id"])
        self.assertEqual(
            {"recheck_status", "recheck_order_id", "last_recheck_at"},
            present["opportunity_watches.recheck"],
        )
        self.assertTrue(present["bridge_index"])

        self.assertIn("watch_order_bridge_contract_v1", self._marker_keys())
        self.assertTrue(check_schema_health(conn=self._h.conn)["ok"])

    def test_second_init_is_idempotent_noop(self) -> None:
        """R5: upgraded-schema second init leaves an identical schema + markers."""
        self._apply_legacy_schema()
        self._seed_legacy_rows()
        initialize_database()

        tables_1 = self._table_count()
        markers_1 = self._marker_keys()

        initialize_database()

        self.assertEqual(tables_1, self._table_count(), "table count changed on re-init")
        self.assertEqual(markers_1, self._marker_keys(), "marker set changed on re-init")
        self.assertTrue(check_schema_health(conn=self._h.conn)["ok"])

    def test_mid_bridge_failure_rolls_back_atomically(self) -> None:
        """R6: a failure after the first bridge column rolls back everything."""
        self._apply_legacy_schema()
        _, sig_id, watch_id, po_id = self._seed_legacy_rows()

        original = mig._add_column
        call_count = 0

        def _boom(cur, table, column, definition):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # after paper_orders.trigger_watch_id, mid-bridge
                raise RuntimeError("forced mid-bridge failure for rollback test")
            return original(cur, table, column, definition)

        mig._add_column = _boom  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                initialize_database()
        finally:
            mig._add_column = original  # type: ignore[assignment]

        # Nothing survives the rollback: no column, index, or marker residue.
        present = self._bridge_columns_present()
        self.assertFalse(present["paper_orders.trigger_watch_id"])
        self.assertFalse(present["bridge_index"])
        self.assertNotIn(
            "watch_order_bridge_contract_v1", self._marker_keys()
        )
        self._assert_legacy_rows_intact(sig_id, watch_id, po_id)

    @pytest.mark.serial
    def test_concurrent_legacy_upgrade_no_half_migration(self) -> None:
        """R7: advisory-lock serializes concurrent legacy upgrades (no half-state)."""
        self._apply_legacy_schema()
        self._seed_legacy_rows()

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _run(label: str) -> None:
            try:
                barrier.wait(timeout=30)
                results[label] = initialize_database()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_run, args=("a",), name="legacy-init-a")
        t2 = threading.Thread(target=_run, args=("b",), name="legacy-init-b")
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)

        self.assertEqual(errors, [], f"concurrent legacy init raised: {errors}")
        self.assertIn("a", results)
        self.assertIn("b", results)
        self.assertTrue(results["a"]["ok"], f"init-a not ok: {results['a']}")
        self.assertTrue(results["b"]["ok"], f"init-b not ok: {results['b']}")

        # Final state is consistent + healthy (one upgrade, no interleaving).
        present = self._bridge_columns_present()
        self.assertTrue(present["paper_orders.trigger_watch_id"])
        self.assertEqual(
            {"recheck_status", "recheck_order_id", "last_recheck_at"},
            present["opportunity_watches.recheck"],
        )
        self.assertTrue(present["bridge_index"])
        self.assertEqual(self._marker_keys(), EXPECTED_MARKERS)
        self.assertTrue(check_schema_health(conn=self._h.conn)["ok"])

    def test_marker_written_only_after_schema_complete(self) -> None:
        """R8: the bridge marker helper runs only once the bridge schema exists."""
        self._apply_legacy_schema()

        original = mig._ensure_watch_order_bridge_contract_marker
        seen: dict[str, bool] = {}

        def _spy(cur):
            # Introspect the SAME cursor (same uncommitted txn): the bridge
            # schema must already be complete at marker-write time.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='paper_orders'"
            )
            seen["trigger_watch_id"] = any(
                r["column_name"] == "trigger_watch_id" for r in cur.fetchall()
            )
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='opportunity_watches'"
            )
            seen["recheck"] = {
                "recheck_status", "recheck_order_id", "last_recheck_at",
            } <= {r["column_name"] for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() "
                "AND indexname='idx_paper_orders_trigger_watch_once'"
            )
            seen["bridge_index"] = cur.fetchone() is not None
            return original(cur)

        mig._ensure_watch_order_bridge_contract_marker = _spy  # type: ignore[assignment]
        try:
            result = initialize_database()
        finally:
            mig._ensure_watch_order_bridge_contract_marker = original  # type: ignore[assignment]

        self.assertTrue(result["ok"])
        self.assertTrue(seen["trigger_watch_id"], "marker written before trigger_watch_id")
        self.assertTrue(seen["recheck"], "marker written before recheck columns")
        self.assertTrue(seen["bridge_index"], "marker written before bridge index")
        self.assertIn("watch_order_bridge_contract_v1", self._marker_keys())

    def test_check_schema_health_verifies_bridge_predicate(self) -> None:
        """R9: health is ok only when the correct unique partial index exists."""
        self._apply_legacy_schema()
        initialize_database()
        health = check_schema_health(conn=self._h.conn)
        self.assertTrue(health["ok"], f"upgraded schema not healthy: {health}")

        # Negative: drop the bridge index -> health must fail closed on it.
        with self._h.conn.cursor() as cur:
            cur.execute("DROP INDEX idx_paper_orders_trigger_watch_once")
        self._h.conn.commit()
        health = check_schema_health(conn=self._h.conn)
        self.assertFalse(health["ok"])
        missing_cols = [m["column"] for m in health["missing_columns"]]
        self.assertTrue(
            any("idx_paper_orders_trigger_watch_once" in c for c in missing_cols),
            f"bridge index not flagged by check_schema_health: {health}",
        )

    def test_revert_fail_without_auto_wiring(self) -> None:
        """R3: reverting the auto-wiring reproduces the 42703 release blocker."""
        self._apply_legacy_schema()

        original = mig._apply_08_04_watch_order_bridge_migration

        def _noop(cur):
            return None

        # Simulate the pre-08-06 code: initialize_database no longer runs the
        # additive bridge migration, so the schema DDL's standalone
        # CREATE UNIQUE INDEX ... WHERE trigger_watch_id ... hits UndefinedColumn
        # on the legacy paper_orders. The fail-closed wrapper must surface a
        # RuntimeError naming the column (the exact defect that blocked release).
        mig._apply_08_04_watch_order_bridge_migration = _noop  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError) as ctx:
                initialize_database()
        finally:
            mig._apply_08_04_watch_order_bridge_migration = original  # type: ignore[assignment]

        self.assertIn("trigger_watch_id", str(ctx.exception))

        # No residue: the failed init rolled back, leaving the legacy schema bare.
        present = self._bridge_columns_present()
        self.assertFalse(present["paper_orders.trigger_watch_id"])
        self.assertFalse(present["bridge_index"])


if __name__ == "__main__":
    unittest.main()
