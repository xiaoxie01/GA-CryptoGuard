# -*- coding: utf-8 -*-
"""08-02 production fix P1-3 (2026-08-02): execution-funnel report contract —
RED-first behavioral test + revert-fail.

P1-3 PRD (verbatim): "LLM rows show separately: call succeeded, plan
confirmed, risk passed, final executable. 'LLM not confirmed' only from
immutable synthesis evidence, never inferred from a final trade_plan cleared
by later gates. New diagnostics: confirmed_without_executable_plan,
no_candidate_with_candidate_plan, executable_status_without_plan,
opportunity_watch_not_materialized, opportunity_watch_untriggerable_condition,
execution_funnel_starvation. Fix existing diagnostics that only check 'has
plan but not confirmed' while missing the inverse contradictions. Historical
rows split current/legacy via a NEW marker; marker only written by the
initialize_database publish path; tests use a fresh DB image; production is
NOT written this round."

Contracts exercised here (all real PostgreSQL via ``make_repo`` — the marker
is auto-written by ``initialize_database`` into each fresh scratch schema):

1. ROW SPLIT (hourly_report._decision_row): each row carries four independent
   immutable aspects — llm_call_succeeded (llm_status==ok), llm_plan_confirmed
   (llm_plan_verdict==confirmed), risk_passed (risk_check.ok), final_executable
   (confirmed + executable + has_trade_plan + non-empty trade_plan). A plan
   cleared by later gates must NOT be inferred back into llm_plan_confirmed.

2. MARKER: ``execution_funnel_report_contract_v1`` is registered by
   initialize_database (fail-closed: a missing marker surfaces
   execution_funnel_report_contract_marker_missing AND self-skips the six
   funnel checks — no silent green). Pre-marker findings demote to
   legacy_info (the watch scan is NOT SQL-bound, so the cutoff is its real
   demotion path); post-marker findings stay current errors.

3. PER-DECISION CHECKS (SQL-bound by created_at >= marker applied_at):
   - confirmed_without_executable_plan: state=confirmed but NOT final-executable.
   - no_candidate_with_candidate_plan: state=no_candidate but a plan persists.
   - executable_status_without_plan: plan_status=executable but NOT final-executable.
   - opportunity_watch_not_materialized: wire-in gate satisfied but no active watch.
   - opportunity_watch_untriggerable_condition: watch whose condition can never trigger.

4. AGGREGATE CHECK (live 24h, marker lower-bound; Codex P1-4 produced cohort):
   - execution_funnel_starvation: PRODUCED S/A decisions (confirmed verdict +
     plan evidence + risk ok + effective S/A) with zero final-executable S/A
     plans; stats lower bound = max(now-24h, marker.applied_at); pre-marker raw
     S/A and legitimate non-executables (LLM unconfirmed / risk rejected / HTF
     degraded) must NOT fire (see test_pg_execution_funnel_starvation_codex_p1_4).

REVERT-FAIL proof (documented in each POS test; executed as a one-time manual
revert during verification): the POS tests ARE the revert-fail triggers — each
seeds a genuine defect and asserts its code fires. Reverting the check
(function stub -> ``[]``, or deleting the wiring call in
``diagnose_report_accuracy``) makes the same defect UNDETECTED and the POS
assert FAILS (RED). This proves each check is the sole emitter of its code and
the wire-in call is real.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import pytest

# 5.4: all classes are data-only single-connection tests verified SAFE for the
# shared per-worker rollback schema (no DDL, no explicit commit, no second
# connection). The P1-3 contract marker is seeded once per worker by
# initialize_database; per-test marker DELETEs roll back at close() so the next
# test sees the seeded baseline. Schema init once per worker instead of per
# test (~52x).
pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.diagnostics import report_diagnostics
from plugins.crypto_guard.diagnostics.report_diagnostics import (
    CONFIRMED_WITHOUT_EXECUTABLE_PLAN,
    EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,
    EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING,
    EXECUTION_FUNNEL_STARVATION,
    EXECUTABLE_STATUS_WITHOUT_PLAN,
    NO_CANDIDATE_WITH_CANDIDATE_PLAN,
    OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH,
    OPPORTUNITY_WATCH_NOT_MATERIALIZED,
    OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION,
    _is_final_executable,
    diagnose_report_accuracy,
)
from plugins.crypto_guard.ga_master.decision_schema import controller_decision_from_legacy
from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.notify.hourly_report import _decision_row
from plugins.crypto_guard.tests.pg_fixtures import make_repo

_EXECUTION_FUNNEL_CODES = frozenset({
    CONFIRMED_WITHOUT_EXECUTABLE_PLAN,
    NO_CANDIDATE_WITH_CANDIDATE_PLAN,
    EXECUTABLE_STATUS_WITHOUT_PLAN,
    OPPORTUNITY_WATCH_NOT_MATERIALIZED,
    OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH,
    OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION,
    EXECUTION_FUNNEL_STARVATION,
})

# P0 producer regression: analysis time for a produced decision (ms).
_ANALYSIS_TIME_MS = 1785487499999

# ── structured watch shapes (P0-3 schema-valid) ─────────────────────────────

_STRUCTURED_WATCH = {
    "needed": True,
    "direction": "LONG",
    "reason": "test",
    "conditions": [
        {"type": "price_above", "side": "LONG", "level": 100.0, "timeframe": "15m"},
    ],
    "invalid_condition": {
        "type": "price_below", "side": "LONG", "level": 90.0, "timeframe": "15m",
    },
    "expires_minutes": 240,
}

# ── raw_decision_json shapes (one contradiction family per check) ───────────

# final executable: the NEG control for confirmed/executable checks.
_FINAL_EXECUTABLE_RAW = {
    "llm_status": "ok",
    "plan_execution_state": "confirmed",
    "plan_status": "executable",
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True},
    "raw_signal_grade": "S",
    "effective_signal_grade": "S",
    "suggested_actions": [],
    "opportunity_watch": None,
}

# confirmed but NOT final-executable (plan present but withheld by a later gate).
_CONFIRMED_WITHHELD_RAW = {
    "plan_execution_state": "confirmed",
    "plan_status": "withheld",
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True},
    "effective_signal_grade": "B",
}

# plan_status=executable but never confirmed + no plan persisted.
_EXECUTABLE_UNCONFIRMED_RAW = {
    "plan_execution_state": "unconfirmed",
    "plan_status": "executable",
    "has_trade_plan": False,
    "trade_plan": None,
    "llm_plan_verdict": "rejected",
    "risk_check": {"ok": False},
    "effective_signal_grade": "C",
}

# lifecycle no_candidate but a candidate plan persists.
_NO_CANDIDATE_WITH_PLAN_RAW = {
    "plan_execution_state": "no_candidate",
    "plan_status": "no_candidate",
    "has_trade_plan": False,
    "trade_plan": None,
    "candidate_trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "llm_plan_verdict": "none",
    "effective_signal_grade": "C",
}

_NO_CANDIDATE_CLEAN_RAW = {
    "plan_execution_state": "no_candidate",
    "plan_status": "no_candidate",
    "has_trade_plan": False,
    "trade_plan": None,
    "candidate_trade_plan": None,
    "llm_plan_verdict": "none",
    "effective_signal_grade": "C",
}

# wire-in gate satisfied (P0-2): structured watch + grade A + no open order.
_MATERIALIZE_RAW = {
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "has_trade_plan": False,
    "suggested_actions": ["create_opportunity_watch"],
    "opportunity_watch": _STRUCTURED_WATCH,
    "raw_signal_grade": "A",
    "effective_signal_grade": "A",
}

_MATERIALIZE_UNSTRUCTURED_RAW = {
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "has_trade_plan": False,
    "suggested_actions": ["create_opportunity_watch"],
    "opportunity_watch": {
        "needed": True, "direction": "LONG",
        "conditions": ["BTC 收盘突破上沿或跌破下沿"],
        "invalid_condition": None,
    },
    "raw_signal_grade": "A",
    "effective_signal_grade": "A",
}

_MATERIALIZE_GRADE_C_RAW = {
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "has_trade_plan": False,
    "suggested_actions": ["create_opportunity_watch"],
    "opportunity_watch": _STRUCTURED_WATCH,
    "raw_signal_grade": "C",
    "effective_signal_grade": "C",
}

# PRODUCED S/A that never becomes final-executable (starvation POS). Codex
# P1-4 (2026-08-03): the error cohort is the PRODUCED cohort (confirmed verdict
# + plan evidence + risk ok + effective S/A) minus final-executable, so these
# seeds carry a confirmed verdict, plan evidence and risk ok. The old bare
# raw-grade-only shapes (no verdict/risk/plan) are P1-4-FORBIDDEN — raw S/A
# without those gates must NEVER fire (see
# test_pg_execution_funnel_starvation_codex_p1_4 group 2).
_STARVATION_STRICT_S_RAW = {
    "raw_signal_grade": "S",
    "effective_signal_grade": "S",
    "llm_status": "ok",
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True, "reasons": []},
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "llm_synthesis_trade_plan": {"entry": 100.0, "stop_loss": 95.0, "side": "LONG"},
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "suggested_actions": [],
    "opportunity_watch": None,
}

_STARVATION_STRICT_A_RAW = {
    "raw_signal_grade": "A",
    "effective_signal_grade": "A",
    "llm_status": "ok",
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True, "reasons": []},
    "has_trade_plan": False,
    "trade_plan": None,
    "llm_synthesis_trade_plan": {"entry": 100.0, "stop_loss": 95.0, "side": "LONG"},
    "plan_execution_state": "withheld",
    "plan_status": "withheld",
    "suggested_actions": [],
    "opportunity_watch": None,
}

# ── DB helpers ──────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _insert_decision(
    conn,
    *,
    symbol: str,
    raw: dict,
    created_at: datetime,
    signal_grade: str = "B",
) -> int:
    now_ms = int(created_at.timestamp() * 1000)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ga_decisions"
                "(symbol, analysis_time, analysis_time_utc, decision_type, "
                " signal_grade, confidence, market_bias, trend_stage, decision, "
                " skill_result_refs_json, evidence_json, counter_evidence_json, "
                " risk_check_json, feishu_actions_json, final_summary, "
                " raw_decision_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    symbol, now_ms, _iso(created_at), "scheduled",
                    signal_grade, 0.6, "neutral", "middle", "hold",
                    json.dumps([]), json.dumps({}), json.dumps([]),
                    json.dumps({}), json.dumps([]), "summary",
                    json.dumps(raw), created_at,
                ),
            )
            return int(cur.fetchone()["id"])


def _insert_watch(
    conn,
    *,
    symbol: str,
    conditions,
    invalid_condition=None,
    status: str = "active",
    created_at: datetime | None = None,
    dedupe_key: str | None = None,
    ga_decision_id: int | None = None,
) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_watches"
                "(symbol, direction, watch_reason, watch_condition_json, "
                " invalid_condition_json, status, ga_decision_id, dedupe_key, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    symbol, "LONG", "test", json.dumps(conditions),
                    json.dumps(invalid_condition) if invalid_condition is not None else None,
                    status, ga_decision_id, dedupe_key,
                    created_at or datetime.now(timezone.utc),
                ),
            )
            return int(cur.fetchone()["id"])


def _insert_open_paper_order(conn, *, symbol: str, status: str = "open") -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO paper_orders (symbol, side, order_type, status) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (symbol, "BUY", "LIMIT", status),
            )
            return int(cur.fetchone()["id"])


def _execution_funnel_marker_applied_at(conn) -> datetime:
    """Read the P1-3 marker applied_at seeded by initialize_database into the
    scratch schema. Raises if absent — the marker MUST be present after init."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
            (EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "initialize_database must seed "
        f"{EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY!r} (P1-3)"
    )
    applied = row["applied_at"]
    if isinstance(applied, datetime):
        return applied
    return datetime.fromisoformat(str(applied).replace("Z", "+00:00"))


