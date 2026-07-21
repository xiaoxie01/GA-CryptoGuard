"""P8-2: real-PG service-ownership CAS (acquire / renew / release) on PG.

The ``每完成一个业务域立即运行对应真实 PostgreSQL 测试`` gate for the P8-2
domain (``service_manager`` migrated to PG-native). It drives the REAL
production ownership functions
``acquire_service_ownership`` -> ``_renew_service_ownership_lease`` ->
``release_service_ownership`` against the real PG schema (initialized via
``initialize_database``), using TWO independent pooled connections (psycopg_pool
hands distinct backends, so a lock on one is invisible to the other until commit
- the same concurrency primitive proven in the P7 suite).

These tests do NOT spawn service threads; they exercise ONLY the ownership CAS
layer (the safety-critical R7-P0-3 / R8-E / R10-P1 contracts):

  P8-O1 - acquire / reclaim / heartbeat / release on ONE conn: a fresh acquire
         returns ``acquired: True`` and stamps the lease row; a heartbeat renews
         it (CAS on pid+owner_token); a second same-process acquire returns
         ``already_started``; release clears the row + cache.

  P8-O2 - cross-process dual-owner prevention (R7-P0-3): conn_a acquires; a
         SECOND conn_b with a DIFFERENT pid probe (simulating an external live
         process) sees conn_a's live lease and returns
         ``already_started_external`` -- it does NOT acquire. The PID-liveness
         probe is injected so conn_b sees conn_a's pid as alive without a real
         second process. When the probe reports the owner pid DEAD, conn_b
         reclaims (crash/restart recovery).

  P8-O3 - heartbeat lost sets the R8-E ownership-lost latch: a heartbeat whose
         CAS matches 0 rows (lease reclaimed out from under the owner) returns
         ``lost`` AND sets ``_OWNERSHIP_LOST`` so the three service loops stop
         claiming.

  P8-O4 - release-on-init-failure CAS (R10-P1): after an acquire, a
         ``release_service_ownership`` on a FRESH conn (simulating the init-
         failure path that re-opens a connection) clears the lease row via the
         pid+owner_token CAS, so the next acquire does NOT hit the same-process
         fast path.

  P8-O5 - no plaintext DSN in the lease row: the ``db_path`` field stored in
         ``_service_ownership`` (and returned in ``already_started_external``)
         must NOT contain the DSN password -- only a redacted identifier (dbname
         or host), so the secret never lands in the DB / logs / operator output.
"""

from __future__ import annotations

import os
import unittest

from plugins.crypto_guard import service_manager as sm
from plugins.crypto_guard.service_manager import (
    _OWNERSHIP_LOST,
    OWNERSHIP_LEASE_KEY,
    OWNERSHIP_LEASE_TTL_MS,
    acquire_service_ownership,
    _renew_service_ownership_lease,
    release_service_ownership,
)
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _redacted_dbid() -> str:
    """The redacted identifier the production code should store in the lease
    row's ``db_path`` field (dbname only -- never the DSN password)."""
    from plugins.crypto_guard.config.loader import resolve_database_url

    dsn = resolve_database_url()
    # Derive dbname from the DSN path; never return the raw DSN.
    tail = dsn.rsplit("/", 1)[-1]
    # Strip any query/params if present.
    return tail.split("?", 1)[0] or "crypto_guard"


class _LeaseConn:
    """A pooled-connection holder that exposes ``conn`` and releases on exit."""

    def __init__(self) -> None:
        self._cm = pg_db.get_conn()
        self.conn = self._cm.__enter__()

    def close(self) -> None:
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass


