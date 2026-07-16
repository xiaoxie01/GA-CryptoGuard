from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger, log_path
from plugins.crypto_guard.run_ga_workers import run_once
from plugins.crypto_guard.run_scheduler import run_job
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db


_START_LOCK = threading.Lock()
_STARTED = False
_THREADS: list[threading.Thread] = []

# P1-2 R4: Warmup readiness gate — explicit 3-state machine.
#
# The old binary ``threading.Event`` was fail-open: it was set to True on
# BOTH success and failure, so a failed/degraded warmup let analysis
# proceed on bad data. The state machine fixes this:
#
#   "pending"  — warmup started but not yet finished (analysis deferred)
#   "ready"    — warmup succeeded AND data is not degraded (analysis allowed)
#   "failed"   — warmup raised, returned degraded=True, or timed out
#                (analysis deferred; next periodic warmup can recover to "ready")
#
# Only ``state == "ready"`` opens the gate. Exceptions, degraded results,
# and timeouts all transition to "failed" — the gate stays closed and
# ``enqueue_market_analysis`` returns a deferred result.
#
# Default is "ready" so tests/CLI that never call ``start_all_services``
# proceed normally. The readiness gate respects this default. In tests we
# don't run the warmup thread, so the default must allow analysis.
_WARMUP_STATE = "ready"  # one of "pending", "ready", "failed"
_WARMUP_LOCK = threading.Lock()
_WARMUP_STARTED_AT: float | None = None
_WARMUP_TIMEOUT_SECONDS = 600.0  # 10 minutes
_WARMUP_FAILURE_REASON: str = ""

# 07-13 R6-F (P1-1): cross-process service ownership lease. Prevents an
# accidental duplicate ``start_all_services()`` against the same DB from
# spawning two service thread sets. The lease is persisted in
# ``_service_ownership`` (single row, key=OWNERSHIP_LEASE_KEY). A live external
# owner (different PID, lease not expired, PID alive per the liveness probe)
# blocks a duplicate start and returns ``already_started_external`` with the
# owner's PID / DB path / release commit / owner identity for operator
# diagnosis. A stale lease (expired OR dead PID) is reclaimable (crash/restart
# recovery). The launcher (hub.pyw / fsapp.py) is NOT assumed to be the
# service owner -- only the process that successfully acquires the lease owns
# the service set. AC15: this logic lives entirely in service_manager.py; it
# does not modify hub.pyw.
#
# 07-14 R7-P0-3: the acquire path now uses an explicit ``BEGIN IMMEDIATE``
# transaction (not a read-then-INSERT-OR-REPLACE) so two concurrent processes
# cannot both read a stale owner and both win the write. ``BEGIN IMMEDIATE``
# takes a RESERVED lock immediately; the second acquire blocks on BEGIN until
# the first commits, then re-SELECTs and sees the fresh owner -> blocks. The
# lease also carries a per-process random ``owner_token`` (persisted alongside
# ``pid``) so the heartbeat renewal CAS keys on ``key + pid + owner_token``:
# PID alone is unsafe because an OS may recycle a dead PID onto a NEW process,
# which would then look like the same owner. A periodic heartbeat
# (``_renew_service_ownership_lease``) runs from the scheduler loop (every 20s,
# well under the 5-min TTL) so a long-running owner's lease never expires
# while it is alive -- pre-fix, ``start_all_services`` acquired ONCE at startup
# and never renewed, so after 5 min a second process reclaimed
# (``acquired: True``) and spawned a duplicate service set. AC15: hub.pyw is
# untouched; the heartbeat is driven from ``_scheduler_loop`` which this module
# already owns.
OWNERSHIP_LEASE_KEY = "service_ownership"
OWNERSHIP_LEASE_TTL_MS = 5 * 60 * 1000  # 5 minutes; renewed by the heartbeat
# Heartbeat cadence (seconds). Must be << OWNERSHIP_LEASE_TTL_MS so the lease
# is refreshed long before expiry. The scheduler loop already wakes every 20s;
# the heartbeat piggy-backs on each wake.
OWNERSHIP_HEARTBEAT_SECONDS = 20
# In-process cache of the lease this process holds (so a same-process
# reentrant start is the idempotent ``already_started`` path, not external).
# Carries ``owner_token`` so the heartbeat renewal CAS can prove ownership.
_OWNERSHIP_LEASE: dict[str, Any] | None = None
_OWNERSHIP_LOCK = threading.Lock()
# 07-15 R8-E (P1-3): one-way ownership-lost latch. Set by
# ``_renew_service_ownership_lease`` when the heartbeat reports ``reason="lost"``
# (the row was reclaimed by another process, or our PID was recycled and the
# token no longer matches). Once set, the three service loops
# (``_scheduler_loop`` / ``_user_worker_loop`` / ``_background_worker_loop``)
# STOP dispatching / claiming -- they break out of their ``while True`` so this
# owner no longer acts as owner (preventing a dual-owner condition where a
# second process reclaimed the lease while the old owner's loops kept claiming
# work). The latch is one-way: it is never cleared by the loops. A fresh owner
# is a fresh process (a new ``start_all_services``), which gets a clean Event
# (the module is re-imported) -- within one process the only transition is
# clear -> set. AC15: this lives entirely in service_manager.py.
_OWNERSHIP_LOST = threading.Event()
# PID liveness probe. Production uses os.kill(pid, 0) (returns True if the
# process exists). Tests inject a stub via set_pid_liveness_probe to simulate
# live/dead external owners without signalling real PIDs.
_PID_LIVENESS_PROBE: Callable[[int], bool] | None = None

LOGGER = get_logger("crypto_guard.service")


def set_pid_liveness_probe(probe: Callable[[int], bool] | None) -> None:
    """Inject a PID-liveness probe (test hook). Production path leaves this
    ``None`` and uses ``os.kill(pid, 0)``. A test passes ``lambda pid: True``
    to simulate a live external owner or ``lambda pid: False`` for a dead one.
    """
    global _PID_LIVENESS_PROBE
    with _OWNERSHIP_LOCK:
        _PID_LIVENESS_PROBE = probe