def _codes(result: dict) -> set[str]:
    return {i["type"] for i in result["issues"]}


def _execution_funnel_issues(result: dict) -> list[dict]:
    return [i for i in result["issues"] if i["type"] in _EXECUTION_FUNNEL_CODES]


class TestDecisionRowFourAspectSplit(unittest.TestCase):
    """P1-3 PRD row split: four immutable aspects, never inferred back from a
    cleared trade_plan. Pure unit test of hourly_report._decision_row.

    Codex P1-4 (2026-08-03): the split is marker-gated and FAILS CLOSED — a
    None cutoff demotes to legacy / blanked aspects, so every call passes an
    explicit post-marker cutoff to actually compute the aspects."""

    CUTOFF = "2026-08-02T12:00:00Z"

    def _row(self, raw: dict) -> dict:
        # _decision_row reads risk_passed from the risk_check_json COLUMN
        # (mirroring a persisted row), not from inside raw_decision_json.
        return {
            "raw_decision_json": raw,
            "trade_plan_json": None,
            "risk_check_json": raw.get("risk_check") or {},
            "created_at": "2026-08-02T13:00:00Z",
        }

    def _split(self, raw: dict) -> dict:
        return _decision_row(
            self._row(raw), execution_funnel_cutoff_utc=self.CUTOFF,
        )

    def test_final_executable_has_all_four(self) -> None:
        r = self._split(_FINAL_EXECUTABLE_RAW)
        self.assertTrue(r["llm_call_succeeded"])
        self.assertTrue(r["llm_plan_confirmed"])
        self.assertTrue(r["risk_passed"])
        self.assertTrue(r["final_executable"])

    def test_confirmed_withheld_is_not_executable(self) -> None:
        # state=confirmed + plan present, but plan_status=withheld (a later
        # gate cleared the executable status). llm_plan_confirmed stays True
        # (immutable verdict) while final_executable MUST be False.
        r = self._split(_CONFIRMED_WITHHELD_RAW)
        self.assertTrue(r["llm_plan_confirmed"])
        self.assertTrue(r["risk_passed"])
        self.assertFalse(r["final_executable"])

    def test_llm_failed_row_all_false(self) -> None:
        raw = {
            "llm_status": "failed",
            "llm_plan_verdict": "rejected",
            "risk_check": {"ok": False},
            "plan_execution_state": "unconfirmed",
            "plan_status": "withheld",
            "has_trade_plan": False,
            "trade_plan": None,
        }
        r = self._split(raw)
        self.assertFalse(r["llm_call_succeeded"])
        self.assertFalse(r["llm_plan_confirmed"])
        self.assertFalse(r["risk_passed"])
        self.assertFalse(r["final_executable"])

    def test_risk_rejected_is_not_executable(self) -> None:
        raw = {
            "llm_status": "ok",
            "llm_plan_verdict": "confirmed",
            "risk_check": {"ok": False},
            "plan_execution_state": "risk_rejected",
            "plan_status": "withheld",
            "has_trade_plan": False,
            "trade_plan": None,
        }
        r = self._split(raw)
        self.assertTrue(r["llm_call_succeeded"])
        self.assertTrue(r["llm_plan_confirmed"])
        self.assertFalse(r["risk_passed"])
        self.assertFalse(r["final_executable"])