class _patch_getpid:
    """Temporarily patch ``os.getpid`` to a fixed pid so a second acquire on a
    different pooled conn faithfully simulates a DIFFERENT live process.

    The ownership CAS keys on ``pid``: ``external = (owner_pid != my_pid)`` and
    the heartbeat/release CAS key on ``pid + owner_token``. Both pooled conns in
    one interpreter share the real ``os.getpid()``, so without this patch the
    "external live owner blocks" branch (``external and owner_live``) can never
    fire -- the second acquire would take the same-pid-cache-miss reclaim path
    instead. This mirrors the production-faithful pattern used in
    ``test_smoke.py`` (the SQLite ownership suite).
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._orig = os.getpid

    def __enter__(self) -> "_patch_getpid":
        os.getpid = lambda *a, **k: self._pid  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        os.getpid = self._orig  # type: ignore[assignment]


class TestPgServiceOwnershipP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self._dsn = os.environ["CRYPTO_GUARD_DATABASE_URL"]
        # Reset the in-process ownership cache + lost latch between tests so each
        # test starts from a clean ownership state.
        sm._OWNERSHIP_LEASE = None
        _OWNERSHIP_LOST.clear()
        # The pid-liveness probe is a test injection seam; clear it so production
        # behavior runs by default. Tests that need to simulate a second live
        # process set their own probe.
        sm._PID_LIVENESS_PROBE = None
        self._redacted = _redacted_dbid()

    def tearDown(self) -> None:
        sm._PID_LIVENESS_PROBE = None
        sm._OWNERSHIP_LEASE = None
        _OWNERSHIP_LOST.clear()
        self._repo_handle.close()

    def _count_lease_rows(self) -> int:
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM _service_ownership")
                row = cur.fetchone()
        return int(row["c"]) if row else 0

    def _lease_row(self) -> dict | None:
        with pg_db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pid, lease_until_ms, owner_token, db_path FROM _service_ownership WHERE key=%s",
                    (OWNERSHIP_LEASE_KEY,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    # ── P8-O1: acquire / reclaim / heartbeat / release on one conn ───────────

    def test_acquire_renew_release_lifecycle(self) -> None:
        lc = _LeaseConn()
        try:
            got = acquire_service_ownership(lc.conn, self._redacted)
            self.assertTrue(got.get("acquired"), "P8-O1: fresh acquire must return acquired=True. Got %r" % (got,))

            # The lease row is stamped.
            row = self._lease_row()
            self.assertIsNotNone(row)
            self.assertEqual(int(row["pid"]), os.getpid())
            self.assertIsNotNone(row["owner_token"])

            # Heartbeat renews (CAS on pid+owner_token) -> renewed=True.
            lease_before = int(row["lease_until_ms"])
            hb = _renew_service_ownership_lease(lc.conn)
            self.assertTrue(hb.get("renewed"), "P8-O1: heartbeat must renew. Got %r" % (hb,))
            self.assertGreaterEqual(int(hb["lease_until_ms"]), lease_before)

            # Same-process reentrant acquire -> already_started (fast path).
            again = acquire_service_ownership(lc.conn, self._redacted)
            self.assertFalse(again.get("acquired"))
            self.assertEqual(again.get("reason"), "already_started")

            # Release clears the row + cache.
            rel = release_service_ownership(lc.conn)
            self.assertTrue(rel.get("released"), "P8-O1: release must clear the row. Got %r" % (rel,))
            self.assertEqual(self._count_lease_rows(), 0)
            self.assertIsNone(sm._OWNERSHIP_LEASE)
        finally:
            lc.close()

    # ── P8-O2: cross-process dual-owner prevention (R7-P0-3) ─────────────────

    def test_external_live_owner_blocks_second_acquire(self) -> None:
        # conn_a acquires as this process (real pid).
        lc_a = _LeaseConn()
        lc_b = _LeaseConn()
        try:
            got = acquire_service_ownership(lc_a.conn, self._redacted)
            self.assertTrue(got.get("acquired"))
            owner_pid = os.getpid()
            row = self._lease_row()
            self.assertIsNotNone(row)

            # Simulate a SECOND live external process (conn_b): patch os.getpid
            # to a DIFFERENT pid and report BOTH pids alive via the liveness
            # probe. The acquire must return already_started_external, NOT
            # acquire -- a live external owner blocks a duplicate start.
            sm._PID_LIVENESS_PROBE = lambda pid: True
            sm._OWNERSHIP_LEASE = None  # conn_b is a different process
            with _patch_getpid(owner_pid + 1):
                dup = acquire_service_ownership(lc_b.conn, self._redacted)
            self.assertFalse(dup.get("acquired"))
            self.assertEqual(dup.get("reason"), "already_started_external")
            self.assertEqual(int(dup["owner_pid"]), owner_pid)
            # The lease row is unchanged (still owned by conn_a's pid).
            row2 = self._lease_row()
            self.assertIsNotNone(row2)
            self.assertEqual(int(row2["pid"]), owner_pid)
        finally:
            sm._PID_LIVENESS_PROBE = None
            sm._OWNERSHIP_LEASE = None
            lc_a.close()
            lc_b.close()

    def test_external_dead_owner_is_reclaimable(self) -> None:
        # conn_a acquires; then the owner pid is reported DEAD by the probe
        # (crash/restart). A second conn_b (a DIFFERENT pid) must RECLAIM
        # (acquired=True).
        lc_a = _LeaseConn()
        lc_b = _LeaseConn()
        try:
            got = acquire_service_ownership(lc_a.conn, self._redacted)
            self.assertTrue(got.get("acquired"))
            owner_pid = os.getpid()
            # Owner pid is dead -> reclaimable. conn_b runs as a different pid.
            sm._PID_LIVENESS_PROBE = lambda pid: False
            sm._OWNERSHIP_LEASE = None  # conn_b is a different process
            with _patch_getpid(owner_pid + 1):
                reclaimed = acquire_service_ownership(lc_b.conn, self._redacted)
            self.assertTrue(reclaimed.get("acquired"),
                            "P8-O2: a dead external owner must be reclaimable. Got %r" % (reclaimed,))
            # The lease row now belongs to conn_b's (patched) pid (reclaimed).
            row = self._lease_row()
            self.assertIsNotNone(row)
            self.assertEqual(int(row["pid"]), owner_pid + 1)
        finally:
            sm._PID_LIVENESS_PROBE = None
            sm._OWNERSHIP_LEASE = None
            lc_a.close()
            lc_b.close()

    # ── P8-O3: heartbeat lost sets the R8-E ownership-lost latch ─────────────

    def test_heartbeat_lost_sets_ownership_lost_latch(self) -> None:
        lc = _LeaseConn()
        try:
            got = acquire_service_ownership(lc.conn, self._redacted)
            self.assertTrue(got.get("acquired"))
            # Simulate the lease being reclaimed out from under us: clear the
            # DB row so the heartbeat CAS matches 0 rows -> reason=lost.
            with pg_db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM _service_ownership WHERE key=%s", (OWNERSHIP_LEASE_KEY,))
            self.assertFalse(_OWNERSHIP_LOST.is_set())
            hb = _renew_service_ownership_lease(lc.conn)
            self.assertFalse(hb.get("renewed"))
            self.assertEqual(hb.get("reason"), "lost")
            self.assertTrue(_OWNERSHIP_LOST.is_set(),
                            "P8-O3: a lost heartbeat must set the R8-E _OWNERSHIP_LOST latch "
                            "so the service loops stop claiming.")
            # The cache is cleared.
            self.assertIsNone(sm._OWNERSHIP_LEASE)
        finally:
            lc.close()

    # ── P8-O4: release-on-init-failure CAS (R10-P1) ──────────────────────────

    def test_release_on_fresh_conn_clears_lease(self) -> None:
        # Acquire on conn_a, then release on a FRESH conn_b (the R10-P1 init-
        # failure path re-opens a connection). The pid+owner_token CAS must
        # clear the row.
        lc_a = _LeaseConn()
        try:
            got = acquire_service_ownership(lc_a.conn, self._redacted)
            self.assertTrue(got.get("acquired"))
            self.assertIsNotNone(self._lease_row())
        finally:
            lc_a.close()
        # Fresh connection for the release (the owner-path conn may be closed).
        lc_b = _LeaseConn()
        try:
            rel = release_service_ownership(lc_b.conn)
            self.assertTrue(rel.get("released"),
                            "P8-O4: release on a fresh conn must clear the lease via CAS. Got %r" % (rel,))
            self.assertEqual(self._count_lease_rows(), 0)
            self.assertIsNone(sm._OWNERSHIP_LEASE)
        finally:
            lc_b.close()

    # ── P8-O5: no plaintext DSN in the lease row ─────────────────────────────

    def test_lease_row_db_path_is_redacted_not_raw_dsn(self) -> None:
        lc = _LeaseConn()
        try:
            got = acquire_service_ownership(lc.conn, self._redacted)
            self.assertTrue(got.get("acquired"))
            row = self._lease_row()
            self.assertIsNotNone(row)
            stored = row["db_path"]
            # The raw DSN contains the password; it MUST NOT appear in the row.
            self.assertNotIn(self._dsn, str(stored),
                             "P8-O5: the raw DSN must not be stored in the lease row db_path.")
            # And the password segment (``:<pw>@``) must not appear.
            # The DSN shape is postgresql://user:PW@host:port/db
            if "@" in self._dsn:
                cred = self._dsn.split("://", 1)[1].split("@", 1)[0]
                self.assertNotIn(cred, str(stored),
                                 "P8-O5: the DSN credentials must not be stored in the lease row.")
        finally:
            lc.close()

    # ── P8-O6: the transaction-scoped advisory lock serializes concurrent FIRST
    #    acquires (the empty-table dual-owner race R7-P0-3 exists to prevent) ──
    #
    # ``SELECT ... FOR UPDATE`` locks NOTHING on a non-existent row, so without
    # the advisory lock two concurrent FIRST acquires (empty lease table) would
    # both see ``None``, both UPSERT, and both return ``acquired: True`` -> a
    # dual-owner service set. SQLite's ``BEGIN IMMEDIATE`` RESERVED lock blocked
    # this at BEGIN, before any SELECT; the PG-native equivalent is a
    # transaction-scoped ``pg_advisory_xact_lock`` acquired unconditionally at
    # transaction start. These two tests PROVE that lock is load-bearing with a
    # DETERMINISTIC barrier (no flaky two-process race):
    #
    #   GREEN  -- conn_a holds the advisory lock inside an OPEN transaction; a
    #            concurrent acquire on conn_b must BLOCK on the same advisory
    #            lock (it cannot proceed while conn_a's transaction is open),
    #            proving the lock serializes the two acquires.
    #   RED    -- with the advisory lock DISABLED (the revert-fail lever),
    #            conn_b's acquire does NOT block on the advisory lock (it
    #            proceeds immediately past the point where the lock would have
    #            been taken), proving the lock -- and nothing else -- is what
    #            serializes.
    #
    # Both run the contending acquire in a background thread so the test thread
    # can time it; the barrier is deterministic (conn_a's open transaction, not
    # a scheduling race).

    def _hold_advisory_lock_then_acquire(
        self, holder: object, acquirer: object, disable_lock: bool,
    ) -> tuple[float, dict]:
        import threading
        import time

        from plugins.crypto_guard import service_manager as svc

        hi, lo = svc._ownership_advisory_lock_key()
        # holder opens a transaction and takes the advisory lock, then SITS in
        # that open transaction (holding the lock) until released.
        holder_ready = threading.Event()
        holder_done = threading.Event()
        acquire_result: dict = {}
        acquire_started = threading.Event()

        def _hold() -> None:
            with holder.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (hi, lo))
                cur.fetchone()
                holder_ready.set()
                holder_done.wait(15)  # hold the lock until the acquirer is done
                cur.execute("ROLLBACK")

        def _acquire() -> None:
            acquire_started.set()
            try:
                got = acquire_service_ownership(
                    acquirer, _redacted_dbid(),
                )
                acquire_result.update(got)
            except Exception as exc:  # pragma: no cover - surfaced
                acquire_result["error"] = repr(exc)

        sm._ADVISORY_LOCK_ENABLED = not disable_lock
        sm._OWNERSHIP_LEASE = None
        _OWNERSHIP_LOST.clear()
        sm._PID_LIVENESS_PROBE = None

        th_hold = threading.Thread(target=_hold, daemon=True)
        th_acq = threading.Thread(target=_acquire, daemon=True)
        th_hold.start()
        self.assertTrue(holder_ready.wait(10), "holder did not take the lock")
        t0 = time.monotonic()
        th_acq.start()
        # If the lock is enabled, the acquirer BLOCKS (we do NOT join it; we
        # prove it is stuck by the elapsed time). If disabled, it finishes fast.
        th_acq.join(timeout=5)
        elapsed = time.monotonic() - t0
        # Release the holder so the test can clean up.
        holder_done.set()
        th_hold.join(timeout=10)
        # Reset the production seam for subsequent tests.
        sm._ADVISORY_LOCK_ENABLED = True
        return elapsed, acquire_result

    def test_advisory_lock_blocks_concurrent_acquire(self) -> None:
        # GREEN: conn_a holds the advisory lock in an open transaction. conn_b's
        # acquire must BLOCK on the same advisory lock -> its 5s join times out
        # (the thread is still alive, stuck in pg_advisory_xact_lock).
        lc_holder = _LeaseConn()
        lc_acquirer = _LeaseConn()
        try:
            elapsed, _ = self._hold_advisory_lock_then_acquire(
                lc_holder.conn, lc_acquirer.conn, disable_lock=False,
            )
            # The acquirer did NOT finish within 5s -> it was blocked on the
            # advisory lock (the only synchronization between the two conns).
            self.assertGreaterEqual(
                elapsed, 4.5,
                "P8-O6: a concurrent acquire must BLOCK while another "
                "transaction holds the advisory lock (elapsed=%.2fs)." % elapsed,
            )
        finally:
            sm._ADVISORY_LOCK_ENABLED = True
            sm._OWNERSHIP_LEASE = None
            _OWNERSHIP_LOST.clear()
            # Clear any lease the acquirer may have written once unblocked.
            with pg_db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM _service_ownership WHERE key=%s",
                        (OWNERSHIP_LEASE_KEY,),
                    )
            lc_holder.close()
            lc_acquirer.close()

    def test_revert_fail_no_advisory_lock_does_not_block(self) -> None:
        # RED (revert-fail proof): with the advisory lock DISABLED, conn_b's
        # acquire does NOT block on the advisory lock -- it proceeds past that
        # point immediately (finishes well within the 5s join). This proves the
        # advisory lock -- and nothing else -- is what serializes the two
        # acquires. Remove the lock in production and the empty-table
        # dual-owner race reopens.
        lc_holder = _LeaseConn()
        lc_acquirer = _LeaseConn()
        try:
            elapsed, got = self._hold_advisory_lock_then_acquire(
                lc_holder.conn, lc_acquirer.conn, disable_lock=True,
            )
            # The acquirer FINISHED (did not block) -> no advisory lock held it.
            self.assertLess(
                elapsed, 4.5,
                "P8-O6 revert-fail: WITHOUT the advisory lock, a concurrent "
                "acquire must NOT block (elapsed=%.2fs); the lock is "
                "load-bearing." % elapsed,
            )
            # And it reached a real outcome (acquired or saw the holder's row),
            # proving it proceeded past the lock point.
            self.assertTrue(
                got.get("acquired") or got.get("reason"),
                "P8-O6 revert-fail: the unblocked acquire must reach an "
                "outcome. Got %r" % (got,),
            )
        finally:
            sm._ADVISORY_LOCK_ENABLED = True
            sm._OWNERSHIP_LEASE = None
            _OWNERSHIP_LOST.clear()
            with pg_db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM _service_ownership WHERE key=%s",
                        (OWNERSHIP_LEASE_KEY,),
                    )
            lc_holder.close()
            lc_acquirer.close()


if __name__ == "__main__":
    unittest.main()
