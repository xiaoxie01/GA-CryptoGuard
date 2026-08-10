# -*- coding: utf-8 -*-
"""5.4 rollback_isolation RED contract (Step 5, test-feedback acceleration).

Contract under test (``@pytest.mark.rollback_isolation`` + pg_fixtures):
  1. An opted-in test's ``make_repo()`` resolves to ONE shared per-worker
     schema (the DDL runs once per worker, not once per test).
  2. Writes from one opted-in test are ROLLED BACK before the next — the next
     test sees the clean baseline, with NO schema drop and NO per-test reset.
  3. Within one opted-in test, its own writes stay visible (nested savepoints
     inside the outer transaction).
  4. ``make_repo()``'s DEFAULT per-test fresh-schema isolation is UNCHANGED for
     non-marked tests (distinct schemas, writes isolated by schema drop).
  5. ``rollback_active()`` is only True inside a marked test (fixture resets).

RED-first: before the pg_fixtures rollback machinery exists, test #1 fails
(schemas differ) while the DEFAULT-isolation guard stays green.
"""
from __future__ import annotations

import psycopg_pool
import pytest

from plugins.crypto_guard.tests import pg_fixtures as fx

pytestmark = [pytest.mark.pg]


def _decision() -> dict:
    return {
        "symbol": "BTCUSDT",
        "analysis_time": 1723000000000,
        "analysis_time_utc": "2026-08-08T00:00:00Z",
        "decision_type": "opportunity_watch_recheck",
        "signal_grade": "S",
        "confidence": 0.9,
        "market_bias": "bullish",
        "trend_stage": "early",
        "decision": "trade_plan_available",
        "skill_result_refs": {},
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "rb-contract",
        "raw_llm_summary": "rb-contract",
        "rendered_summary": "rb-contract",
        "batch_id": None,
        "previous_grade": "D",
        "llm_status": "ok",
    }


def _count_decisions(handle) -> int:
    row = handle.conn.execute(
        "SELECT count(*) AS n FROM ga_decisions"
    ).fetchone()
    return int(row["n"])


@pytest.mark.rollback_isolation
class TestRollbackIsolationOptIn:
    """The 5.4 fast path: shared per-worker schema + per-test rollback."""

    def test_marked_make_repo_shares_worker_schema(self):
        h1 = fx.make_repo()
        s1 = h1.schema
        h1.close()
        h2 = fx.make_repo()
        try:
            # RED discriminator: before the machinery exists these are two
            # fresh per-test schemas; under rollback isolation one shared
            # per-worker schema serves every opted-in test.
            assert h2.schema == s1
        finally:
            h2.close()

    def test_writes_rolled_back_between_tests(self):
        h1 = fx.make_repo()
        try:
            h1.repo.create_ga_decision(_decision())
            assert _count_decisions(h1) == 1  # own write visible in-test
        finally:
            h1.close()
        h2 = fx.make_repo()
        try:
            # close() rolled the whole outer transaction back: no schema drop,
            # no reset, but the next test still sees the clean baseline.
            assert h2.schema == h1.schema  # same shared worker schema
            assert _count_decisions(h2) == 0
        finally:
            h2.close()

    def test_own_writes_visible_within_test(self):
        h = fx.make_repo()
        try:
            h.repo.create_ga_decision(_decision())
            # Nested savepoint inside the outer txn: visible to this test.
            assert _count_decisions(h) == 1
        finally:
            h.close()


@pytest.mark.rollback_isolation
def test_rollback_active_true_only_inside_marker():
    assert fx.rollback_active() is True


class TestDefaultIsolationUnchanged:
    """Non-marked tests keep the DEFAULT fresh per-test schema isolation."""

    def test_make_repo_fresh_per_test_schema_unchanged(self):
        assert fx.rollback_active() is False  # fixture reset after marked tests
        h1 = fx.make_repo()
        s1 = h1.schema
        h1.close()
        h2 = fx.make_repo()
        try:
            # DEFAULT: distinct fresh schemas, writes isolated by schema drop.
            assert h2.schema != s1
            assert _count_decisions(h2) == 0
        finally:
            h2.close()

    def test_rollback_repo_explicit_contract(self):
        # The explicit fast path is also callable without the marker; it still
        # shares the worker schema and rolls back between checkouts.
        assert fx.rollback_active() is False
        h1 = fx.rollback_repo()
        s1 = h1.schema
        try:
            h1.repo.create_ga_decision(_decision())
            assert _count_decisions(h1) == 1
        finally:
            h1.close()
        h2 = fx.rollback_repo()
        try:
            assert h2.schema == s1
            assert _count_decisions(h2) == 0
        finally:
            h2.close()


class _BoomHandle:
    """A rollback handle whose close() fails (broken connection)."""

    def close(self) -> None:
        raise RuntimeError("cleanup failed")


class _OkHandle:
    closed = False

    def close(self) -> None:
        self.closed = True