class TestDecisionRowExecutionFunnelWindowGate(unittest.TestCase):
    """08-02 P2-3 (fresh reviewer): the four-aspect execution-funnel split is
    marker-gated in ``hourly_report._decision_row``.

    A row created BEFORE the report-contract marker deployed
    (``execution_funnel_cutoff_utc``) was produced by a codebase that never
    wrote top-level ``has_trade_plan`` / ``llm_plan_verdict`` /
    ``plan_execution_state``, so computing the four aspects on it is
    misleading — every genuinely-executable pre-marker row would render
    ``final_executable=False``. The pre-fix split applied to every row
    unconditionally.

    RED-first (revert-fail): force ``execution_funnel_scope`` to ``"current"``
    in ``_decision_row`` (pre-fix behavior) and
    ``test_legacy_row_before_cutoff_has_blank_aspects`` fails on the first
    assertion — the scope is "current" and every aspect is computed instead of
    ``None``.
    """

    CUTOFF = "2026-08-02T12:00:00Z"

    def _row(self, raw: dict, *, created_at: Any) -> dict:
        return {
            "id": 1,
            "symbol": "BTCUSDT",
            "signal_grade": "S",
            "confidence": 0.9,
            "market_bias": "bull",
            "trend_stage": "middle",
            "decision": "buy",
            "final_summary": "summary",
            "rendered_summary": None,
            "raw_decision_json": raw,
            "trade_plan_json": None,
            "risk_check_json": raw.get("risk_check") or {},
            "created_at": created_at,
        }

    def test_legacy_row_before_cutoff_has_blank_aspects(self) -> None:
        # _FINAL_EXECUTABLE_RAW is the NEG control that would compute all-True
        # aspects in the current window; a pre-marker row MUST NOT show them.
        r = _decision_row(
            self._row(_FINAL_EXECUTABLE_RAW, created_at="2026-08-02T11:59:00Z"),
            execution_funnel_cutoff_utc=self.CUTOFF,
        )
        self.assertEqual(r["execution_funnel_scope"], "legacy")
        for aspect in ("llm_call_succeeded", "llm_plan_confirmed",
                       "risk_passed", "final_executable"):
            self.assertIsNone(r[aspect], aspect)
        # Non-funnel fields stay intact (only the split is gated).
        self.assertEqual(r["symbol"], "BTCUSDT")
        self.assertEqual(r["final_summary"], "summary")

    def test_current_row_after_cutoff_computes_aspects(self) -> None:
        r = _decision_row(
            self._row(_FINAL_EXECUTABLE_RAW, created_at="2026-08-02T12:01:00Z"),
            execution_funnel_cutoff_utc=self.CUTOFF,
        )
        self.assertEqual(r["execution_funnel_scope"], "current")
        self.assertTrue(r["llm_call_succeeded"])
        self.assertTrue(r["llm_plan_confirmed"])
        self.assertTrue(r["risk_passed"])
        self.assertTrue(r["final_executable"])

    def test_row_at_cutoff_boundary_is_current(self) -> None:
        # _after_execution_funnel_cutoff uses >=, so a row created exactly at
        # the marker instant is inside the contract window.
        r = _decision_row(
            self._row(_FINAL_EXECUTABLE_RAW, created_at=self.CUTOFF),
            execution_funnel_cutoff_utc=self.CUTOFF,
        )
        self.assertEqual(r["execution_funnel_scope"], "current")
        self.assertTrue(r["final_executable"])

    def test_no_cutoff_fails_closed_to_legacy(self) -> None:
        # Codex P1-4: a None cutoff (marker not deployed in this DB) FAILS
        # CLOSED to legacy (aspects blanked) — the report never fail-opens to
        # current; the marker-missing diagnostic surfaces the absent state.
        r = _decision_row(self._row(_FINAL_EXECUTABLE_RAW, created_at="2026-01-01T00:00:00Z"))
        self.assertEqual(r["execution_funnel_scope"], "legacy")
        for aspect in ("llm_call_succeeded", "llm_plan_confirmed",
                       "risk_passed", "final_executable"):
            self.assertIsNone(r[aspect], aspect)

    def test_datetime_created_at_gated_like_string(self) -> None:
        # Persisted rows carry an aware datetime, not a string.
        created = datetime(2026, 8, 2, 11, 59, tzinfo=timezone.utc)
        r = _decision_row(
            self._row(_FINAL_EXECUTABLE_RAW, created_at=created),
            execution_funnel_cutoff_utc=self.CUTOFF,
        )
        self.assertEqual(r["execution_funnel_scope"], "legacy")
        for aspect in ("llm_call_succeeded", "llm_plan_confirmed",
                       "risk_passed", "final_executable"):
            self.assertIsNone(r[aspect], aspect)

    def test_missing_created_at_fails_closed_to_legacy(self) -> None:
        # Codex P1-4: a malformed/missing created_at FAILS CLOSED to legacy
        # (aspects blanked) — never fail-open to current.
        row = self._row(_FINAL_EXECUTABLE_RAW, created_at="2026-08-02T12:01:00Z")
        row["created_at"] = None
        r = _decision_row(row, execution_funnel_cutoff_utc=self.CUTOFF)
        self.assertEqual(r["execution_funnel_scope"], "legacy")
        for aspect in ("llm_call_succeeded", "llm_plan_confirmed",
                       "risk_passed", "final_executable"):
            self.assertIsNone(r[aspect], aspect)


