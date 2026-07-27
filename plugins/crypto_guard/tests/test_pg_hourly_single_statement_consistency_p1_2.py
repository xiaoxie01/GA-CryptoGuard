"""终审返工 R2 P1-2 (2026-07-26): single-statement consistent hourly snapshot.

RED-first test for the Codex re-review finding:

  ``repository.hourly_scheduled_analysis_distribution`` previously executed TWO
  ``cur.execute`` calls (one for ``total_decisions``/``batch_count``, one for
  the per-``signal_grade`` GROUP BY). Under PostgreSQL's default READ COMMITTED
  isolation, each statement gets its OWN snapshot of committed rows. A backend
  that commits a new ``scheduled_analysis`` row in the window BETWEEN the two
  executes makes that row visible to exactly ONE statement: ``total_decisions``
  (statement 1) could read 10 while ``sum(signal_distribution.values())``
  (statement 2) reads 9 (or 11) - a self-inconsistent aggregate that no Python
  post-hoc reconciliation can prove.

  The contract (verbatim from the re-review):
    - 改为单条 SQL/CTE，一次 execute 同时返回：total_decisions、distinct non-null
      batch_count、signal_grade/count.
    - 零行返回 total=0, batch_count=0, distribution={}.
    - 保留 decision_type='scheduled_analysis' only + [start,end) 窗口.
    - 新增强制并发/语句间写入回归测试，证明旧两查询实现会产生
      total_decisions != sum(distribution)，新单语句实现始终相等.
    - 不接受仅在 Python 中事后修改 total 来掩盖竞态.

How this test forces the race on the OLD two-query code WITHOUT timing luck:

  The test does NOT rely on wall-clock timing to win the race. Instead it
  instruments the repository's cursor: the wrapper counts executes and fires a
  hook exactly once, immediately AFTER execute #1 completes. The hook commits
  a new scheduled row via ``direct_conn`` (a second, non-pooled backend on the
  same schema). On the OLD two-query body that instant is the boundary between
  execute #1 (total/batch) and execute #2 (distribution), so execute #2 sees
  the new row but execute #1 did not.

  On the OLD two-query code this yields total_decisions == N (execute #1
  snapshot, no new row) but sum(distribution) == N+1 (execute #2 snapshot, new
  row visible) -> the inconsistency the re-review names.

  终审返工 R3 P2-2 (2026-07-26) - honest hook semantics: the hook fires AFTER
  execute #1 completes on BOTH implementations (``fire_after_exec`` is the
  constructor threshold - always 1 here - not a counter; ``hook_fired`` is
  True on the new path too). What differs is WHERE that instant falls. On the
  OLD code it falls between statement 1 and statement 2, so statement 2's
  fresh READ COMMITTED snapshot sees the injected commit. On the NEW code the
  lone statement has ALREADY returned its buffered result when the hook runs:
  the injected commit postdates the statement's snapshot and is invisible to
  it. The injection is NOT "a no-op because it never runs" - it runs and
  commits; it simply cannot retroactively enter an already-closed snapshot.

  The REAL guarantees this file proves:
    1. The production function issues exactly ONE execute
       (``exec_count == 1``).
    2. total_decisions / batch_count / signal_distribution derive from that
       single statement's snapshot, so they can never disagree
       (total == sum(distribution) == 9; the injected "A" row is absent).
    3. The OLD two-query impl, under the SAME injection, sees the new commit
       between its two executes and mismatches (total 9 != distribution sum
       10) - the race is real, not theoretical hand-waving.

Uses the isolated PG fixture ``make_repo()`` (CRYPTO_GUARD_REDIS_DISABLED=1,
crypto_guard_test role/DB); never touches production.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import unittest

from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.storage import repository as repository_mod
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo, direct_conn


# Window shared by every case in this file.
_START = "2026-07-24T17:05:00Z"
_END = "2026-07-24T18:05:00Z"


def _scheduled_decision(symbol: str, *, grade: str, batch_id: str | None,
                        analysis_time_utc: str, analysis_time: int) -> dict:
    """A scheduled_analysis ga_decision row for hourly-aggregate tests."""
    return {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": analysis_time_utc,
        "decision_type": "scheduled_analysis",
        "signal_grade": grade,
        "confidence": 0.7,
        "market_bias": "neutral",
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [{"k": "v"}],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": batch_id,
        "previous_grade": grade,
    }


def _raw_insert_scheduled(conn, *, symbol: str, grade: str, batch_id: str,
                          analysis_time_utc: str, analysis_time: int) -> None:
    """Insert a scheduled_analysis row directly on an independent backend.

    Used by the between-execute injector so the new row commits on a SEPARATE
    backend (simulating a real concurrent writer), not on the pooled conn the
    repository is scanning with.

    Routes through ``CryptoGuardRepository(conn).create_ga_decision`` (rather
    than a hand-written INSERT) so the row uses the EXACT column mapping the
    rest of the suite relies on (the API dict keys ``skill_result_refs`` /
    ``evidence`` / ... map to the ``_json`` columns in the real schema; a bare
    INSERT against the wrong column names is a false failure).
    """
    row = _scheduled_decision(
        symbol, grade=grade, batch_id=batch_id,
        analysis_time_utc=analysis_time_utc, analysis_time=analysis_time,
    )
    repo_b = CryptoGuardRepository(conn)
    repo_b.create_ga_decision(row)
    # ``create_ga_decision`` wraps its INSERT in ``conn.transaction()`` which
    # commits on success; force the independent backend's commit explicitly so
    # the row is durable and visible to a fresh READ COMMITTED snapshot taken
    # by the pooled conn's NEXT execute.
    conn.commit()


class _CountingCursor:
    """Wrap a real cursor and call a hook exactly once, right after the
    ``fire_after_exec``-th execute completes (threshold 1 in this file).

    终审返工 R3 P2-2 (2026-07-26) - honest semantics: the hook fires after
    execute #1 on BOTH implementations, so ``state["hook_fired"]`` is True on
    the new single-statement path too. The discriminator is NOT whether the
    hook ran but WHEN relative to remaining statements:

    - OLD two-query impl: the hook instant falls BETWEEN execute #1 and
      execute #2; execute #2's fresh READ COMMITTED snapshot sees the
      injected commit -> total (stmt 1) != distribution sum (stmt 2).
    - NEW single-statement impl: there IS no later statement. The injected
      commit postdates the lone statement's snapshot, so it is invisible;
      the load-bearing assertion is ``exec_count == 1``, not hook silence.

    All cursor methods are delegated to the wrapped cursor so the repository
    sees a fully functional cursor. Execute counts and hook-fired state are
    written into the shared ``state`` dict so the caller can assert on them
    after the repository call returns (the wrapper object goes out of scope
    with the ``with conn.cursor() as cur`` block).
    """

    def __init__(self, real_cursor, *, fire_after_exec: int, hook, state: dict):
        self._real = real_cursor
        self._fire_after_exec = fire_after_exec
        self._hook = hook
        self._state = state

    def execute(self, *args, **kwargs):
        result = self._real.execute(*args, **kwargs)
        self._state["exec_count"] = int(self._state.get("exec_count", 0)) + 1
        if (self._state["exec_count"] == self._fire_after_exec
                and not self._state.get("hook_fired", False)):
            self._state["hook_fired"] = True
            self._hook()
        return result

    # Delegate the context-manager protocol so ``with conn.cursor() as cur:``
    # (the repository's usage) works through the wrapper.
    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_between_execute_hook(repo, schema: str, *, inject_row: dict):
    """Replace ``repo.conn.cursor`` so the cursor it returns counts executes
    and injects ``inject_row`` on a SECOND backend between execute #1 and #2.

    Returns a dict capturing how many executes ran and whether the hook fired.
    A fresh independent backend (``direct_conn``) is created lazily inside the
    hook so the injected row commits on a separate transaction - mirroring a
    real concurrent writer committing between the two statements.
    """
    state = {"exec_count": 0, "hook_fired": False, "schema": schema,
             "inject": inject_row}
    real_cursor_factory = repo.conn.cursor

    def make_counting_cursor(*a, **kw):
        real = real_cursor_factory(*a, **kw)

        def hook():
            state["hook_fired"] = True
            conn_b = direct_conn(schema)
            try:
                _raw_insert_scheduled(
                    conn_b,
                    symbol=inject_row["symbol"],
                    grade=inject_row["grade"],
                    batch_id=inject_row["batch_id"],
                    analysis_time_utc=inject_row["analysis_time_utc"],
                    analysis_time=inject_row["analysis_time"],
                )
            finally:
                conn_b.close()

        return _CountingCursor(real, fire_after_exec=1, hook=hook, state=state)

    repo.conn.cursor = make_counting_cursor
    return state


class TestSingleStatementConsistentHourlySnapshotP1(unittest.TestCase):
    """P1-2: the hourly aggregate must be a SINGLE statement so total and
    distribution derive from one snapshot and can never disagree."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo
        self.schema = self._repo_handle.schema

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_baseline(self) -> None:
        """Seed 3 complete batches (9 decisions) inside the window."""
        rows = []
        grades_per_batch = [("B", "B", "C"), ("C", "D", "D"), ("B", "C", "C")]
        batch_ts = ["2026-07-24T17:10:00Z", "2026-07-24T17:40:00Z", "2026-07-24T18:00:00Z"]
        for bi, grades in enumerate(grades_per_batch):
            ts = batch_ts[bi]
            at_ms = 1785000000000 + bi * 1000
            for si, g in enumerate(grades):
                rows.append(_scheduled_decision(
                    f"BASE{bi}{si}USDT", grade=g, batch_id=f"15m:b{bi}",
                    analysis_time_utc=ts, analysis_time=at_ms + si,
                ))
        for row in rows:
            self.repo.create_ga_decision(row)

    def test_zero_rows_returns_zero_empty_distribution(self) -> None:
        """零行返回 total=0, batch_count=0, distribution={}."""
        result = self.repo.hourly_scheduled_analysis_distribution(
            start_utc=_START, end_utc=_END,
        )
        self.assertEqual(result["total_decisions"], 0, result)
        self.assertEqual(result["batch_count"], 0, result)
        self.assertEqual(result["signal_distribution"], {}, result)

    def test_total_equals_distribution_sum_static(self) -> None:
        """Static case: total_decisions == sum(signal_distribution.values())."""
        self._seed_baseline()
        result = self.repo.hourly_scheduled_analysis_distribution(
            start_utc=_START, end_utc=_END,
        )
        total = result["total_decisions"]
        dist_sum = sum(result["signal_distribution"].values())
        self.assertEqual(total, 9, result)
        self.assertEqual(dist_sum, 9, result)
        self.assertEqual(total, dist_sum, result)
        self.assertEqual(result["batch_count"], 3, result)

    def test_single_statement_invariant_under_between_execute_write(self) -> None:
        """The single-statement impl is invariant under a concurrent commit
        landing right after its (only) execute.

        终审返工 R3 P2-2 (2026-07-26) - honest mechanics: the hook DOES fire
        here too (after execute #1 - the only execute), and the injected row
        DOES commit on its independent backend. But the lone statement's READ
        COMMITTED snapshot was taken when that statement started, BEFORE the
        hook ran, so the commit is invisible to it: total == sum(distribution)
        == 9 and grade "A" is absent. The load-bearing revert signal is
        ``exec_count == 1``: a revert to two executes would place the same
        hook instant BETWEEN statements, execute #2's fresh snapshot would see
        the injected row, and total (9) != distribution sum (10) - exactly
        what ``test_old_two_query_impl_reproduces_the_race`` demonstrates on
        the faithful old body.
        """
        self._seed_baseline()
        inject_row = {
            "symbol": "INJECTUSDT", "grade": "A",
            "batch_id": "15m:inj",
            "analysis_time_utc": "2026-07-24T17:55:00Z",
            "analysis_time": 1785000000999,
        }
        state = _install_between_execute_hook(
            self.repo, self.schema, inject_row=inject_row,
        )
        try:
            result = self.repo.hourly_scheduled_analysis_distribution(
                start_utc=_START, end_utc=_END,
            )
        finally:
            # ``direct_conn`` committed its own backend txn; the pooled conn's
            # READ COMMITTED snapshot for the (single) execute is unaffected by
            # restoring the cursor factory.
            pass
        # The NEW single-statement code runs exactly ONE execute. The hook DID
        # fire (after that execute - see _CountingCursor docstring), but with
        # no second statement there is no fresh snapshot for the injected
        # commit to enter. exec_count==1 is the revert signal: two executes
        # would expose a between-statement boundary and break the invariant.
        self.assertEqual(state["exec_count"], 1,
                         "single-statement impl must issue exactly ONE execute; "
                         f"observed {state['exec_count']}")
        self.assertTrue(state["hook_fired"],
                        "hook fires after execute #1 on the new path too "
                        "(R3 P2-2 honest semantics); if this is False the "
                        "injection never ran and the invariant below is vacuous")
        total = result["total_decisions"]
        dist_sum = sum(result["signal_distribution"].values())
        self.assertEqual(total, dist_sum, result)
        # The injected row was committed on a separate backend AFTER the single
        # execute's snapshot was taken (READ COMMITTED snapshots are
        # per-statement, taken at first statement read). Because the single
        # statement's snapshot predates the inject commit, the injected row is
        # NOT visible to it: total stays 9, not 10. (If it WERE visible, the
        # hook would have had to fire before the execute started, which the
        # boundary design prevents.)
        self.assertEqual(total, 9, result)
        self.assertNotIn("A", result["signal_distribution"], result)

    def test_old_two_query_impl_reproduces_the_race(self) -> None:
        """Revert-fail / race proof: a FAITHFUL reimplementation of the OLD
        two-query logic, run under the SAME between-execute injection, DOES
        produce total_decisions != sum(distribution). This proves the race is
        not theoretical and that the single-statement fix is load-bearing: if
        someone reverts to two executes, this assertion (mirroring the bug)
        holds True while ``test_single_statement_invariant...`` flips to FAIL.
        """
        self._seed_baseline()
        inject_row = {
            "symbol": "INJECTUSDT", "grade": "A",
            "batch_id": "15m:inj",
            "analysis_time_utc": "2026-07-24T17:55:00Z",
            "analysis_time": 1785000000999,
        }
        # A faithful copy of the OLD two-query body, instrumented with the
        # same between-execute injection.
        conn = self.conn
        real_cursor_factory = conn.cursor
        state = {"exec_count": 0, "hook_fired": False}

        def make_counting(*a, **kw):
            real = real_cursor_factory(*a, **kw)

            def hook():
                state["hook_fired"] = True
                conn_b = direct_conn(self.schema)
                try:
                    _raw_insert_scheduled(
                        conn_b, symbol=inject_row["symbol"],
                        grade=inject_row["grade"],
                        batch_id=inject_row["batch_id"],
                        analysis_time_utc=inject_row["analysis_time_utc"],
                        analysis_time=inject_row["analysis_time"],
                    )
                finally:
                    conn_b.close()

            return _CountingCursor(real, fire_after_exec=1, hook=hook, state=state)

        conn.cursor = make_counting
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total_decisions,
                           COUNT(DISTINCT batch_id) FILTER (WHERE batch_id IS NOT NULL) AS batch_count
                    FROM ga_decisions
                    WHERE decision_type = 'scheduled_analysis'
                      AND analysis_time_utc >= %s AND analysis_time_utc < %s
                    """,
                    [_START, _END],
                )
                agg = dict(cur.fetchone() or {})
                # The injected row commits HERE, on a separate backend, between
                # the two executes.
                cur.execute(
                    """
                    SELECT signal_grade, COUNT(*) AS count
                    FROM ga_decisions
                    WHERE decision_type = 'scheduled_analysis'
                      AND analysis_time_utc >= %s AND analysis_time_utc < %s
                    GROUP BY signal_grade
                    """,
                    [_START, _END],
                )
                dist = {str(r["signal_grade"] or "-"): int(r["count"])
                        for r in cur.fetchall()}
        finally:
            pass
        old_total = int(agg.get("total_decisions") or 0)
        old_dist_sum = sum(dist.values())
        # The OLD two-query code DID fire the between-execute hook (it ran 2
        # executes), and the injected row IS visible to execute #2 but NOT to
        # execute #1 -> the inconsistency the re-review names.
        self.assertTrue(state["hook_fired"],
                        "OLD two-query impl must run TWO executes (hook fires)")
        self.assertEqual(state["exec_count"], 2,
                         f"OLD two-query impl must run exactly TWO executes; "
                         f"observed {state['exec_count']}")
        self.assertEqual(old_total, 9, (agg, dist))
        self.assertEqual(old_dist_sum, 10, (agg, dist))
        self.assertNotEqual(old_total, old_dist_sum,
                            "OLD two-query impl must produce total != sum "
                            "under between-execute write; if this ever equals, "
                            "the race reproduction is no longer faithful")

    def test_render_path_uses_single_statement_total(self) -> None:
        """The render path (``_pg_hourly_scheduled_stats`` -> repo aggregate)
        inherits the single-statement consistency: rendered N (batch_count) and
        M (total_decisions) and the distribution sum are mutually consistent."""
        self._seed_baseline()
        stats = hourly_report._pg_hourly_scheduled_stats(
            self.repo, generated_at_utc="2026-07-24T18:05:00Z",
        )
        self.assertTrue(stats.get("ok"), stats)
        self.assertEqual(stats.get("source"), "postgres", stats)
        self.assertEqual(stats.get("total_decisions"), 9, stats)
        self.assertEqual(stats.get("batch_count"), 3, stats)
        self.assertEqual(sum((stats.get("signal_distribution") or {}).values()), 9,
                         stats)