def _set_handle(monkeypatch, handle):
    monkeypatch.setattr(fx, "_ROLLBACK_HANDLE",
                        type("H", (), {"value": handle}))


def test_safe_teardown_preserves_primary_failure(monkeypatch):
    """A cleanup exception after a failed test must NOT mask the primary one."""
    _set_handle(monkeypatch, _BoomHandle())
    # Primary failure present -> cleanup error suppressed (logged, not raised).
    fx.safe_close_open_rollback_handle(ValueError("primary"))


def test_safe_teardown_surfaces_cleanup_error_on_clean_test(monkeypatch):
    """On a clean test a cleanup failure IS the real error -> surfaced."""
    _set_handle(monkeypatch, _BoomHandle())
    with pytest.raises(RuntimeError):
        fx.safe_close_open_rollback_handle(None)


def test_safe_teardown_closes_handle(monkeypatch):
    handle = _OkHandle()
    _set_handle(monkeypatch, handle)
    fx.safe_close_open_rollback_handle(None)
    assert handle.closed


def test_safe_teardown_noop_without_open_handle(monkeypatch):
    _set_handle(monkeypatch, None)
    fx.safe_close_open_rollback_handle(ValueError("primary"))


def _establish_rollback_pool() -> None:
    """Open the shared per-worker rollback schema + dedicated pool once."""
    h = fx.rollback_repo()
    h.close()


def test_rollback_pool_opened_once_across_handles(monkeypatch):
    """RED: sequential rollback handles add ZERO pool opens.

    The per-worker rollback pool is opened exactly once (lazily, at the first
    checkout) and then REUSED by every subsequent checkout. The old code
    re-created the process pool on every rollback close() (a per-test
    ``pg_db.reset_pool()``), so two sequential handles paid ``pool.open()``
    twice — the 3s pool-open churn behind the 08-09 PoolTimeout flake. The
    fixed code opens it zero additional times: the worker-setup checkout above
    already opened it, and ``close()`` only rolls back + returns the
    connection.
    """
    _establish_rollback_pool()

    opens = {"n": 0}
    real_open = psycopg_pool.ConnectionPool.open

    def counting_open(self, *args, **kwargs):
        opens["n"] += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(psycopg_pool.ConnectionPool, "open", counting_open)

    h1 = fx.rollback_repo()
    try:
        h1.repo.create_ga_decision(_decision())
    finally:
        h1.close()
    h2 = fx.rollback_repo()
    try:
        assert h2.schema == h1.schema
        assert _count_decisions(h2) == 0  # h1's write rolled back
    finally:
        h2.close()

    assert opens["n"] == 0


def test_rollback_cleanup_never_closes_worker_pool(monkeypatch):
    """RED: failure cleanup (safe_close) must NOT close the worker pool.

    The old ``close()`` ended with ``pg_db.reset_pool()``, closing the process
    pool on every test — including the failure-cleanup path, so a failed test
    destroyed the pool the next rollback test would have to re-open. The fixed
    ``close()`` only rolls back and returns the connection to the dedicated
    per-worker rollback pool, which stays open until worker teardown.
    """
    _establish_rollback_pool()

    closes = {"n": 0}
    real_close = psycopg_pool.ConnectionPool.close

    def counting_close(self, *args, **kwargs):
        closes["n"] += 1
        return real_close(self, *args, **kwargs)

    monkeypatch.setattr(psycopg_pool.ConnectionPool, "close", counting_close)

    h = fx.rollback_repo()
    # Simulate a failed test: the conftest safety net closes the open handle.
    fx.safe_close_open_rollback_handle(ValueError("primary"))
    assert closes["n"] == 0


def test_atexit_drops_both_shared_schemas(tmp_path):
    """P2-6 RED: process exit must drop the reusable schema too.

    The 08-09 rollback-pool work replaced the baseline
    ``atexit.register(_drop_reusable_schema)`` with
    ``atexit.register(_drop_rollback_schema)``, so the reusable schema (every
    ``make_reusable_repo`` site in ``_smoke_suite``) was no longer dropped at
    exit and accumulated across runs. A subprocess stubs ``atexit.register``
    BEFORE importing pg_fixtures and records exactly which drop handlers the
    module registers at import; BOTH must be present. Each drop is
    lock-guarded and no-ops when its schema was never created, so the two
    registrations are independent and safe.
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    code = textwrap.dedent(f"""
        import atexit
        captured = []
        real_register = atexit.register

        def recording_register(func, *args, **kwargs):
            captured.append(func)
            return real_register(func, *args, **kwargs)

        atexit.register = recording_register

        from plugins.crypto_guard.tests import pg_fixtures as fx

        atexit.register = real_register
        assert fx._drop_rollback_schema in captured, (
            "rollback atexit drop missing")
        assert fx._drop_reusable_schema in captured, (
            "reusable atexit drop missing (P2-6): reusable schema accumulates "
            "across runs")
    """)
    subprocess.run([sys.executable, "-c", code], check=True, cwd=repo_root)