class TestRenderForwardsExecutionFunnelCutoff(unittest.TestCase):
    """08-02 P2-3: ``render_ga_hourly_summary`` must forward the marker cutoff
    to EVERY ``_decision_row`` call.

    Without forwarding, the gated split silently degrades to the pre-P2-3
    all-current behavior — the unit gate in ``_decision_row`` passes but the
    report never receives the cutoff. Revert-fail: remove the
    ``execution_funnel_cutoff_utc`` kwarg from the ``render_ga_hourly_summary``
    call in ``_decision_row``'s list comprehension (pre-fix code) and this test
    fails on ``call.kwargs`` not carrying the cutoff.
    """

    CUTOFF = "2026-08-02T12:00:00Z"

    def _row(self, raw: dict, *, symbol: str, created_at: str) -> dict:
        return {
            "id": 1,
            "symbol": symbol,
            "signal_grade": "S",
            "confidence": 0.9,
            "decision": "buy",
            "market_bias": "bull",
            "trend_stage": "middle",
            "analysis_time": 1785487499999,
            "batch_id": "15m:test",
            "previous_grade": "S",
            "trade_plan_json": None,
            "risk_check_json": raw.get("risk_check") or {},
            "feishu_actions_json": "[]",
            "raw_decision_json": raw,
            "rendered_summary": "s",
            "final_summary": "summary",
            "created_at": created_at,
        }

    def test_cutoff_reaches_every_decision_row_call(self) -> None:
        rows = [
            # one post-marker and one pre-marker row; BOTH must get the cutoff.
            self._row(_FINAL_EXECUTABLE_RAW, symbol="BTCUSDT",
                      created_at="2026-08-02T13:00:00Z"),
            self._row(_CONFIRMED_WITHHELD_RAW, symbol="ETHUSDT",
                      created_at="2026-08-02T11:00:00Z"),
        ]
        with mock.patch.object(
            hourly_report, "_decision_row",
            wraps=hourly_report._decision_row,
        ) as spy:
            hourly_report.render_ga_hourly_summary(
                "2026-08-02T13:30:00Z",
                active_symbols=["BTCUSDT", "ETHUSDT"],
                ga_decisions=rows,
                open_orders=[], active_watches=[], failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                execution_funnel_cutoff_utc=self.CUTOFF,
            )
        self.assertEqual(spy.call_count, 2)
        for call in spy.call_args_list:
            self.assertEqual(
                call.kwargs.get("execution_funnel_cutoff_utc"), self.CUTOFF,
                "every _decision_row call must receive the marker cutoff",
            )


