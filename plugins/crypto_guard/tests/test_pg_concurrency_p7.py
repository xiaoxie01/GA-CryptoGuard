"""P7: concurrency primitives on real PG with two independent connections.

Exercises the repository concurrency primitives converted in P7 against the
real PG schema (initialized via ``initialize_database``). The defining P7
contract is: **two independent pooled connections racing on the same unit of
work never double-claim** -- one wins, the other is idle. This settles the
contracts that a single-connection test cannot prove:

  C1 - ``claim_next_job`` (SKIP LOCKED): two conns both call ``claim_next_job``
       on a queue with ONE pending job; exactly one gets the row (status
       'running'), the other gets ``None``. Revert-fail: a plain ``FOR UPDATE``
       (no SKIP LOCKED) makes the second conn BLOCK on the locked row -> the
       test detects that the second claim does not return promptly ``None``
       (it either deadlocks waiting or, after the first commits, re-selects a
       now-running row that the WHERE status='pending' filter excludes -- but a
       naive translation that drops ONLY ``SKIP LOCKED`` keeps the row locked so
       the second conn hangs). The honest revert is: drop the ``status='running'
       AND started_at=NOW()`` update so the row STAYS pending -> BOTH conns
       claim it (double-claim). That is the regression the test guards.
  C2 - ``claim_next_batch`` (exact-set atomic claim + token/lease stamp): a
       sealed batch; two conns call ``claim_next_batch``; exactly one gets the
       full enabled set (every returned row carries THAT worker's claim_token),
       the other gets ``None``. The returned rows are provably-owned: each row
       carries the winner's token, not a mix.
  C3 - ``claim_job_by_id_cas`` (CAS): a future-scheduled job CAS-claims False
       (not due); a due pending job CAS-claims True and stamps token+lease; a
       SECOND CAS on the now-running job returns False (status mismatch); a CAS
       on a non-existent id returns False WITHOUT aborting the outer transaction
       (user hard rule: outer-txn-not-rolled-back regression -- a sentinel row
       written in the SAME outer txn before the CAS must survive).
  C4 - ``acquire_lock`` / ``release_lock`` (ON CONFLICT DO NOTHING keyed on
       rowcount): first owner acquires (True); a second owner with a different
       owner string FAILS while TTL unexpired (False); after release_lock, a
       new owner acquires.

This is the "每完成一个业务域立即运行对应真实 PostgreSQL 测试" gate for P7.
NOT a mock; uses two real pooled conns (two ``pg_db.get_conn()``).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from psycopg.pq import TransactionStatus

from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import direct_conn, make_repo


def _prod_payload(*, symbol: str, batch_id: str, snapshot_id: int, analysis_time: int) -> dict:
    """The exact production scheduled_market_analysis payload shape.

    ``payload.symbol`` is authoritative (R7-P0-1); ``payload.snapshot`` is a
    dict whose ``symbol`` STRICTLY EQUALS ``payload.symbol`` (R8-A identity
    contract). ``seal_analysis_batch`` / ``claim_next_batch`` both validate this
    via ``validate_job_identity``.
    """
    snapshot = {"symbol": symbol, "analysis_time_utc": analysis_time, "mode": "scheduled"}
    return {
        "snapshot_id": snapshot_id,
        "snapshot": snapshot,
        "primary_interval": "5m",
        "batch_id": batch_id,
        "symbol": symbol,
    }


class TestPgConcurrencyP7(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn_a = self._repo_handle.conn
        self.repo_a = self._repo_handle.repo
        # A direct second backend shares only this test's scratch schema.
        self.conn_b = direct_conn(self._repo_handle.schema)
        self.repo_b = CryptoGuardRepository(self.conn_b)

    def tearDown(self) -> None:
        self.conn_b.close()
        self._repo_handle.close()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _build_sealed_batch(
        self, *, batch_id: str, symbols: list[str], analysis_time: int,
        repo: CryptoGuardRepository,
    ) -> list[int]:
        """Build a sealed batch in the real production shape and return job ids.

        Mirrors ``cron_scheduler.enqueue_market_analysis`` Phase 2:
        start_analysis_batch -> (save_market_snapshot + enqueue_job +
        mark_batch_symbol_completed per symbol) -> seal_analysis_batch. Uses
        the authoritative ``payload.symbol`` contract.
        """
        repo.start_analysis_batch(
            batch_id=batch_id, primary_interval="5m",
            analysis_time=analysis_time, enabled_symbols=symbols,
        )
        job_ids: list[int] = []
        for sym in symbols:
            # A minimal market_snapshot row. save_market_snapshot upserts on
            # (symbol, analysis_time, mode) and reads snapshot["symbol"],
            # int(snapshot["analysis_time_utc"]), snapshot["mode"]. analysis_time_utc
            # is the BIGINT analysis time (epoch-ms); the snapshot dict embedded in
            # the job payload also carries "symbol" for the identity contract.
            snap = {
                "symbol": sym,
                "analysis_time_utc": analysis_time,
                "mode": "scheduled",
            }
            snapshot_id = repo.save_market_snapshot(snap)
            payload = _prod_payload(
                symbol=sym, batch_id=batch_id,
                snapshot_id=snapshot_id, analysis_time=analysis_time,
            )
            repo.mark_batch_symbol_completed(
                batch_id=batch_id, symbol=sym, status="pending",
            )
            jid = repo.enqueue_job(
                "scheduled_market_analysis", priority=6, source="scheduler",
                session_id=f"system:scheduled:5m:{sym}:{analysis_time}",
                payload=payload,
            )
            job_ids.append(jid)
        sealed = repo.seal_analysis_batch(batch_id)
        self.assertTrue(sealed, "test fixture: the seeded batch must seal")
        return job_ids

    # ── C1: claim_next_job SKIP LOCKED double-claim ─────────────────────────

    def test_claim_next_job_two_conns_no_double_claim(self) -> None:
        """C1: two conns racing on one pending job -> exactly one claims."""
        self.repo_a.enqueue_job(
            "test_claim_race", priority=5, source="test",
            session_id="race-1", payload={"k": "v"},
        )
        winner = self.repo_a.claim_next_job()
        loser = self.repo_b.claim_next_job()
        self.assertIsNotNone(winner, "first conn should claim the lone job")
        self.assertEqual(winner["status"], "running")
        self.assertEqual(winner["job_type"], "test_claim_race")
        self.assertIsNone(loser, "second conn must NOT double-claim the same job")

    def test_claim_next_job_distributes_distinct_jobs(self) -> None:
        """C1 sanity: with two distinct pending jobs, each conn gets a different one."""
        jid1 = self.repo_a.enqueue_job("dist", priority=5, source="t", session_id="d1", payload={})
        jid2 = self.repo_a.enqueue_job("dist", priority=5, source="t", session_id="d2", payload={})
        c1 = self.repo_a.claim_next_job()
        c2 = self.repo_b.claim_next_job()
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        self.assertNotEqual(c1["id"], c2["id"])
        self.assertEqual({c1["id"], c2["id"]}, {jid1, jid2})
        # A third claim finds nothing.
        self.assertIsNone(self.repo_a.claim_next_job())

    # ── C2: claim_next_batch exact-set atomic claim + token/lease ───────────

    def test_claim_next_batch_two_conns_one_wins_full_set(self) -> None:
        """C2: two conns race on one sealed batch; exactly one gets the full set."""
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._build_sealed_batch(
            batch_id="5m:5000", symbols=syms, analysis_time=5000,
            repo=self.repo_a,
        )
        winner_rows = self.repo_a.claim_next_batch()
        loser_rows = self.repo_b.claim_next_batch()
        self.assertIsNotNone(winner_rows, "first conn should claim the batch")
        self.assertIsNone(loser_rows, "second conn must NOT claim the already-running batch")
        # The winner's rows are provably-owned: every row carries the SAME
        # claim_token (the winner's), the full enabled set, and a stamped lease.
        self.assertEqual(len(winner_rows), len(syms))
        tokens = {r["claim_token"] for r in winner_rows}
        self.assertEqual(len(tokens), 1, "all winner rows share one claim_token")
        win_token = tokens.pop()
        self.assertTrue(win_token, "claim_token is non-empty")
        # payload_json is JSONB -> psycopg returns it already-decoded as a dict
        # (the P6 _decode_json contract). Read .symbol directly, no json.loads.
        claimed_syms = {r["payload_json"]["symbol"] for r in winner_rows}
        self.assertEqual(claimed_syms, set(syms))
        for r in winner_rows:
            self.assertEqual(r["status"], "running")
            self.assertIsNotNone(r["lease_until"], "lease stamped on every claimed row")

    def test_fair_provider_boundary_holds_no_transaction(self) -> None:
        """The long provider phase runs after claim commit, never in a DB txn."""
        from plugins.crypto_guard.reasoning.llm_fair_scheduler import SymbolLLMResult
        from plugins.crypto_guard.run_ga_workers import process_fair_batch

        batch_id = "5m:provider-idle"
        symbol = "BTCUSDT"
        self._build_sealed_batch(
            batch_id=batch_id, symbols=[symbol], analysis_time=8_000,
            repo=self.repo_a,
        )
        jobs = self.repo_a.claim_next_batch()
        self.assertIsNotNone(jobs)
        observed: dict[str, object] = {}

        def fake_run_fair_batch(**_kwargs):
            observed["status"] = self.conn_a.info.transaction_status
            row = self.conn_b.execute(
                "SELECT status, claim_token, lease_until FROM agent_jobs "
                "WHERE id=%s",
                (int(jobs[0]["id"]),),
            ).fetchone()
            self.conn_b.rollback()
            observed["visible"] = dict(row)
            return {
                symbol: SymbolLLMResult(
                    symbol=symbol, schedule_position=0, schedule_round=1,
                    candidate=None, attempt_meta={},
                    terminal_reason="missing_snapshot",
                )
            }

        with patch(
            "plugins.crypto_guard.reasoning.llm_fair_scheduler.run_fair_batch",
            side_effect=fake_run_fair_batch,
        ):
            process_fair_batch(self.repo_a, jobs)
        self.assertEqual(observed["status"], TransactionStatus.IDLE)
        self.assertEqual(observed["visible"]["status"], "running")
        self.assertEqual(observed["visible"]["claim_token"], jobs[0]["claim_token"])
        self.assertIsNotNone(observed["visible"]["lease_until"])

    def test_fair_batch_fails_closed_when_claim_changes_during_provider(self) -> None:
        """A post-provider heartbeat detects ownership theft before writes."""
        from plugins.crypto_guard.reasoning.llm_fair_scheduler import SymbolLLMResult
        from plugins.crypto_guard.run_ga_workers import process_fair_batch

        batch_id = "5m:claim-loss"
        symbol = "ETHUSDT"
        self._build_sealed_batch(
            batch_id=batch_id, symbols=[symbol], analysis_time=8_100,
            repo=self.repo_a,
        )
        jobs = self.repo_a.claim_next_batch()
        self.assertIsNotNone(jobs)

        def steal_claim(**_kwargs):
            self.conn_b.execute(
                "UPDATE agent_jobs SET claim_token='other-owner' WHERE id=%s",
                (int(jobs[0]["id"]),),
            )
            self.conn_b.commit()
            return {
                symbol: SymbolLLMResult(
                    symbol=symbol, schedule_position=0, schedule_round=1,
                    candidate=None, attempt_meta={}, terminal_reason="missing_snapshot",
                )
            }

        with patch(
            "plugins.crypto_guard.reasoning.llm_fair_scheduler.run_fair_batch",
            side_effect=steal_claim,
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                process_fair_batch(self.repo_a, jobs)
        row = self.conn_b.execute(
            "SELECT status, claim_token FROM agent_jobs WHERE id=%s",
            (int(jobs[0]["id"]),),
        ).fetchone()
        self.conn_b.rollback()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["claim_token"], "other-owner")
        self.assertEqual(
            self.conn_a.execute(
                "SELECT COUNT(*) AS c FROM ga_decisions WHERE batch_id=%s",
                (batch_id,),
            ).fetchone()["c"],
            0,
        )
        self.conn_a.rollback()

    def test_claim_next_batch_skips_locked_batch_and_claims_next(self) -> None:
        """C2 regression: a locked oldest batch must not block another worker."""
        self._build_sealed_batch(
            batch_id="5m:6000", symbols=["BTCUSDT"], analysis_time=6000,
            repo=self.repo_a,
        )
        self._build_sealed_batch(
            batch_id="5m:7000", symbols=["ETHUSDT"], analysis_time=7000,
            repo=self.repo_a,
        )

        with self.conn_a.transaction():
            self.conn_a.execute(
                "SELECT batch_id FROM analysis_batches WHERE batch_id=%s FOR UPDATE",
                ("5m:6000",),
            ).fetchone()
            with self.conn_b.transaction():
                # Without SKIP LOCKED this becomes a bounded LockNotAvailable
                # failure instead of hanging the suite indefinitely.
                self.conn_b.execute("SET LOCAL lock_timeout = '1000ms'")
                claimed = self.repo_b.claim_next_batch()

        self.assertIsNotNone(claimed)
        self.assertEqual(
            {row["payload_json"]["symbol"] for row in claimed},
            {"ETHUSDT"},
        )

    def test_sealed_batch_rejects_post_validation_job_insert(self) -> None:
        """P0 regression: the DB rejects membership changes after sealing.

        This is the race boundary the old read-check/update claim could not
        protect at READ COMMITTED. The trigger locks the same batch row as the
        claimant and rejects the insert after observing ``claim_ready_at``.
        """
        batch_id = "5m:sealed-race"
        self._build_sealed_batch(
            batch_id=batch_id, symbols=["BTCUSDT"], analysis_time=8000,
            repo=self.repo_a,
        )
        with self.assertRaises(Exception):
            with self.conn_b.transaction():
                # Registering a new member after seal is itself forbidden. If
                # this line is neutralized, the following INSERT/trigger is the
                # second database-level guard.
                self.conn_b.execute(
                    "INSERT INTO batch_symbol_status(batch_id, symbol, status) "
                    "VALUES (%s, %s, 'pending')",
                    (batch_id, "ETHUSDT"),
                )
                payload = _prod_payload(
                    symbol="ETHUSDT", batch_id=batch_id,
                    snapshot_id=1, analysis_time=8000,
                )
                self.repo_b.enqueue_job(
                    "scheduled_market_analysis", priority=6,
                    source="scheduler", session_id="sealed-extra",
                    payload=payload,
                )

        claimed = self.repo_a.claim_next_batch()
        self.assertIsNotNone(claimed)
        self.assertEqual([row["symbol"] for row in claimed], ["BTCUSDT"])

    # ── C3: claim_job_by_id_cas CAS ─────────────────────────────────────────

    def test_cas_future_scheduled_not_claimed_early(self) -> None:
        """C3: a future scheduled_at CAS-claims False (due-time gate)."""
        # Enqueue a job scheduled 60 minutes in the future.
        future_iso = "2099-01-01T00:00:00Z"  # far future; schema accepts ISO TIMESTAMPTZ
        jid = self.repo_a.enqueue_job(
            "cas_future", priority=5, source="t", session_id="fut-1",
            payload={"k": 1}, scheduled_at=future_iso,
        )
        # CAS with default expected_status='pending' must fail the due-time gate.
        self.assertFalse(self.repo_a.claim_job_by_id_cas(job_id=jid))
        # The row is still pending (the CAS UPDATE matched 0 rows).
        with self.conn_a.cursor() as cur:
            cur.execute("SELECT status FROM agent_jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "pending")

    def test_cas_due_pending_claims_and_second_cas_fails(self) -> None:
        """C3: a due pending job CAS-claims True; a second CAS fails (status mismatch)."""
        jid = self.repo_a.enqueue_job(
            "cas_due", priority=5, source="t", session_id="due-1", payload={"k": 1},
        )
        # First CAS (repo_a) succeeds and stamps token+lease.
        self.assertTrue(self.repo_a.claim_job_by_id_cas(job_id=jid))
        # Second CAS (repo_b) on the now-running row fails -- status != pending.
        self.assertFalse(self.repo_b.claim_job_by_id_cas(job_id=jid))
        with self.conn_a.cursor() as cur:
            cur.execute("SELECT status, claim_token, lease_until FROM agent_jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "running")
        self.assertTrue(row["claim_token"])
        self.assertIsNotNone(row["lease_until"])

    def test_cas_nonexistent_id_does_not_abort_outer_transaction(self) -> None:
        """C3 regression (user hard rule): a failed CAS on a non-existent id must
        NOT abort the caller's outer transaction. A sentinel row written in the
        SAME outer txn BEFORE the CAS must survive the CAS returning False."""
        with self.conn_a.transaction():
            with self.conn_a.cursor() as cur:
                # Sentinel: a pending job that exists in this outer txn.
                cur.execute(
                    "INSERT INTO agent_jobs(job_type, priority, source, session_id, payload_json) "
                    "VALUES ('sentinel', 5, 't', 'sent-1', '{}'::jsonb) RETURNING id",
                )
                sentinel_id = int(cur.fetchone()["id"])
            # CAS on a non-existent id inside the SAME transaction.
            claimed = self.repo_a.claim_job_by_id_cas(job_id=9_999_999)
            self.assertFalse(claimed)
            # The sentinel row must still be visible inside this txn (not rolled back).
            with self.conn_a.cursor() as cur:
                cur.execute("SELECT id FROM agent_jobs WHERE id=%s", (sentinel_id,))
                self.assertIsNotNone(cur.fetchone(), "sentinel survived the failed CAS")
        # After commit, the sentinel persists.
        with self.conn_b.cursor() as cur:
            cur.execute("SELECT id FROM agent_jobs WHERE id=%s", (sentinel_id,))
            self.assertIsNotNone(cur.fetchone(), "sentinel persisted across conns after commit")

    # ── C4: acquire_lock / release_lock ─────────────────────────────────────

    def test_acquire_lock_first_owner_then_second_fails_then_release(self) -> None:
        """C4: first owner acquires; second (different owner) fails; release -> new owner."""
        lock = "p7_test_lock"
        owner_a = "workerA"
        owner_b = "workerB"
        try:
            self.assertTrue(self.repo_a.acquire_lock(lock, owner_a, ttl_seconds=300))
            # Second owner (different) cannot acquire while TTL unexpired.
            self.assertFalse(self.repo_b.acquire_lock(lock, owner_b, ttl_seconds=300))
            # Same owner re-acquiring also fails (row exists; ON CONFLICT DO NOTHING).
            self.assertFalse(self.repo_a.acquire_lock(lock, owner_a, ttl_seconds=300))
        finally:
            self.repo_a.release_lock(lock)
        # After release, a new owner acquires.
        self.assertTrue(self.repo_b.acquire_lock(lock, owner_b, ttl_seconds=60))
        self.repo_b.release_lock(lock)


if __name__ == "__main__":
    unittest.main()