def _pid_alive(pid: int) -> bool:
    """Return True if process ``pid`` is currently alive. Uses the injected
    probe when set (tests), else ``os.kill(pid, 0)`` (production). On Windows,
    ``os.kill(pid, 0)`` raises ``PermissionError`` for a live process owned by
    another session -- treat that as alive; ``ProcessLookupError`` means dead.
    """
    probe = _PID_LIVENESS_PROBE
    if probe is not None:
        try:
            return bool(probe(pid))
        except Exception:
            return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Live process we cannot signal (different session / Windows ACL).
        return True
    except OSError:
        return False


def _release_commit() -> str:
    """Return a best-effort release/commit identifier for the running code. We
    try git describe of the plugin directory; if git is unavailable or the dir
    is not a repo, fall back to a sentinel so the field is never empty in
    production (operators need *something* to correlate a live lease to code).
    """
    try:
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here, capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return (out.stdout or "").strip() or "unknown_commit"
        return "unknown_commit"
    except Exception:
        return "unknown_commit"


def _owner_identity() -> str:
    """Return a human-readable owner identity for the lease (process cmdline
    head + user), so an operator can tell which process holds the lease."""
    try:
        argv0 = (sys.argv[0] if sys.argv and sys.argv[0] else "unknown")
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
        return f"{os.path.basename(argv0)}@{user}"
    except Exception:
        return "unknown_owner"


def acquire_service_ownership(conn: sqlite3.Connection, db_path: str) -> dict[str, Any]:
    """Atomically acquire (or reclaim) the service-ownership lease for ``db_path``.

    Returns one of:
      - ``{"acquired": True}`` -- this process now owns the lease (first start,
        or reclaimed a stale/dead lease). Caller MUST proceed to spawn threads.
      - ``{"acquired": False, "reason": "already_started_external",
         "owner_pid": ..., "owner_db_path": ..., "owner_release_commit": ...,
         "owner_identity": ...}`` -- a live external process owns the lease;
        caller MUST NOT spawn threads and MUST return this to the operator.
      - ``{"acquired": False, "reason": "already_started"}`` -- this same process
        already holds the lease (in-process reentrant start); idempotent.

    07-14 R7-P0-3: the check-and-update runs inside an explicit
    ``BEGIN IMMEDIATE`` transaction so two concurrent processes cannot both
    read a stale owner row and both win the write. ``BEGIN IMMEDIATE`` takes a
    RESERVED lock immediately; a second concurrent acquire blocks on its own
    ``BEGIN IMMEDIATE`` until the first commits, then re-SELECTs and observes
    the freshly written owner -> it must block (live external owner) or reclaim
    (now stale) under the *post-commit* row state. The previous read-then-
    ``INSERT OR REPLACE`` (no BEGIN) raced: both could read the stale row, both
    write, and both return ``acquired: True`` -> two service sets.

    The lease also carries a per-process random ``owner_token`` (persisted
    alongside ``pid``). The heartbeat renewal CAS keys on
    ``key + pid + owner_token``: PID alone is unsafe because an OS may recycle
    a dead PID onto a NEW process, which would then look like the same owner.
    A recycled-PID process has a different ``owner_token`` and cannot renew or
    claim the live owner's lease.
    """
    global _OWNERSHIP_LEASE
    now_ms = int(time.time() * 1000)
    lease_until_ms = now_ms + OWNERSHIP_LEASE_TTL_MS
    my_pid = os.getpid()
    commit = _release_commit()
    identity = _owner_identity()
    owner_token = secrets.token_hex(16)

    with _OWNERSHIP_LOCK:
        # In-process fast path: this process already holds the lease.
        if _OWNERSHIP_LEASE is not None and _OWNERSHIP_LEASE.get("pid") == my_pid:
            # Refresh lease_until so a long-running owner keeps it alive.
            # CAS on the cached owner_token so a recycled-PID same-process
            # mismatch (theoretically impossible here since pid+token are
            # both cached) cannot stomp an external owner.
            cached_token = _OWNERSHIP_LEASE.get("owner_token")
            conn.execute(
                "UPDATE _service_ownership SET lease_until_ms=? "
                "WHERE key=? AND owner_token=?",
                (lease_until_ms, OWNERSHIP_LEASE_KEY, cached_token),
            )
            conn.commit()
            _OWNERSHIP_LEASE["lease_until_ms"] = lease_until_ms
            return {"acquired": False, "reason": "already_started"}

        # Atomic acquire under BEGIN IMMEDIATE. The RESERVED lock blocks a
        # concurrent BEGIN IMMEDIATE from a second process until we COMMIT,
        # so the second process observes our freshly written row on its
        # re-SELECT. busy_timeout=5000 (sqlite_db.py) bounds the wait.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT pid, started_at_ms, db_path, release_commit, "
                "owner_identity, lease_until_ms, owner_token "
                "FROM _service_ownership WHERE key=?",
                (OWNERSHIP_LEASE_KEY,),
            ).fetchone()

            if row is not None:
                owner_pid = int(row["pid"])
                lease_until = int(row["lease_until_ms"])
                lease_expired = lease_until <= now_ms
                owner_live = _pid_alive(owner_pid)
                external = owner_pid != my_pid

                if external and owner_live:
                    # R7-P0-3: a LIVE external PID blocks a duplicate start
                    # REGARDLESS of lease expiry. Pre-fix, an expired lease
                    # (heartbeat missed/stalled) was silently reclaimable, which
                    # spawned a SECOND service set while the original owner's
                    # threads were still running -> duplicate analysis / orders.
                    # Now: lease expiry alone is NOT sufficient to reclaim when
                    # the PID is alive. The heartbeat keeps the lease fresh in
                    # normal operation; an expired lease + live PID is an
                    # operator signal that the owner is sick (heartbeat stalled)
                    # -- the operator reconciles (kill the hung owner; its PID
                    # then dies and reclaim succeeds). We hold a RESERVED lock
                    # here; release it (ROLLBACK) so the owner's heartbeat /
                    # operations are not blocked. The block is NOT silent: it
                    # logs a warning + returns structured already_started_external
                    # (with lease_expired flag) for operator diagnosis.
                    conn.execute("ROLLBACK")
                    LOGGER.warning(
                        "service ownership lease held by external process "
                        "pid=%s db=%s commit=%s owner=%s lease_until_ms=%s "
                        "lease_expired=%s -- duplicate start_all_services "
                        "blocked (already_started_external). If lease_expired "
                        "is true the owner's heartbeat stalled; operator must "
                        "reconcile (the live PID is NOT auto-reclaimed to "
                        "avoid a duplicate service set).",
                        owner_pid, row["db_path"], row["release_commit"],
                        row["owner_identity"], lease_until, lease_expired,
                    )
                    return {
                        "acquired": False,
                        "reason": "already_started_external",
                        "owner_pid": owner_pid,
                        "owner_db_path": row["db_path"],
                        "owner_release_commit": row["release_commit"],
                        "owner_identity": row["owner_identity"],
                        "lease_expired": lease_expired,
                    }
                # Reclaimable: external DEAD owner (crash/restart recovery --
                # the owner is provably gone, safe to reclaim even if the lease
                # has not yet expired), OR same-pid-but-cache-miss (the row's
                # pid equals ours but we hold no cached lease -- by PID
                # uniqueness among live processes this means the original owner
                # at this PID is dead and the PID was recycled to us; reclaim
                # is safe and starts ONE service set, not a duplicate). Reclaim
                # under the RESERVED lock.
                LOGGER.info(
                    "reclaiming service ownership lease old_pid=%s external=%s "
                    "lease_expired=%s owner_live=%s -> new_pid=%s",
                    owner_pid, external, lease_expired, owner_live, my_pid,
                )

            # Acquire / reclaim the lease atomically under the RESERVED lock.
            conn.execute(
                "INSERT OR REPLACE INTO _service_ownership"
                "(key, pid, started_at_ms, db_path, release_commit, "
                "owner_identity, lease_until_ms, owner_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (OWNERSHIP_LEASE_KEY, my_pid, now_ms, db_path, commit, identity,
                 lease_until_ms, owner_token),
            )
            conn.execute("COMMIT")
        except Exception:
            # Any failure inside the transaction: roll back so we never leave
            # a dangling RESERVED/EXCLUSIVE lock or a half-written row.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        _OWNERSHIP_LEASE = {
            "pid": my_pid, "db_path": db_path, "release_commit": commit,
            "owner_identity": identity, "lease_until_ms": lease_until_ms,
            "owner_token": owner_token,
        }
        # R8-E (P1-3): a fresh authoritative owner clears the ownership-lost
        # latch. In production a re-acquire is a fresh process (module re-import
        # -> clean Event); within one process (recovery / reentrant start) we
        # clear it explicitly so the loops resume claiming for the new owner.
        # The latch is only meaningful for a process that LOST a lease it held;
        # a process that just acquired is authoritative by definition.
        _OWNERSHIP_LOST.clear()
        LOGGER.info(
            "service ownership lease acquired pid=%s db=%s commit=%s owner=%s "
            "lease_until_ms=%s",
            my_pid, db_path, commit, identity, lease_until_ms,
        )
        return {"acquired": True}


