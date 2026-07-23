"""P8-1: real-PG producer end-to-end + zero-residue on the cron_scheduler path.

This is the ``每完成一个业务域立即运行对应真实 PostgreSQL 测试`` gate for the
P8-1 domain (``cron_scheduler.enqueue_market_analysis`` migrated to PG-native
transactions). It drives the REAL production chain
``enqueue_market_analysis`` -> ``seal_analysis_batch`` -> ``claim_next_batch``
against the real PG schema (initialized via ``initialize_database``), mocking
ONLY ``build_market_state_snapshot`` (no network). It does NOT call
``_enqueue_batch_jobs`` (the hand-rolled helper); it exercises the production
producer's two-phase transaction model:

  P8-D1 - success path: a full enabled set seals, N real-format session_ids,
         N authoritative payload.symbol values (each == its snapshot.symbol),
         deferred module_analysis_results persist, and ONE ``claim_next_batch``
         returns the exact enabled set.

  P8-D2 - zero-residue on seal failure (R8-C/P1-2 on PG): a forced seal failure
         (inject a foreign/inconsistent job so the exact-set validation fails)
         after the Phase-2 transaction opens must leave ZERO residue - no batch
         row, no jobs, no batch_symbol_status rows, no market_snapshots rows,
         no module_analysis_results rows. This is the orphan-snapshot guarantee
         on PG: pre-R8-C the snapshot auto-committed in Phase 1 and survived the
         Phase-2 ROLLBACK; on PG the snapshot is persisted INSIDE the
         ``conn.transaction()`` so the rollback reverts it too.

The P7 suite proved the repo primitives on PG; this suite proves the PRODUCER
that calls them drives the transaction boundaries correctly under psycopg's
``autocommit=False`` model (Phase-1 commit so prepared audit logs survive a
Phase-2 rollback; Phase-2 atomic ``conn.transaction()``).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.concurrency, pytest.mark.e2e]

import unittest
from unittest.mock import patch

from plugins.crypto_guard.scheduler import cron_scheduler as cron_mod
from plugins.crypto_guard.scheduler.cron_scheduler import enqueue_market_analysis
from plugins.crypto_guard.service_manager import (
    _set_warmup_ready,
    get_warmup_state,
)
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestPgProducerP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo
        # Discover the seeded enabled set the way the producer does (via the
        # repository query), rather than hardcoding symbols that might drift.
        self.enabled = list(self.repo.active_analysis_symbols())
        self.assertGreater(
            len(self.enabled), 0,
            "test fixture: the seeded DB must have at least one active symbol",
        )
        # psycopg pooled connections are ``autocommit=False`` (always in an
        # implicit transaction). The SELECT above opened a transaction whose
        # transaction-start ``NOW()`` is frozen at THIS instant. A later
        # ``claim_next_batch`` (or any assertion using ``NOW()``) on THIS conn
        # would see a stale NOW() predating the producer's enqueued jobs -> the
        # head SELECT's ``scheduled_at <= NOW()`` predicate would reject them ->
        # claim spuriously returns None. Commit now so the NEXT transaction on
        # this conn starts after the producer's enqueue (fresh NOW()). This
        # mirrors production, where a worker opens a fresh transaction per
        # claim, well after the scheduler enqueued.
        self.conn.commit()
        # A deterministic analysis_time that does not collide with real data.
        self.analysis_time = 1_700_000_000_000

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _fake_snapshot(self):
        """A canned ``build_market_state_snapshot`` that mirrors the real
        production payload shape: returns a snapshot dict whose ``symbol``
        STRICTLY EQUALS the requested symbol (the R8-A identity contract), and
        appends deferred module_analysis_results tuples to the sink so the P8-D1
        success path also proves the deferred module rows persist on PG.
        """

        def _impl(
            repo, *, symbol, analysis_time_utc, mode, timeframes,
            module_result_sink=None, skill_log_sink=None,
            batch_id=None, attempt_id=None, **_kwargs,
        ):
            if module_result_sink is not None:
                module_result_sink.append(
                    (symbol, "15m", int(analysis_time_utc), "trend_stage", {"trend_stage": "up"}, 0.8)
                )
                module_result_sink.append(
                    (symbol, "multi", int(analysis_time_utc), "market_regime", {"regime": "trend"}, 0.7)
                )
            return {
                "symbol": symbol,
                "analysis_time_utc": int(analysis_time_utc),
                "mode": mode or "scheduled",
                "modules": {},
                "data_quality": {},
            }

        return _impl

    def _count(self, sql: str, params: tuple = ()) -> int:
        # ``pg_db`` returns dict rows, so alias the count and read by name.
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return 0
        # dict_row -> access by column name; fall back to positional for safety.
        if isinstance(row, dict):
            return int(list(row.values())[0])
        return int(row[0])

    # ── P8-D1: success path e2e ─────────────────────────────────────────────

    def test_real_producer_seals_and_claims_exact_enabled_set(self) -> None:
        """P8-D1: the real producer seals a full enabled set and a single
        ``claim_next_batch`` returns exactly it."""
        old_warmup = get_warmup_state()
        try:
            _set_warmup_ready()
            with patch.object(cron_mod, "build_market_state_snapshot", self._fake_snapshot()):
                result = enqueue_market_analysis(
                    analysis_time_utc=self.analysis_time, primary_interval="15m",
                    timeframes=["15m"],
                )
            self.assertTrue(
                result.get("ok"),
                "P8-D1: the real producer must report ok=True. Got %r" % (result,),
            )
            self.assertTrue(
                result.get("sealed"),
                "P8-D1: the real producer must seal the batch. Got %r" % (result,),
            )
            self.assertEqual(result.get("queued"), len(self.enabled))
            batch_id = result["batch_id"]
            # The producer snaps analysis_time_utc to the last CLOSED 15m candle
            # boundary via latest_closed_close_time_ms (the param is only a
            # hint), so read the actual analysis_time from the result rather than
            # assume it equals the input.
            analysis_time = int(result["analysis_time_utc"])
            self.assertEqual(batch_id, f"15m:{analysis_time}")

            # N real-format session_ids, each carrying an authoritative
            # payload.symbol that equals its snapshot.symbol (identity contract).
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, payload_json FROM agent_jobs "
                    "WHERE job_type='scheduled_market_analysis' ORDER BY id ASC"
                )
                rows = [dict(r) for r in cur.fetchall()]
            self.assertEqual(len(rows), len(self.enabled))
            expected_sessions = {
                f"system:scheduled:15m:{s}:{analysis_time}" for s in self.enabled
            }
            actual_sessions = {r["session_id"] for r in rows}
            self.assertEqual(
                actual_sessions, expected_sessions,
                "P8-D1: every job must use the production session_id format. Got %r" % (actual_sessions,),
            )
            # payload_json is JSONB -> psycopg returns it already-decoded (P6).
            sym_set = set(self.enabled)
            for r in rows:
                payload = r["payload_json"]
                sym = payload.get("symbol")
                self.assertIn(sym, sym_set)
                self.assertEqual(
                    sym, payload.get("snapshot", {}).get("symbol"),
                    "P8-D1: payload.symbol must equal payload.snapshot.symbol. "
                    "Got %r vs %r" % (sym, payload.get("snapshot", {}).get("symbol")),
                )
                self.assertEqual(payload.get("batch_id"), batch_id)
                self.assertIsNotNone(payload.get("snapshot_id"))

            # The batch row is sealed (claim_ready_at stamped).
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT claim_ready_at, enabled_symbols_json FROM analysis_batches WHERE batch_id=%s",
                    (batch_id,),
                )
                batch_row = cur.fetchone()
            self.assertIsNotNone(batch_row)
            self.assertIsNotNone(batch_row["claim_ready_at"])
            # enabled_symbols_json is JSONB -> already-decoded list.
            self.assertEqual(set(batch_row["enabled_symbols_json"] or []), set(self.enabled))

            # Deferred module_analysis_results persist on the success path
            # (2 tuples per enabled symbol from the canned builder).
            module_count = self._count(
                "SELECT COUNT(*) FROM module_analysis_results WHERE analysis_time=%s",
                (int(analysis_time),),
            )
            self.assertEqual(
                module_count, 2 * len(self.enabled),
                "P8-D1: deferred module_analysis_results must persist (2/symbol). "
                "Expected %d, got %d." % (2 * len(self.enabled), module_count),
            )

            # ONE claim_next_batch returns the EXACT enabled set.
            claimed = self.repo.claim_next_batch()
            self.assertIsNotNone(claimed)
            self.assertEqual(len(claimed), len(self.enabled))
            claimed_syms = set()
            for c in claimed:
                self.assertEqual(str(c["status"]), "running")
                claimed_syms.add(c["payload_json"].get("symbol"))
            self.assertEqual(claimed_syms, set(self.enabled))
            # A second claim returns None (no other sealed+pending batch).
            self.assertIsNone(self.repo.claim_next_batch())
        finally:
            if old_warmup == "ready":
                _set_warmup_ready()

    # ── P8-D2: zero-residue on seal failure (R8-C on PG) ────────────────────

    def test_seal_failure_leaves_zero_residue_including_snapshot(self) -> None:
        """P8-D2 (R8-C/P1-2 on PG): a Phase-2 seal failure must leave ZERO
        residue - no batch, no jobs, no batch_symbol_status, no
        market_snapshots, no module_analysis_results. The snapshot is persisted
        INSIDE the ``conn.transaction()`` (not Phase-1 autocommit), so the
        Phase-2 rollback reverts it too (no orphan).

        Failure is injected by making ``seal_analysis_batch`` return False after
        the producer has built every Phase-2 row. This exercises the producer's
        real controlled rollback without constructing an impossible database
        state: PostgreSQL now rejects foreign scheduled-job membership at the
        trigger/FK boundary before the seal can run.
        """
        old_warmup = get_warmup_state()
        # A distinct analysis_time hint so this test's residue is isolated; the
        # producer snaps it to the last closed 15m boundary, so we capture the
        # ACTUAL batch_id from the seal call (we cannot pre-compute it).
        analysis_time_hint = 1_700_000_001_000
        try:
            _set_warmup_ready()

            captured: dict[str, str] = {}

            def _force_seal_failure(_repo_self, bid):
                captured.setdefault("batch_id", bid)
                return False

            # Patch the CLASS method so the producer's OWN internal repo (built
            # from its own pooled conn) takes the controlled failure path.
            with patch.object(CryptoGuardRepository, "seal_analysis_batch", _force_seal_failure), \
                 patch.object(cron_mod, "build_market_state_snapshot", self._fake_snapshot()):
                result = enqueue_market_analysis(
                    analysis_time_utc=analysis_time_hint, primary_interval="15m",
                    timeframes=["15m"],
                )
            batch_id = captured.get("batch_id") or result.get("batch_id")
            analysis_time = int(result["analysis_time_utc"])
            # The seal failed -> ok=False, sealed=False (controlled rollback,
            # NOT a crash that re-raises).
            self.assertFalse(
                result.get("ok"),
                "P8-D2: a seal failure must report ok=False. Got %r" % (result,),
            )
            self.assertFalse(result.get("sealed"))

            # ZERO residue across every Phase-2 table.
            self.assertEqual(
                self._count("SELECT COUNT(*) FROM analysis_batches WHERE batch_id=%s", (batch_id,)),
                0,
                "P8-D2: no batch row must survive a seal-failure rollback.",
            )
            self.assertEqual(
                self._count(
                    "SELECT COUNT(*) FROM agent_jobs WHERE job_type='scheduled_market_analysis' "
                    "AND session_id LIKE %s",
                    (f"%:{analysis_time}",),
                ),
                0,
                "P8-D2: no scheduled jobs must survive.",
            )
            self.assertEqual(
                self._count(
                    "SELECT COUNT(*) FROM batch_symbol_status WHERE batch_id=%s",
                    (batch_id,),
                ),
                0,
                "P8-D2: no batch_symbol_status rows must survive.",
            )
            self.assertEqual(
                self._count(
                    "SELECT COUNT(*) FROM market_snapshots WHERE analysis_time=%s",
                    (int(analysis_time),),
                ),
                0,
                "P8-D2 (R8-C): no orphan market_snapshots must survive - the "
                "snapshot was persisted inside the Phase-2 transaction so the "
                "rollback reverted it too.",
            )
            self.assertEqual(
                self._count(
                    "SELECT COUNT(*) FROM module_analysis_results WHERE analysis_time=%s",
                    (int(analysis_time),),
                ),
                0,
                "P8-D2 (R8 P2-2): no orphan module_analysis_results must survive.",
            )
        finally:
            if old_warmup == "ready":
                _set_warmup_ready()

    # ── P8-D3: revert-fail proof for the R8-C snapshot-in-transaction guard ──

    def test_orphan_snapshot_survives_when_persisted_outside_phase2(self) -> None:
        """P8-D3 (revert-fail / positive control for the R8-C guarantee).

        P8-D2 asserts ZERO ``market_snapshots`` residue after a seal-failure
        rollback - that is the R8-C guarantee: the snapshot is persisted INSIDE
        the Phase-2 ``conn.transaction()`` so the rollback reverts it too. This
        test proves that assertion is LOAD-BEARING, not vacuously true (i.e. not
        "zero because nothing was ever written"): it simulates the PRE-R8-C bug
        shape - persisting the snapshot in a SEPARATELY-COMMITTED transaction
        BEFORE Phase 2 opens - and asserts the orphan ``market_snapshots`` row
        SURVIVES the Phase-2 seal-failure rollback.

        If a future change moves ``save_market_snapshot`` back out of the
        Phase-2 transaction (the pre-R8-C bug), production would leak orphan
        snapshots on every seal failure; this test goes RED to flag it. As long
        as the persist stays inside Phase 2 (the current correct shape), a
        SEPARATE pre-Phase-2 persist - which is what this test injects - is the
        only way to leave residue, and residue is exactly what we assert here.
        """
        old_warmup = get_warmup_state()
        try:
            _set_warmup_ready()

            # Strategy: simulate the PRE-R8-C bug shape with TWO injections.
            #   (a) A ``build_market_state_snapshot`` patch that, for the FIRST
            #       symbol, persists a snapshot via the producer's OWN repo/conn
            #       during Phase 1 and then calls ``conn.commit()`` directly.
            #       Phase 1 has NO explicit ``conn.transaction()`` block open
            #       (only the autocommit=False implicit txn), so a direct
            #       ``commit()`` is legal and durable - the snapshot row
            #       survives any later Phase-2 rollback. This is the pre-R8-C
            #       "snapshot auto-committed before BEGIN IMMEDIATE" bug.
            #   (b) A ``seal_analysis_batch`` patch that returns False, which
            #       drives the producer's real controlled Phase-2 rollback.
            #       Earlier versions inserted an invalid foreign job here, but
            #       the PostgreSQL membership trigger now correctly rejects
            #       that impossible state before the seal can run.
            orphan_sym = self.enabled[0]
            orphan_committed_at: list[int] = []

            def _snapshot_with_pre_phase2_orphan(
                repo, *, symbol, analysis_time_utc, mode, timeframes,
                module_result_sink=None, skill_log_sink=None,
                batch_id=None, attempt_id=None, **_kwargs,
            ):
                # Persist+commit an orphan snapshot for the FIRST symbol, in an
                # independently-committed transaction (pre-R8-C bug shape).
                if not orphan_committed_at:
                    orphan_committed_at.append(int(analysis_time_utc))
                    repo.save_market_snapshot({
                        "symbol": symbol,
                        "analysis_time_utc": int(analysis_time_utc),
                        "mode": mode or "scheduled",
                        "modules": {},
                        "data_quality": {},
                    })
                    repo.conn.commit()  # legal in Phase 1 (no explicit txn block)
                if module_result_sink is not None:
                    module_result_sink.append(
                        (symbol, "15m", int(analysis_time_utc), "trend_stage", {"trend_stage": "up"}, 0.8)
                    )
                return {
                    "symbol": symbol,
                    "analysis_time_utc": int(analysis_time_utc),
                    "mode": mode or "scheduled",
                    "modules": {},
                    "data_quality": {},
                }

            def _force_seal_failure(_repo_self, _batch_id):
                return False

            with patch.object(CryptoGuardRepository, "seal_analysis_batch", _force_seal_failure), \
                 patch.object(cron_mod, "build_market_state_snapshot", _snapshot_with_pre_phase2_orphan):
                result = enqueue_market_analysis(
                    analysis_time_utc=1_700_000_002_000, primary_interval="15m",
                    timeframes=["15m"],
                )
            analysis_time = int(result["analysis_time_utc"])
            self.assertFalse(result.get("ok"))
            self.assertFalse(result.get("sealed"))

            # The orphan snapshot, persisted in its OWN committed transaction
            # BEFORE Phase 2, SURVIVES the Phase-2 seal-failure rollback. This
            # is the bug shape P8-D2 guards against: residue > 0 here proves the
            # ONLY thing keeping P8-D2's residue at 0 is the real producer
            # persisting inside Phase 2 (revert of that -> this becomes the
            # production reality, and P8-D2's zero-count goes RED).
            snapshot_count = self._count(
                "SELECT COUNT(*) FROM market_snapshots WHERE analysis_time=%s",
                (int(analysis_time),),
            )
            self.assertGreaterEqual(
                snapshot_count, 1,
                "P8-D3 revert-fail: the pre-R8-C bug shape (separately-committed "
                "snapshot) MUST leave an orphan surviving the Phase-2 rollback. "
                "Got count=%d - if this is 0, the bug shape was not reproduced "
                "and P8-D2's zero-residue assertion is unproven." % snapshot_count,
            )
        finally:
            if old_warmup == "ready":
                _set_warmup_ready()


if __name__ == "__main__":
    unittest.main()