class TestProducerWritesTopLevelHasTradePlan(unittest.TestCase):
    """P0 (fresh reviewer): the ONLY ga_decision producer
    (``controller_decision_from_legacy``) must emit top-level ``has_trade_plan``.

    ``ga_decision.schema.json:5`` REQUIRES it; ``repository.create_ga_decision``
    docstring declares it top-level in ``raw_decision_json``; three consumers
    read it: ``hourly_report._decision_row.final_executable``,
    ``report_diagnostics._is_final_executable``, and the
    ``execution_funnel_starvation`` SQL (``raw_decision_json->>'has_trade_plan'
    = 'true'``). The pre-fix producer silently dropped it, so every
    production-persisted row rendered ``final_executable=False`` while the
    P1-3 fixtures (hand-written ``has_trade_plan: True``) kept the tests green.

    Regression goes through the REAL production shape:
    ``controller_decision_from_legacy -> create_ga_decision -> row -> the three
    consumers``. RED-first: before the fix this class fails on the first
    assertion (``ga_decision`` has no top-level ``has_trade_plan``), so the
    ``_decision_row`` / ``_is_final_executable`` / diagnostic assertions below
    can never pass by hand-written fixture luck.
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _produce_and_persist(self) -> dict:
        legacy = {
            "symbol": "BTCUSDT",
            "decision": "trade_plan_available",
            "signal_grade": "S",
            "raw_signal_grade": "S",
            "effective_signal_grade": "S",
            "confidence": 0.9,
            "summary": "P0 producer regression",
            "market_bias": "bullish",
            "trend_stage": "early",
            "has_trade_plan": True,
            "trade_plan": {"entry": 100.0, "stop_loss": 95.0, "side": "LONG"},
            "risk_check": {"ok": True, "reasons": []},
            "evidence": [],
            "counter_evidence": [],
            "risk_notes": [],
            "llm_status": "ok",
            "llm_plan_verdict": "confirmed",
            "plan_execution_state": "confirmed",
            "plan_status": "executable",
        }
        ga_decision = controller_decision_from_legacy(
            legacy=legacy,
            decision_type="scheduled",
            analysis_time=_ANALYSIS_TIME_MS,
            skill_result_refs={},
            feishu_actions=[],
            snapshot_id=None,
            analysis_state_id=None,
        )
        ga_id = self.repo.create_ga_decision(ga_decision)
        return ga_decision, self.repo.get_ga_decision(ga_id)

    def _raw_from_row(self, row: dict) -> dict:
        raw = row["raw_decision_json"]
        if isinstance(raw, str):
            return json.loads(raw)
        return raw

    def test_producer_emits_top_level_has_trade_plan(self) -> None:
        ga_decision, _row = self._produce_and_persist()
        # Direct producer contract (RED trigger: absent before the fix).
        self.assertIs(ga_decision["has_trade_plan"], True)
        # Consumers, all reading the PERSISTED shape. Codex P1-4: the split is
        # marker-gated and FAILS CLOSED, so the cutoff is the marker the fresh
        # DB seeded — the produced row is post-marker -> current -> computed.
        cutoff = _execution_funnel_marker_applied_at(self.conn).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.assertIs(
            _decision_row(_row, execution_funnel_cutoff_utc=cutoff)["final_executable"],
            True,
        )
        self.assertIs(_is_final_executable(self._raw_from_row(_row)), True)

    def test_producer_row_never_fires_execution_funnel_diagnostics(self) -> None:
        _, _row = self._produce_and_persist()
        result = diagnose_report_accuracy(self.repo)
        codes = _codes(result)
        self.assertNotIn(CONFIRMED_WITHOUT_EXECUTABLE_PLAN, codes)
        # Starvation SQL reads raw_decision_json->>'has_trade_plan' = 'true';
        # the produced S-grade row IS final-executable, so no starvation.
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, codes)


class TestExecutionFunnelReportContractMarker(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_marker_registered_by_initialize_database(self) -> None:
        applied = _execution_funnel_marker_applied_at(self.conn)
        self.assertIsInstance(applied, datetime)

    def test_marker_missing_is_fail_closed(self) -> None:
        # Seed a genuine defect row; delete the marker. The marker-missing
        # check MUST fire (fail-closed) and the six funnel checks MUST
        # self-skip (the defect must NOT be reported while the contract is
        # undeployed — it would be historical audit, not a current error).
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_CONFIRMED_WITHHELD_RAW, created_at=datetime.now(timezone.utc),
        )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _migration_state WHERE key=%s",
                    (EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,),
                )
        result = diagnose_report_accuracy(self.repo)
        codes = _codes(result)
        self.assertIn(EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING, codes)
        self.assertNotIn(CONFIRMED_WITHOUT_EXECUTABLE_PLAN, codes)

    def test_marker_cutoff_demotes_pre_marker_watch(self) -> None:
        marker_applied = _execution_funnel_marker_applied_at(self.conn)
        pre_watch_id = _insert_watch(
            self.conn, symbol="PREUSDT",
            conditions=["BTC 收盘突破上沿"],
            created_at=marker_applied - timedelta(hours=1),
        )
        post_watch_id = _insert_watch(
            self.conn, symbol="POSTUSDT",
            conditions=["BTC 收盘突破上沿"],
            created_at=marker_applied + timedelta(hours=1),
        )
        result = diagnose_report_accuracy(self.repo)
        fired = [i for i in result["issues"]
                 if i["type"] == OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION]
        self.assertEqual(len(fired), 2)
        by_id = {int(i["details"]["watch_id"]): i for i in fired}
        self.assertEqual(by_id[pre_watch_id]["severity"], "legacy_info")
        self.assertEqual(by_id[post_watch_id]["severity"], "error")


class TestConfirmedWithoutExecutablePlan(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_confirmed_without_plan(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_CONFIRMED_WITHHELD_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(CONFIRMED_WITHOUT_EXECUTABLE_PLAN, _codes(result))

    def test_no_fire_on_final_executable(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_FINAL_EXECUTABLE_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(CONFIRMED_WITHOUT_EXECUTABLE_PLAN, _codes(result))


class TestNoCandidateWithCandidatePlan(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_no_candidate_with_plan(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_NO_CANDIDATE_WITH_PLAN_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(NO_CANDIDATE_WITH_CANDIDATE_PLAN, _codes(result))

    def test_no_fire_on_clean_no_candidate(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_NO_CANDIDATE_CLEAN_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(NO_CANDIDATE_WITH_CANDIDATE_PLAN, _codes(result))


class TestExecutableStatusWithoutPlan(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_executable_status_without_plan(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_EXECUTABLE_UNCONFIRMED_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(EXECUTABLE_STATUS_WITHOUT_PLAN, _codes(result))

    def test_no_fire_on_final_executable(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_FINAL_EXECUTABLE_RAW, created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTABLE_STATUS_WITHOUT_PLAN, _codes(result))

    def test_confirmed_and_executable_status_reports_once(self) -> None:
        # Single-emit precedence (08-02 review P2-D): a row that is BOTH
        # confirmed AND executable-status, but whose trade_plan is empty, is a
        # "confirmed without executable plan" contradiction — that is the more
        # descriptive finding. It must NOT also be reported as
        # executable_status_without_plan (double-reporting the same row inflates
        # the error count and splits the reader's attention).
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "plan_execution_state": "confirmed",
                "plan_status": "executable",
                "has_trade_plan": True,
                "trade_plan": {},
                "llm_plan_verdict": "confirmed",
                "risk_check": {"ok": True},
                "effective_signal_grade": "A",
            },
            created_at=datetime.now(timezone.utc),
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(CONFIRMED_WITHOUT_EXECUTABLE_PLAN, _codes(result))
        self.assertNotIn(EXECUTABLE_STATUS_WITHOUT_PLAN, _codes(result))


class TestOpportunityWatchNotMaterialized(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_when_no_active_watch(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_fires_on_controller_feishu_actions_envelope(self) -> None:
        # 08-02 Finding 5 (P2): controller-produced rows persist the suggested
        # action list at TOP level as ``feishu_actions`` (decision_schema §8
        # envelope line 144), NOT ``suggested_actions``. The P1-3 check
        # previously read only ``suggested_actions``, so it NEVER fired on a
        # real controller row (the tests wrote the legacy shape and masked the
        # gap). RED: revert _persisted_actions to
        # ``raw.get("suggested_actions")`` and this controller-shaped row is
        # UNDETECTED (assertion fails).
        raw = dict(_MATERIALIZE_RAW)
        raw["feishu_actions"] = list(raw.pop("suggested_actions"))
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=raw, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_no_fire_when_active_watch_exists(self) -> None:
        # Producer path (repository.upsert_auto_opportunity_watch) writes
        # dedupe_key="auto:BTCUSDT:LONG" — lowercase ``auto:`` prefix, ga_decision_id
        # NULL. The materialization check MUST match that exact key (08-02
        # review P1-A): a watch materialized via the auto path is real. The old
        # ``.upper()`` lookup missed the stored lowercase key and false-positived
        # an already-materialized watch (RED).
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        _insert_watch(
            self.conn, symbol="BTCUSDT", conditions=[_STRUCTURED_WATCH["conditions"][0]],
            dedupe_key="auto:BTCUSDT:LONG",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_no_fire_when_watch_refreshed_to_newer_decision(self) -> None:
        # Two gate-satisfied decisions for the same symbol+direction share one
        # dedupe_key. When the producer's ON CONFLICT DO UPDATE refreshes the
        # single active watch to the SECOND decision's ga_decision_id, the
        # FIRST decision must still be satisfied by the dedupe_key match (not
        # by ga_decision_id). The P1-A ``.upper()`` bug broke this: the first
        # decision's lookup missed the stored lowercase key AND its
        # ga_decision_id no longer pointed at the refreshed watch, so it
        # false-positived NOT_MATERIALIZED (RED).
        d1 = _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        d2 = _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_RAW,
            created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            signal_grade="A",
        )
        _insert_watch(
            self.conn, symbol="BTCUSDT", conditions=[_STRUCTURED_WATCH["conditions"][0]],
            dedupe_key="auto:BTCUSDT:LONG", ga_decision_id=d1,
        )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE opportunity_watches SET ga_decision_id=%s "
                    "WHERE dedupe_key=%s",
                    (d2, "auto:BTCUSDT:LONG"),
                )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_no_fire_when_open_paper_order(self) -> None:
        # The wire-in gate skips symbols with an open paper order — the
        # diagnostic must mirror that skip, not flag a by-design decision.
        _insert_decision(
            self.conn, symbol="ETHUSDT",
            raw=_MATERIALIZE_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        _insert_open_paper_order(self.conn, symbol="ETHUSDT", status="open")
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_no_fire_on_unstructured_watch(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_UNSTRUCTURED_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_no_fire_on_grade_c(self) -> None:
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=_MATERIALIZE_GRADE_C_RAW, created_at=datetime.now(timezone.utc),
            signal_grade="C",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))


class TestOpportunityWatchAdvertisedWithoutWatch(unittest.TestCase):
    """08-02 Finding 5 (P2) companion: a decision advertising
    create_opportunity_watch with NO structured watch (None / missing /
    unstructured dict) is a broken promise at the decision layer — the P0-2
    wire-in (fail-closed on is_structured_watch) can never honor the action.

    REVERT-FAIL: each POS test IS the revert-fail trigger — stubbing
    _check_opportunity_watch_advertised_without_watch to ``[]`` (or deleting
    the wiring call in diagnose_report_accuracy) makes the same persisted
    broken-promise row UNDETECTED and the POS assertion FAILS (RED). Proves
    the check is the sole emitter of its code and the wire-in call is real.

    Relationship to the materialization check: NOT_MATERIALIZED owns rows with
    a STRUCTURED watch (wire-in gate satisfied but no active watch); this
    companion owns rows with an UNSTRUCTURED/None watch (the materialization
    check's skipped-by-design path). They never double-report the same row.
    """

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_on_none_watch(self) -> None:
        # Legacy test shape (suggested_actions top level) + None watch.
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "suggested_actions": ["create_opportunity_watch"],
                "opportunity_watch": None,
            },
            created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result))

    def test_fires_on_feishu_envelope_with_unstructured_watch(self) -> None:
        # Controller §8 envelope: feishu_actions at top level + unstructured
        # bidirectional watch (the Finding 2 shape the controller must never
        # persist with create_opportunity_watch after the Finding-2 filter).
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "feishu_actions": [
                    "create_opportunity_watch", "add_to_watchlist", "ignore",
                ],
                "opportunity_watch": {
                    "needed": True, "direction": "bidirectional",
                },
            },
            created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))

    def test_fires_on_feishu_envelope_with_missing_watch_key(self) -> None:
        # Nested legacy suggested_actions fallback must not save a row whose
        # TOP-LEVEL opportunity_watch key is entirely absent.
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "feishu_actions": ["create_opportunity_watch"],
                "raw_legacy_decision": {
                    "suggested_actions": ["create_opportunity_watch"],
                },
            },
            created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))

    def test_no_fire_on_structured_watch(self) -> None:
        # The materialization check owns the structured-watch case; this
        # companion must NOT double-report it (single-emit per row).
        raw = dict(_MATERIALIZE_RAW)
        raw["feishu_actions"] = list(raw.pop("suggested_actions"))
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw=raw, created_at=datetime.now(timezone.utc),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))

    def test_no_fire_without_create_opportunity_watch_action(self) -> None:
        # None watch alone is not a broken promise — only an ADVERTISED
        # create_opportunity_watch without a structured watch is.
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "feishu_actions": ["ignore"],
                "opportunity_watch": None,
            },
            created_at=datetime.now(timezone.utc),
            signal_grade="C",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))

    def test_no_fire_on_pre_marker_row(self) -> None:
        # Mirrors the sibling per-decision checks: the SQL lower bound excludes
        # pre-contract rows (historical audit must not be evaluated as current).
        _insert_decision(
            self.conn, symbol="BTCUSDT",
            raw={
                "feishu_actions": ["create_opportunity_watch"],
                "opportunity_watch": None,
            },
            created_at=(
                _execution_funnel_marker_applied_at(self.conn) - timedelta(days=1)
            ),
            signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_ADVERTISED_WITHOUT_WATCH, _codes(result))


class TestOpportunityWatchUntriggerableCondition(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_on_bare_string_condition(self) -> None:
        _insert_watch(self.conn, symbol="BTCUSDT", conditions=["BTC 收盘突破上沿"])
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_fires_on_unknown_kind_condition(self) -> None:
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[{"type": "magic_signal", "side": "LONG", "level": 100.0}],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_no_fire_on_structured_conditions(self) -> None:
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[_STRUCTURED_WATCH["conditions"][0]],
            invalid_condition=_STRUCTURED_WATCH["invalid_condition"],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_no_fire_on_root_dict_account_feedback_recheck(self) -> None:
        # The manual account-feedback gate (paper_broker._create_opportunity_
        # watch_from_gate, :1214) persists watch_condition_json as a ROOT DICT
        # with type exactly "account_feedback_recheck". The watcher
        # (evaluate_watch, opportunity_watcher.py:82) routes that exact
        # root-dict form to _check_account_feedback_recheck, which CAN trigger
        # — so it is NOT an untriggerable watch (08-02 review P1-B + R2 Finding
        # 1). A naive kind-whitelist or a per-item special case flagged these
        # by-design watches (RED).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions={"type": "account_feedback_recheck", "side": "LONG"},
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_fires_on_list_item_account_feedback_recheck(self) -> None:
        # 08-02 R2 review Finding 1 (brand-new reviewer): the watcher routes
        # account_feedback_recheck ONLY as the root-dict form
        # (opportunity_watcher.py:82). A ROOT-LIST containing an
        # account_feedback_recheck item falls through to _condition_hit, where
        # the kind is not SUPPORTED and IS untriggerable. The diagnostic mirror
        # must agree — the per-item special case must NOT clear a list-item
        # variant (pre-fix RED: it wrongly returned False for any
        # account_feedback_recheck item regardless of root shape).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[{"type": "account_feedback_recheck", "side": "LONG"}],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_fires_on_kind_only_account_feedback_recheck(self) -> None:
        # 08-02 R2 review Finding 1 (brand-new reviewer): the watcher reads
        # ``type`` (not ``kind``) on the root dict for the account_feedback_
        # recheck route. A kind-only variant (no ``type``) is not routed, falls
        # to _condition_hit, and is untriggerable. The diagnostic mirror must
        # agree — the special case must not trigger on ``kind`` (pre-fix RED:
        # ``cond.get("type") or cond.get("kind")`` cleared a kind-only variant
        # the watcher flags).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[{"kind": "account_feedback_recheck", "side": "LONG"}],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_no_fire_on_uppercase_supported_kind(self) -> None:
        # 08-02 R2 P2-2 (fresh reviewer): the watcher lowercases the kind before
        # the SUPPORTED-set comparison, so a supported kind spelled
        # "Price_Above" with a numeric level triggers normally. The diagnostic
        # mirror must do the same — a case-variant of a SUPPORTED kind is NOT
        # untriggerable (pre-fix RED: it was flagged as an unknown kind).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[{"type": "Price_Above", "side": "LONG", "level": 100.0}],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_fires_on_uppercase_account_feedback_recheck(self) -> None:
        # 08-02 R2 P2-2 (fresh reviewer): the watcher routes ONLY the exact
        # lowercase root-dict "account_feedback_recheck" to
        # _check_account_feedback_recheck (CAN trigger); an uppercase variant
        # falls through to _condition_hit and is untriggerable. The diagnostic
        # mirror matches: "ACCOUNT_FEEDBACK_RECHECK" IS flagged (pre-fix RED:
        # the raw-case equality was broadened to lowercase and wrongly cleared
        # it, diverging from the watcher's exact-match routing).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[{"type": "ACCOUNT_FEEDBACK_RECHECK", "side": "LONG"}],
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))

    def test_mixed_watch_reports_sub_condition_data_quality(self) -> None:
        # 08-02 R2 P2-3 (fresh reviewer): the watcher's whole-watch dead marker
        # is ``all(...)`` — a watch with one live structured condition plus one
        # dead bare-string condition is NOT dead. The diagnostic must frame the
        # dead sub-condition as a data-quality issue, not overclaim the whole
        # watch "永远无法触发" (pre-fix RED: unconditional full-death message).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=[
                {"type": "price_above", "side": "LONG", "level": 100.0},
                "BTC 收盘突破上沿",
            ],
        )
        result = diagnose_report_accuracy(self.repo)
        issues = _execution_funnel_issues(result)
        hits = [i for i in issues if i["type"] == OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION]
        self.assertEqual(len(hits), 1, result)
        # The mixed message frames the dead sub-condition as a data-quality
        # issue and states the rest can still trigger — it must NOT claim the
        # whole watch is dead (漏斗静默失效 is the all-dead phrasing).
        self.assertIn("数据质量问题", hits[0]["suggested_action"])
        self.assertIn("其余结构化条件仍可继续触发", hits[0]["suggested_action"])
        self.assertNotIn("漏斗静默失效", hits[0]["suggested_action"])

    def test_no_fire_on_terminal_status_watch(self) -> None:
        # A terminal (expired) watch is no longer watched, so its conditions
        # cannot be "untriggerable in the active watch set" — the diagnostic
        # must scope the active set like the watcher does (08-02 review P2-A).
        # The pre-fix version scanned ALL statuses and false-positived expired
        # watches with a bare-string condition (RED).
        _insert_watch(
            self.conn, symbol="BTCUSDT",
            conditions=["BTC 收盘突破上沿"], status="expired",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_UNTRIGGERABLE_CONDITION, _codes(result))


class TestExecutionFunnelStarvation(unittest.TestCase):
    """Starvation POS with the Codex P1-4 produced-cohort seeds: the two
    ``_STARVATION_STRICT_*_RAW`` rows are PRODUCED (confirmed verdict + plan
    evidence + risk ok + effective S/A) but never final-executable, so they
    must fire exactly one error; adding a real executable S/A row makes the
    funnel reachable and must NOT fire (see the dedicated P1-4 groups in
    test_pg_execution_funnel_starvation_codex_p1_4 for pre-marker and
    legitimate-non-executable no-fire cases)."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_fires_on_strict_without_executable(self) -> None:
        now = datetime.now(timezone.utc)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_STARVATION_STRICT_S_RAW,
            created_at=now, signal_grade="S",
        )
        _insert_decision(
            self.conn, symbol="ETHUSDT", raw=_STARVATION_STRICT_A_RAW,
            created_at=now, signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        starvation = [i for i in result["issues"]
                      if i["type"] == EXECUTION_FUNNEL_STARVATION]
        # At most ONE error, strict > 0 and executable == 0.
        self.assertEqual(len(starvation), 1)
        self.assertEqual(starvation[0]["severity"], "error")
        self.assertGreater(starvation[0]["details"]["strict_signal_count_24h"], 0)
        self.assertEqual(starvation[0]["details"]["final_executable_count_24h"], 0)

    def test_no_fire_when_executable_exists(self) -> None:
        now = datetime.now(timezone.utc)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_STARVATION_STRICT_S_RAW,
            created_at=now, signal_grade="S",
        )
        _insert_decision(
            self.conn, symbol="ETHUSDT", raw=_FINAL_EXECUTABLE_RAW,
            created_at=now, signal_grade="S",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))