def _renew_service_ownership_lease(conn: sqlite3.Connection) -> dict[str, Any]:
    """Heartbeat renewal of the lease this process holds (R7-P0-3).

    Called periodically from ``_scheduler_loop`` (every
    ``OWNERSHIP_HEARTBEAT_SECONDS``, well under the 5-min TTL) so a long-running
    owner's lease never expires while it is alive. The renewal is a CAS keyed on
    ``key + pid + owner_token``: it only succeeds if the row still belongs to
    THIS process (same pid AND same owner_token). A recycled-PID process has a
    different owner_token and its CAS matches 0 rows, so it cannot extend the
    live owner's lease -- this is the PID-recycle protection.

    Returns one of:
      - ``{"renewed": True, "lease_until_ms": ...}`` -- lease extended.
      - ``{"renewed": False, "reason": "not_held"}`` -- this process holds no
        cached lease (nothing to renew; e.g. this is a worker that never
        acquired ownership).
      - ``{"renewed": False, "reason": "lost"}`` -- the cached lease no longer
        matches the row (row reclaimed by another process, or our pid was
        recycled and the token no longer matches). The owner should treat this
        as a lost lease and stop acting as owner.
      - ``{"renewed": False, "reason": "error", "error": ...}`` -- DB error;
        the heartbeat should not crash the scheduler loop on a transient error.

    This function does NOT take ``_OWNERSHIP_LOCK`` to renew: it reads the
    cached ``_OWNERSHIP_LEASE`` snapshot once and CASes against the DB. The
    lock is only needed to mutate the cache, which we do under the lock on a
    successful renew.
    """
    global _OWNERSHIP_LEASE
    snapshot = _OWNERSHIP_LEASE
    if snapshot is None or snapshot.get("owner_token") is None:
        return {"renewed": False, "reason": "not_held"}
    now_ms = int(time.time() * 1000)
    lease_until_ms = now_ms + OWNERSHIP_LEASE_TTL_MS
    my_pid = os.getpid()
    token = snapshot["owner_token"]
    try:
        # CAS: only the row whose pid AND owner_token match this process gets
        # its lease_until_ms extended. A recycled-PID process with a different
        # owner_token matches 0 rows and the lease is NOT extended.
        cur = conn.execute(
            "UPDATE _service_ownership SET lease_until_ms=? "
            "WHERE key=? AND pid=? AND owner_token=?",
            (lease_until_ms, OWNERSHIP_LEASE_KEY, my_pid, token),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Row gone (reclaimed) or token mismatch (PID recycled). Our cached
            # lease is stale; clear it so the owner stops acting as owner.
            with _OWNERSHIP_LOCK:
                if _OWNERSHIP_LEASE is not None and \
                        _OWNERSHIP_LEASE.get("owner_token") == token:
                    _OWNERSHIP_LEASE = None
                # R8-E (P1-3): set the ownership-lost latch so the three service
                # loops stop dispatching / claiming. This is the fail-closed
                # signal: a lost lease means this process is no longer the
                # authoritative owner, so its loops must not claim work (else a
                # second process that reclaimed the lease creates a dual owner).
                _OWNERSHIP_LOST.set()
            LOGGER.warning(
                "service ownership lease lost during heartbeat pid=%s "
                "token=%s -- row reclaimed or PID recycled; service loops "
                "will stop claiming (R8-E fail-closed)",
                my_pid, token,
            )
            return {"renewed": False, "reason": "lost"}
        with _OWNERSHIP_LOCK:
            if _OWNERSHIP_LEASE is not None and \
                    _OWNERSHIP_LEASE.get("owner_token") == token:
                _OWNERSHIP_LEASE["lease_until_ms"] = lease_until_ms
        return {"renewed": True, "lease_until_ms": lease_until_ms}
    except Exception as exc:  # pragma: no cover - transient DB error path
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        LOGGER.error("service ownership heartbeat error pid=%s: %s", my_pid, exc)
        return {"renewed": False, "reason": "error", "error": str(exc)}


def release_service_ownership(conn: sqlite3.Connection) -> dict[str, Any]:
    """Release this process's service-ownership lease (07-15 R10-P1).

    Used on the EXCEPTION paths of ``start_all_services`` that fall between
    lease-acquire (:544) and thread-spawn. If ``initialize_database()`` or a
    recovery hook raises AFTER the lease was acquired, the lease row (stamped
    with this PID + a fresh ``owner_token``) and the in-process
    ``_OWNERSHIP_LEASE`` cache MUST be released -- otherwise the NEXT
    ``start_all_services`` call hits ``acquire_service_ownership``'s
    same-process fast path (:218 -- ``_OWNERSHIP_LEASE.pid == my_pid``) and
    returns ``already_started`` while NO threads were ever started, permanently
    locking the process out of its own service set.

    The release is a CAS keyed on ``pid + owner_token``: it only clears the row
    if it still belongs to THIS process (same pid AND same owner_token). This
    mirrors the heartbeat's CAS and preserves the PID-recycle protection: a
    recycled-PID process has a different ``owner_token`` and cannot clear the
    live owner's row. The in-process ``_OWNERSHIP_LEASE`` cache is cleared under
    ``_OWNERSHIP_LOCK`` only if its token still matches the released token.

    Safe to call when this process holds no lease (no-op, returns
    ``released=False, reason=not_held``). Idempotent: a second call after a
    successful release is a no-op.

    Returns one of:
      - ``{"released": True, "owner_token": ...}`` -- the row was cleared.
      - ``{"released": False, "reason": "not_held"}`` -- this process holds no
        cached lease (nothing to release).
      - ``{"released": False, "reason": "cas_mismatch", "owner_token": ...}`` --
        the cached lease no longer matches the row (already reclaimed by another
        process, or our PID was recycled). The cache is cleared so the next
        ``start_all_services`` re-acquires cleanly.
      - ``{"released": False, "reason": "error", "error": ...}`` -- DB error;
        the caller's exception path still propagates the original error.
    """
    global _OWNERSHIP_LEASE
    snapshot = _OWNERSHIP_LEASE
    if snapshot is None or snapshot.get("owner_token") is None:
        return {"released": False, "reason": "not_held"}
    my_pid = os.getpid()
    token = snapshot["owner_token"]
    try:
        # CAS: only the row whose pid AND owner_token match this process is
        # cleared. A recycled-PID process with a different owner_token matches
        # 0 rows and does NOT clear the live owner's row.
        cur = conn.execute(
            "DELETE FROM _service_ownership "
            "WHERE key=? AND pid=? AND owner_token=?",
            (OWNERSHIP_LEASE_KEY, my_pid, token),
        )
        conn.commit()
        released = cur.rowcount > 0
        # Clear the in-process cache regardless: this process is abandoning the
        # lease. If the CAS matched 0 rows (row already reclaimed / PID
        # recycled), the cache is stale anyway and MUST be cleared so the next
        # start_all_services re-acquires instead of hitting the fast path.
        with _OWNERSHIP_LOCK:
            if _OWNERSHIP_LEASE is not None and \
                    _OWNERSHIP_LEASE.get("owner_token") == token:
                _OWNERSHIP_LEASE = None
        if released:
            LOGGER.info(
                "service ownership lease released pid=%s token=%s "
                "(start_all_services exception path; threads never spawned) -- "
                "next start_all_services will re-acquire",
                my_pid, token,
            )
            return {"released": True, "owner_token": token}
        LOGGER.warning(
            "service ownership lease release CAS matched 0 rows pid=%s "
            "token=%s -- row already reclaimed or PID recycled; clearing "
            "stale cache so next start_all_services re-acquires",
            my_pid, token,
        )
        return {"released": False, "reason": "cas_mismatch", "owner_token": token}
    except Exception as exc:  # pragma: no cover - transient DB error path
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        # Even on a DB error, clear the cache defensively so the process is not
        # left locked onto a lease it could not prove it released.
        with _OWNERSHIP_LOCK:
            if _OWNERSHIP_LEASE is not None and \
                    _OWNERSHIP_LEASE.get("owner_token") == token:
                _OWNERSHIP_LEASE = None
        LOGGER.error(
            "service ownership lease release error pid=%s token=%s: %s -- "
            "cache cleared defensively",
            my_pid, token, exc,
        )
        return {"released": False, "reason": "error", "error": str(exc)}


def _set_warmup_started() -> None:
    """Called by ``start_all_services`` to mark the warmup race window open."""
    global _WARMUP_STARTED_AT, _WARMUP_FAILURE_REASON
    with _WARMUP_LOCK:
        _WARMUP_STATE_PENDING()  # transition to pending
        _WARMUP_STARTED_AT = time.time()
        _WARMUP_FAILURE_REASON = ""


def _WARMUP_STATE_PENDING() -> None:
    """Internal helper to set state=pending (assumes lock held)."""
    global _WARMUP_STATE
    _WARMUP_STATE = "pending"


def _set_warmup_ready() -> None:
    """Called when warmup succeeded AND data is not degraded."""
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON, _WARMUP_STARTED_AT
    with _WARMUP_LOCK:
        _WARMUP_STATE = "ready"
        _WARMUP_FAILURE_REASON = ""
        _WARMUP_STARTED_AT = None  # clear so timeout check doesn't fire


def _set_warmup_failed(reason: str) -> None:
    """Called on exception, degraded result, or timeout."""
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON, _WARMUP_STARTED_AT
    with _WARMUP_LOCK:
        _WARMUP_STATE = "failed"
        _WARMUP_FAILURE_REASON = str(reason)
        _WARMUP_STARTED_AT = None  # clear so timeout check doesn't fire


def is_warmup_complete() -> bool:
    """Check whether the warmup gate is open.

    Returns ``True`` ONLY when ``_WARMUP_STATE == "ready"``. The timeout
    fallback transitions to "failed" (not "ready") so analysis stays
    deferred until a subsequent periodic warmup succeeds.
    """
    global _WARMUP_STATE, _WARMUP_FAILURE_REASON
    # Check timeout first: if warmup has been pending longer than the
    # timeout, transition to failed (fail-closed, not fail-open).
    if _WARMUP_STARTED_AT is not None:
        elapsed = time.time() - _WARMUP_STARTED_AT
        if elapsed >= _WARMUP_TIMEOUT_SECONDS:
            with _WARMUP_LOCK:
                if _WARMUP_STATE == "pending":
                    _WARMUP_STATE = "failed"
                    _WARMUP_FAILURE_REASON = "timeout"
                    LOGGER.warning(
                        "warmup timeout (%.0fs elapsed >= %.0fs) — gate "
                        "transitions to failed; analysis stays deferred",
                        elapsed, _WARMUP_TIMEOUT_SECONDS,
                    )
    with _WARMUP_LOCK:
        return _WARMUP_STATE == "ready"


def get_warmup_state() -> str:
    """Return the current warmup state string for diagnostics."""
    with _WARMUP_LOCK:
        return _WARMUP_STATE


def get_warmup_failure_reason() -> str:
    """Return the failure reason (empty string if not failed)."""
    with _WARMUP_LOCK:
        return _WARMUP_FAILURE_REASON


def start_all_services(*, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """随飞书入口启动 CryptoGuard 后台服务。

    这些线程都只做轻量轮询和任务入队/消费；飞书 event handler 仍然快速 ACK，
    用户消息通过 priority=1 的 agent_jobs 优先处理。
    """

    global _STARTED
    if os.environ.get("CRYPTO_GUARD_AUTOSTART", "1").lower() in {"0", "false", "no"}:
        return {"ok": True, "started": False, "reason": "CRYPTO_GUARD_AUTOSTART disabled"}

    with _START_LOCK:
        if _STARTED:
            return {"ok": True, "started": False, "reason": "already_started", "threads": [t.name for t in _THREADS]}

        cfg = load_config()

        # 07-15 R9-P0 (ownership-first startup): acquire the cross-process
        # service-ownership lease BEFORE running ``initialize_database()`` or any
        # recovery/migration. Pre-R9 the sequence was
        # ``initialize_database() -> recover_stale_running_jobs ->
        # recover_stale_prepared_skill_logs -> acquire_service_ownership``: a
        # DUPLICATE non-owner process ran ALL schema migrations + seed + marker
        # writes and mutated agent_jobs/skill_execution_logs, and ONLY THEN
        # returned ``already_started_external``. That is wrong -- a non-owner
        # must make ZERO DB change. The fix reorders to:
        #   1. minimal init of ONLY the ownership table (additive CREATE TABLE
        #      IF NOT EXISTS) so ``acquire_service_ownership`` can read/write it;
        #   2. atomically acquire the lease;
        #   3. non-owner (``already_started_external``) -> return IMMEDIATELY
        #      with ZERO further DB change (no full migrations, no job recovery,
        #      no skill-log recovery);
        #   4. owner (``acquired: True``) -> run full ``initialize_database()``
        #      + recovery, then proceed to spawn threads.
        # The same-process reentrant case (``already_started``) and the
        # ``CRYPTO_GUARD_AUTOSTART`` disabled early-return above are unchanged.
        # ``_START_LOCK`` still serializes concurrent calls in THIS process;
        # cross-process races are arbitrated by the DB lease row + PID-liveness
        # check.
        own_conn = connect_db(cfg.database_path)
        try:
            # Minimal additive init of the ownership lease table ONLY. This is
            # a CREATE TABLE IF NOT EXISTS (apply_r6f_service_ownership_migration)
            # that touches no business rows and no markers -- it just guarantees
            # the lease row exists so the CAS below can read/write it. The full
            # ``initialize_database()`` (which writes ALL migrations + seeds +
            # contract markers) runs later and ONLY for the owner.
            from plugins.crypto_guard.storage.migrations import apply_r6f_service_ownership_migration
            apply_r6f_service_ownership_migration(own_conn)
            own_conn.commit()
            ownership = acquire_service_ownership(own_conn, str(cfg.database_path))
        finally:
            own_conn.close()
        if not ownership.get("acquired"):
            reason = ownership.get("reason")
            if reason == "already_started":
                # Same process already started (lease held by this PID). Fall
                # through to the in-process _STARTED guard below / set state.
                return {"ok": True, "started": False, "reason": "already_started",
                        "threads": [t.name for t in _THREADS]}
            if reason == "already_started_external":
                # R9-P0: a live external owner holds the lease. Return
                # IMMEDIATELY with ZERO further DB change -- the full
                # ``initialize_database()``, ``recover_stale_running_jobs``, and
                # ``recover_stale_prepared_skill_logs`` MUST NOT run for a
                # non-owner (they mutate the DB). Pre-R9 they ran here and
                # mutated migrations/jobs/skill-logs before the reject return.
                return {"ok": True, "started": False, "reason": "already_started_external",
                        "owner_pid": ownership.get("owner_pid"),
                        "owner_db_path": ownership.get("owner_db_path"),
                        "owner_release_commit": ownership.get("owner_release_commit"),
                        "owner_identity": ownership.get("owner_identity"),
                        "threads": [t.name for t in _THREADS]}

        # Owner path (acquired: True): NOW run the full schema migrations,
        # seeds, contract markers, and crash-recovery hooks. These mutate the
        # DB and MUST run for the authoritative owner ONLY.
        #
        # 07-15 R10-P1 (ownership release on init/recovery failure): ALL
        # exception paths between lease-acquire (:544) and thread-spawn MUST
        # release the lease (clear the ``_service_ownership`` DB row via a
        # ``pid + owner_token`` CAS + clear the in-process ``_OWNERSHIP_LEASE``
        # cache). Pre-R10 the init/recovery block had NO try/except: an
        # ``initialize_database()`` or ``recover_stale_*`` raise propagated
        # while the lease row (stamped with this PID) and the ``_OWNERSHIP_LEASE``
        # cache stayed populated. The NEXT ``start_all_services`` then hit
        # ``acquire_service_ownership``'s same-process fast path (:218 --
        # ``_OWNERSHIP_LEASE.pid == my_pid``) and returned ``already_started``
        # even though NO threads were ever started -> the process was
        # permanently locked out of its own service set. The release below runs
        # on a FRESH connection (the owner-path ``conn`` above may be mid-
        # exception / closed) and re-raises the original error so the caller
        # still sees the init failure; the lease is simply cleaned up so the
        # operator's retry can re-acquire. Recent project history had a real
        # ``initialize_database()`` failure, so this is not theoretical.
        try:
            init_result = initialize_database(cfg)
            LOGGER.info("CryptoGuard autostart initializing database path=%s log=%s", cfg.database_path, log_path())
            conn = connect_db(cfg.database_path)
            try:
                recovered = CryptoGuardRepository(conn).recover_stale_running_jobs(older_than_minutes=30)
                if recovered:
                    LOGGER.warning("Recovered stale running agent_jobs count=%s", recovered)
                # 07-14 R8 P2-NEW-1 (contract #4): terminalize crash-residue
                # ``prepared`` skill_execution_logs left behind when a producer died
                # between Phase 1 (prepared autocommit write) and Phase 2 (commit/
                # abort). A hard crash kills the process before the except-block
                # abort path runs, so the rows survive as ``prepared`` audit. This
                # restart hook marks any ``prepared`` row older than the staleness
                # threshold (default 600s -- far longer than any producer tick) as
                # ``aborted`` so it stays excluded from learning (contract #5) yet
                # is no longer a silent stuck-state. Deferred import avoids any
                # import-cycle risk with cron_scheduler.
                from plugins.crypto_guard.scheduler.cron_scheduler import recover_stale_prepared_skill_logs
                skill_recovered = recover_stale_prepared_skill_logs(conn, stale_after_seconds=600)
                terminalized = int(skill_recovered.get("terminalized_prepared_to_aborted") or 0)
                if terminalized:
                    LOGGER.warning(
                        "Recovered stale prepared skill_execution_logs count=%s "
                        "(terminalized to 'aborted', excluded from learning)",
                        terminalized,
                    )
            finally:
                conn.close()
        except Exception:
            # R10-P1: release the lease on ANY init/recovery failure so the
            # next start_all_services re-acquires instead of locking onto the
            # stale lease. ``release_service_ownership`` is a pid+owner_token
            # CAS (preserves PID-recycle protection) + cache clear. Use a fresh
            # connection so a half-open owner-path conn does not interfere.
            try:
                rel_conn = connect_db(cfg.database_path)
                try:
                    release_service_ownership(rel_conn)
                finally:
                    rel_conn.close()
            except Exception as rel_exc:  # pragma: no cover - defensive
                LOGGER.error(
                    "R10-P1: failed to release service ownership lease on "
                    "init/recovery failure (original error will propagate): %s",
                    rel_exc,
                )
            raise

        # R5: Run market-data warmup once at startup before the scheduler loop.
        # This backfills any gaps from downtime so the first analysis tick has
        # healthy data. P1-9: runs in a background thread so it doesn't block
        # startup or delay the scheduler/worker threads from starting. The
        # scheduler loop will retry on the next tick if the warmup fails.
        # P1-2 R4: Transition to "pending" so enqueue_market_analysis defers
        # analysis until warmup finishes. The warmup thread transitions to
        # "ready" (success, no degradation) or "failed" (exception/degraded).
        # The periodic market_data_warmup cron job (every 5 min) can recover
        # from "failed" to "ready" on a subsequent successful run.
        _set_warmup_started()

        def _warmup_bg(_=None) -> None:
            # P1-2 R4: on entry, state is "pending" (set by _set_warmup_started).
            # market_data_warmup() now handles the state transitions internally:
            #   - success (no degradation) → _set_warmup_ready()
            #   - degraded → _set_warmup_failed("degraded")
            #   - exception → _set_warmup_failed(str(exc))
            # The finally block below is a safety net: if market_data_warmup
            # returns without setting state (shouldn't happen, but defensive),
            # transition to "failed" so the gate doesn't stay in "pending".
            try:
                from plugins.crypto_guard.scheduler.cron_scheduler import market_data_warmup
                warmup_result = market_data_warmup()
                if warmup_result.get("degraded"):
                    LOGGER.warning(
                        "CryptoGuard startup market_data_warmup: degraded=%s symbols=%s",
                        warmup_result.get("degraded"),
                        list(warmup_result.get("symbols", {}).keys()),
                    )
                else:
                    LOGGER.info("CryptoGuard startup market_data_warmup: all TFs ready")
            except Exception as exc:
                LOGGER.exception("CryptoGuard startup market_data_warmup failed; scheduler will retry on next tick")
                _set_warmup_failed(str(exc))
            finally:
                # P1-2 R4: if state is still "pending" (e.g. market_data_warmup
                # returned without transitioning state, or an early return path
                # didn't set it), transition to "failed" so the gate doesn't
                # stay closed forever. The next periodic warmup job can
                # recover to "ready".
                if get_warmup_state() == "pending":
                    _set_warmup_failed("incomplete")

        _spawn("crypto_guard_warmup", _warmup_bg, None)

        _spawn("crypto_guard_user_worker", _user_worker_loop, send_message)
        _spawn("crypto_guard_background_worker", _background_worker_loop, send_message)
        # R7-P0-3: pass the DB path to the scheduler loop so it can renew the
        # service-ownership lease each tick (heartbeat) without re-reading
        # config on every wake.
        _spawn("crypto_guard_scheduler", _scheduler_loop, str(cfg.database_path))

        _STARTED = True
        LOGGER.info("CryptoGuard services started threads=%s", [t.name for t in _THREADS])
        return {"ok": True, "started": True, "init": init_result, "threads": [t.name for t in _THREADS]}


def is_started() -> bool:
    return _STARTED


def _spawn(name: str, target: Callable[..., None], arg: Any) -> None:
    thread = threading.Thread(target=target, args=(arg,), name=name, daemon=True)
    thread.start()
    _THREADS.append(thread)
    LOGGER.info("Started background thread name=%s", name)


def _user_worker_loop(send_message: Callable[..., Any] | None) -> None:
    while True:
        # R8-E (P1-3): fail-closed on ownership-lost. This loop has no heartbeat
        # of its own; it relies on the scheduler's heartbeat having set
        # ``_OWNERSHIP_LOST``. When lost, this process is no longer the
        # authoritative owner -- STOP claiming new work (break out). A
        # ``run_once`` already in flight from a prior iteration is allowed to
        # finish; only NEW claims stop. Pre-R8-E this loop had no ownership
        # awareness and claimed forever, so a second process that reclaimed the
        # lease left this loop still claiming (dual owner).
        if _OWNERSHIP_LOST.is_set():
            LOGGER.warning(
                "user_worker loop stopping: service ownership lost (R8-E); "
                "this process is no longer the authoritative owner."
            )
            return
        try:
            result = run_once(user_only=True, send_message=send_message)
            if result.get("processed"):
                LOGGER.info("user_worker processed job_id=%s result_ok=%s", result.get("job_id"), (result.get("result") or {}).get("ok"))
        except Exception:
            LOGGER.exception("user_worker loop failed")
            traceback.print_exc()
        time.sleep(0.5)


def _background_worker_loop(send_message: Callable[..., Any] | None) -> None:
    while True:
        # R8-E (P1-3): fail-closed on ownership-lost (see _user_worker_loop).
        if _OWNERSHIP_LOST.is_set():
            LOGGER.warning(
                "background_worker loop stopping: service ownership lost "
                "(R8-E); this process is no longer the authoritative owner."
            )
            return
        try:
            result = run_once(background=True, send_message=send_message)
            if result.get("processed"):
                LOGGER.info("background_worker processed job_id=%s result_ok=%s", result.get("job_id"), (result.get("result") or {}).get("ok"))
        except Exception:
            LOGGER.exception("background_worker loop failed")
            traceback.print_exc()
        time.sleep(1.5)


def _scheduler_loop(db_path: Any = None) -> None:
    # R7-P0-3: ``db_path`` is the string DB path passed by ``start_all_services``
    # (was ``None`` pre-R7-P0-3). The scheduler loop wakes every 20s, which is
    # well under the 5-min OWNERSHIP_LEASE_TTL_MS, so each wake is the natural
    # heartbeat point: renew the lease so a long-running owner never expires
    # while alive. Pre-fix, ``start_all_services`` acquired the lease ONCE at
    # startup and never renewed it, so after 5 min a second process reclaimed
    # and spawned a duplicate service set.
    #
    # R8-E (P1-3): a lost/reclaimed lease is now FAIL-CLOSED. The heartbeat's
    # ``lost`` branch sets the module-level ``_OWNERSHIP_LOST`` Event (one-way
    # latch). At the top of every wake, if the Event is set, the scheduler
    # STOPS dispatching due jobs (it still logs the lost-lease once) and breaks
    # out of the loop -- this process is no longer authoritative, so it must
    # not tick the scheduler while a second process owns the lease (dual owner).
    # Pre-R8-E the scheduler only set a local ``heartbeat_warned_lost`` flag and
    # then KEPT running every due job -- a reclaimed owner kept ticking. A
    # ``run_job`` already in flight is allowed to finish; only NEW ticks stop.
    last_tick: dict[str, int] = {}
    heartbeat_warned_lost = False
    while True:
        # R8-E (P1-3): fail-closed on ownership-lost. The ``lost`` latch is set
        # by ``_renew_service_ownership_lease`` (either this loop's own heartbeat
        # or -- for the worker loops that have no heartbeat -- this loop's). When
        # set, stop dispatching and exit so we do not dual-own with the new
        # owner.
        if _OWNERSHIP_LOST.is_set():
            if not heartbeat_warned_lost:
                LOGGER.error(
                    "scheduler stopping: service ownership lost (R8-E) -- "
                    "another process reclaimed it or the PID was recycled; "
                    "this owner is no longer authoritative and will stop "
                    "dispatching due jobs. Operator must reconcile."
                )
                heartbeat_warned_lost = True
            return
        try:
            # R7-P0-3: renew the lease first, before running any due job, so
            # the owner stays authoritative across a long job. A short-lived
            # connection per wake is fine -- the renewal is a single CAS UPDATE.
            if db_path:
                try:
                    hb_conn = connect_db(db_path)
                    try:
                        hb = _renew_service_ownership_lease(hb_conn)
                    finally:
                        hb_conn.close()
                    if not hb.get("renewed") and hb.get("reason") == "lost" \
                            and not heartbeat_warned_lost:
                        LOGGER.error(
                            "scheduler detected lost service ownership lease "
                            "(reason=%s) -- another process reclaimed it or the "
                            "PID was recycled; this owner is no longer "
                            "authoritative. Stopping dispatch (R8-E). Operator "
                            "must reconcile.",
                            hb.get("reason"),
                        )
                        heartbeat_warned_lost = True
                    elif hb.get("renewed"):
                        heartbeat_warned_lost = False
                except Exception:
                    LOGGER.exception("service ownership heartbeat failed")
                    traceback.print_exc()
                # R8-E P1-1 (07-14 follow-up): the heartbeat may have set the
                # ``_OWNERSHIP_LOST`` latch DURING this iteration (a ``lost``
                # renewal sets it inside ``_renew_service_ownership_lease``).
                # The top-of-loop check at line 690 ran BEFORE the heartbeat, so
                # it cannot catch a lost detected mid-iteration -- without this
                # re-check the loop would fall through to ``_due_scheduler_jobs``
                # and dispatch in the SAME iteration where it just learned it
                # is no longer the owner (one-tick dual-owner window while the
                # new owner is also dispatching). Stop immediately so only the
                # authoritative owner ticks the scheduler.
                if _OWNERSHIP_LOST.is_set():
                    if not heartbeat_warned_lost:
                        LOGGER.error(
                            "scheduler stopping: service ownership lost during "
                            "heartbeat (R8-E P1-1) -- dispatch skipped for this "
                            "tick; another process reclaimed the lease or the PID "
                            "was recycled. Operator must reconcile."
                        )
                        heartbeat_warned_lost = True
                    return
            now = datetime.now(timezone.utc)
            due_jobs = _due_scheduler_jobs(now)
            for job_name in due_jobs:
                tick_key = _tick_key(job_name, now)
                if last_tick.get(job_name) == tick_key:
                    continue
                last_tick[job_name] = tick_key
                try:
                    LOGGER.info("scheduler running job=%s tick=%s", job_name, tick_key)
                    run_job(job_name)
                    LOGGER.info("scheduler finished job=%s tick=%s", job_name, tick_key)
                except Exception:
                    LOGGER.exception("scheduler job failed job=%s tick=%s", job_name, tick_key)
                    traceback.print_exc()
        except Exception:
            LOGGER.exception("scheduler loop failed")
            traceback.print_exc()
        time.sleep(OWNERSHIP_HEARTBEAT_SECONDS)


def _due_scheduler_jobs(now: datetime) -> list[str]:
    jobs: list[str] = []
    minute = now.minute
    hour = now.hour
    # P0 (R4): analyze_market_15m must be dispatched before hourly_feishu_report
    # so the analysis batch exists when the report checks for it.
    if minute in {1, 16, 31, 46}:
        jobs.append("fetch_15m_klines")
    if minute % 5 == 1:
        jobs.append("fetch_5m_klines")
    if minute in {1, 16, 31, 46}:
        jobs.append("analyze_market_15m")
    if minute in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}:
        jobs.append("hourly_feishu_report")
    jobs.append("alert_outbox_retry")
    if minute == 1:
        jobs.append("fetch_1h_klines")
        if hour in {0, 4, 8, 12, 16, 20}:
            jobs.append("fetch_4h_klines")
        if hour == 0:
            jobs.append("fetch_1d_klines")
    if minute in {3, 18, 33, 48}:
        jobs.append("update_opportunity_watches")
    if minute % 3 == 0:
        jobs.append("update_paper_positions_3m")
    # Pending order lifecycle: TTL expiry + conflict cancellation (every 60 minutes)
    if minute == 0:
        jobs.append("pending_order_management")
    # Pending order revalidation: multi-dimensional review (every 60 minutes, offset by 15)
    if minute == 15:
        jobs.append("pending_order_revalidation")
    # Position conflict revalidation: every 10 minutes at minute % 10 == 5
    if minute % 10 == 5:
        jobs.append("position_conflict_revalidation")
    # Shadow virtual trade update: every minute
    jobs.append("shadow_virtual_trade_update")
    # R5: market-data warmup — runs every 5 min before analysis to backfill gaps
    if minute % 5 == 0:
        jobs.append("market_data_warmup")
    # Daily review: run between 00:05-00:30 UTC (wider window for crash recovery)
    # _tick_key ensures it only runs once per day
    if hour == 0 and 5 <= minute <= 30:
        jobs.append("daily_review")
    return jobs


def _tick_key(job_name: str, now: datetime) -> int:
    if job_name == "analyze_market_15m":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "update_opportunity_watches":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "fetch_15m_klines":
        return int(now.timestamp()) // (15 * 60)
    if job_name == "fetch_5m_klines":
        return int(now.timestamp()) // (5 * 60)
    if job_name == "fetch_1h_klines":
        return int(now.timestamp()) // 3600
    if job_name == "hourly_feishu_report":
        return int(now.timestamp()) // 3600
    if job_name == "alert_outbox_retry":
        return int(now.timestamp()) // 60
    if job_name == "fetch_4h_klines":
        return int(now.timestamp()) // (4 * 3600)
    if job_name == "update_paper_positions_3m":
        return int(now.timestamp()) // (3 * 60)
    if job_name == "position_conflict_revalidation":
        return int(now.timestamp()) // (10 * 60)
    if job_name == "shadow_virtual_trade_update":
        return int(now.timestamp()) // 60
    if job_name == "market_data_warmup":
        return int(now.timestamp()) // (5 * 60)
    return int(now.timestamp()) // 86400
