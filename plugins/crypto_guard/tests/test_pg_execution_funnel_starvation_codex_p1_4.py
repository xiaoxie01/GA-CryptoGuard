# -*- coding: utf-8 -*-
"""Codex terminal-review P1-4 (2026-08-03): execution-funnel starvation is
marker-bound + produced-cohort gated; the four-aspect split FAILS CLOSED.

Finding (verbatim essence):
    all stats SQL lower bound = max(now-24h, marker.applied_at); pre-marker raw
    S/A must not trigger starvation; error cohort = post-marker AND
    (llm_plan_verdict=confirmed, llm_synthesis_trade_plan non-empty,
    risk_check.ok=true, effective grade S/A) BUT final executable false; raw
    S/A with LLM unconfirmed/risk rejected/HTF-grade degradation must NOT
    error; marker missing/corrupt -> hourly four-aspect must not fail-open to
    current (unknown/legacy + marker-missing diagnostic fail-closed).

RED-first + revert-fail: each test drives the REAL diagnostic
(``diagnose_report_accuracy``) or the REAL ``_decision_row`` with the defect
shape and asserts the fail-closed outcome. Reverting the fix flips the
assertions back to RED (proven per group):

  1. PRE-MARKER: a raw S/A contradiction row created BEFORE the report-contract
     marker (but inside the 24h window) must NOT fire starvation. The pre-fix
     code bounds only by now-24h, so the pre-marker row FIRES (RED).
  2. LEGITIMATE NON-EXECUTABLE: raw S/A rows with LLM unconfirmed / risk
     rejected / HTF-grade degradation (effective grade < S/A) must NOT fire.
     The pre-fix code counts raw ``raw_signal_grade`` S/A regardless of
     verdict/risk/effective grade, so they FIRE (RED).
  3. REAL POST-MARKER CONTRADICTION: produced S/A rows (confirmed verdict +
     plan evidence + risk ok + effective S/A) that never become executable
     MUST fire exactly one error.
  4. FOUR-ASPECT FAIL-CLOSED: ``_after_execution_funnel_cutoff`` with a None
     cutoff, a garbage cutoff, or a missing/garbage ``created_at`` -> legacy
     (aspects all None). The pre-fix gate fails open to current (RED).
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.diagnostics.report_diagnostics import (
    EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,
    EXECUTION_FUNNEL_STARVATION,
    diagnose_report_accuracy,
)
from plugins.crypto_guard.notify.hourly_report import _decision_row
from plugins.crypto_guard.tests.pg_fixtures import make_repo

# ── raw_decision_json shapes ────────────────────────────────────────────────

# Final-executable NEG control: produced AND executable (never a contradiction).
_FINAL_EXECUTABLE_RAW = {
    "llm_status": "ok",
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True, "reasons": []},
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "plan_execution_state": "confirmed",
    "plan_status": "executable",
    "raw_signal_grade": "S",
    "effective_signal_grade": "S",
    "suggested_actions": [],
    "opportunity_watch": None,
}

# REAL post-marker contradiction (P1-4): produced (effective S/A + confirmed
# verdict + plan evidence + risk ok) but NEVER final-executable.
_REAL_CONTRADICTION_S_RAW = {
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

_REAL_CONTRADICTION_A_RAW = {
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

# LEGITIMATE non-executables (P1-4: must NOT fire). Each carries the shape the
# pre-fix code wrongly counted as strict S/A.
_LLM_UNCONFIRMED_RAW = {
    "raw_signal_grade": "S",
    "effective_signal_grade": "S",
    "llm_status": "ok",
    "llm_plan_verdict": "rejected",
    "risk_check": {"ok": True, "reasons": []},
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "suggested_actions": [],
    "opportunity_watch": None,
}

_RISK_REJECTED_RAW = {
    "raw_signal_grade": "S",
    "effective_signal_grade": "S",
    "llm_status": "ok",
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": False, "reasons": ["stop_loss_too_far"]},
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "plan_execution_state": "risk_rejected",
    "plan_status": "withheld",
    "suggested_actions": [],
    "opportunity_watch": None,
}

_HTF_DEGRADED_RAW = {
    "raw_signal_grade": "S",
    "effective_signal_grade": "B",
    "llm_status": "ok",
    "llm_plan_verdict": "confirmed",
    "risk_check": {"ok": True, "reasons": []},
    "has_trade_plan": True,
    "trade_plan": {"entry": 100.0, "stop_loss": 95.0},
    "plan_execution_state": "unconfirmed",
    "plan_status": "withheld",
    "suggested_actions": [],
    "opportunity_watch": None,
}

# ── DB helpers ──────────────────────────────────────────────────────────────


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
                    symbol, now_ms, created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "scheduled", signal_grade, 0.6, "neutral", "middle", "hold",
                    json.dumps([]), json.dumps({}), json.dumps([]),
                    json.dumps({}), json.dumps([]), "summary",
                    json.dumps(raw), created_at,
                ),
            )
            return int(cur.fetchone()["id"])


def _execution_funnel_marker_applied_at(conn) -> datetime:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key=%s LIMIT 1",
            (EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "initialize_database must seed "
        f"{EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY!r}"
    )
    applied = row["applied_at"]
    if isinstance(applied, datetime):
        return applied
    return datetime.fromisoformat(str(applied).replace("Z", "+00:00"))


def _codes(result: dict) -> set[str]:
    return {i["type"] for i in result["issues"]}


def _starvation_issues(result: dict) -> list[dict]:
    return [i for i in result["issues"] if i["type"] == EXECUTION_FUNNEL_STARVATION]


# ── 1. PRE-MARKER raw S/A never triggers ────────────────────────────────────


class TestStarvationPreMarkerNoFireCodexP1_4(unittest.TestCase):
    """The stats SQL lower bound is max(now-24h, marker.applied_at): a raw S/A
    contradiction row created BEFORE the marker (but inside 24h) must be
    excluded. RED: pre-fix code bounds only by now-24h, so the pre-marker row
    fires starvation."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_pre_marker_raw_sa_contradiction_no_fire(self) -> None:
        marker = _execution_funnel_marker_applied_at(self.conn)
        # Move the marker back so a within-24h row can sit BEFORE it.
        marker_moved = marker - timedelta(hours=6)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE _migration_state SET applied_at=%s WHERE key=%s",
                    (marker_moved, EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY),
                )
        # Pre-marker (before the moved marker), still inside now-24h.
        created = marker_moved - timedelta(hours=6)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_REAL_CONTRADICTION_S_RAW,
            created_at=created, signal_grade="S",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))

    def test_pre_marker_final_executable_shape_no_fire(self) -> None:
        marker = _execution_funnel_marker_applied_at(self.conn)
        marker_moved = marker - timedelta(hours=6)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE _migration_state SET applied_at=%s WHERE key=%s",
                    (marker_moved, EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_KEY),
                )
        created = marker_moved - timedelta(hours=6)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_FINAL_EXECUTABLE_RAW,
            created_at=created, signal_grade="S",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))


# ── 2. LEGITIMATE non-executables never trigger ─────────────────────────────


class TestStarvationLegitimateNoFireCodexP1_4(unittest.TestCase):
    """Raw S/A with LLM unconfirmed / risk rejected / HTF-grade degradation are
    legitimate non-executables and must NOT error. RED: pre-fix code counts raw
    ``raw_signal_grade`` S/A with no verdict/risk/effective-grade gate, so each
    of these rows fires starvation."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _insert_now(self, raw: dict, *, signal_grade: str, symbol: str) -> None:
        _insert_decision(
            self.conn, symbol=symbol, raw=raw,
            created_at=datetime.now(timezone.utc), signal_grade=signal_grade,
        )

    def test_llm_unconfirmed_raw_sa_no_fire(self) -> None:
        # Confirmed-verdict is REQUIRED for produced: a rejected verdict with a
        # plan-shaped row is a legitimate non-executable, not starvation.
        self._insert_now(_LLM_UNCONFIRMED_RAW, signal_grade="S", symbol="BTCUSDT")
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))

    def test_risk_rejected_raw_sa_no_fire(self) -> None:
        # risk_check.ok=true is REQUIRED for produced: a risk-rejected S/A is a
        # legitimate non-executable (the funnel did its job), not starvation.
        self._insert_now(_RISK_REJECTED_RAW, signal_grade="S", symbol="BTCUSDT")
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))

    def test_htf_grade_degraded_raw_sa_no_fire(self) -> None:
        # EFFECTIVE grade S/A is required: raw S degraded by the HTF cap to B is
        # a legitimate non-executable, not starvation.
        self._insert_now(_HTF_DEGRADED_RAW, signal_grade="S", symbol="BTCUSDT")
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))


# ── 3. REAL post-marker contradiction fires ─────────────────────────────────


class TestStarvationRealContradictionFiresCodexP1_4(unittest.TestCase):
    """Produced S/A rows (confirmed verdict + plan evidence + risk ok +
    effective S/A) with zero final-executable plans in the window MUST fire
    exactly one error."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_real_post_marker_contradiction_fires(self) -> None:
        now = datetime.now(timezone.utc)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_REAL_CONTRADICTION_S_RAW,
            created_at=now, signal_grade="S",
        )
        _insert_decision(
            self.conn, symbol="ETHUSDT", raw=_REAL_CONTRADICTION_A_RAW,
            created_at=now, signal_grade="A",
        )
        result = diagnose_report_accuracy(self.repo)
        starvation = _starvation_issues(result)
        self.assertEqual(len(starvation), 1)
        self.assertEqual(starvation[0]["severity"], "error")
        self.assertGreater(starvation[0]["details"]["strict_signal_count_24h"], 0)
        self.assertEqual(starvation[0]["details"]["final_executable_count_24h"], 0)

    def test_real_contradiction_with_executable_no_fire(self) -> None:
        # One produced-but-blocked row + one produced AND executable row -> the
        # funnel has a reachable execution path -> no starvation.
        now = datetime.now(timezone.utc)
        _insert_decision(
            self.conn, symbol="BTCUSDT", raw=_REAL_CONTRADICTION_S_RAW,
            created_at=now, signal_grade="S",
        )
        _insert_decision(
            self.conn, symbol="ETHUSDT", raw=_FINAL_EXECUTABLE_RAW,
            created_at=now, signal_grade="S",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(EXECUTION_FUNNEL_STARVATION, _codes(result))


# ── 4. FOUR-ASPECT split FAILS CLOSED ───────────────────────────────────────


class TestFourAspectFailClosedCodexP1_4(unittest.TestCase):
    """``_after_execution_funnel_cutoff`` must FAIL CLOSED: a None cutoff
    (marker undeployed), a garbage cutoff, or a missing/garbage ``created_at``
    all demote to legacy (aspects blanked) — never fail-open to current. RED:
    the pre-fix gate returned True on every one of these."""

    CUTOFF = "2026-08-02T12:00:00Z"

    def _row(self, *, created_at: Any) -> dict:
        return {
            "symbol": "BTCUSDT",
            "signal_grade": "S",
            "raw_decision_json": _FINAL_EXECUTABLE_RAW,
            "trade_plan_json": None,
            "risk_check_json": _FINAL_EXECUTABLE_RAW.get("risk_check") or {},
            "created_at": created_at,
        }

    def _assert_legacy_blanked(self, r: dict) -> None:
        self.assertEqual(r["execution_funnel_scope"], "legacy")
        for aspect in ("llm_call_succeeded", "llm_plan_confirmed",
                       "risk_passed", "final_executable"):
            self.assertIsNone(r[aspect], aspect)

    def test_no_cutoff_fails_closed_to_legacy(self) -> None:
        # Cutoff None (marker not deployed in this DB) -> legacy, never current.
        r = _decision_row(self._row(created_at="2026-08-02T13:00:00Z"))
        self._assert_legacy_blanked(r)

    def test_garbage_cutoff_fails_closed_to_legacy(self) -> None:
        # A malformed marker value -> legacy (aspects blanked).
        r = _decision_row(
            self._row(created_at="2026-08-02T13:00:00Z"),
            execution_funnel_cutoff_utc="not-a-date",
        )
        self._assert_legacy_blanked(r)

    def test_missing_created_at_fails_closed_to_legacy(self) -> None:
        row = self._row(created_at="2026-08-02T13:00:00Z")
        row["created_at"] = None
        r = _decision_row(row, execution_funnel_cutoff_utc=self.CUTOFF)
        self._assert_legacy_blanked(r)

    def test_garbage_created_at_fails_closed_to_legacy(self) -> None:
        row = self._row(created_at="garbage")
        r = _decision_row(row, execution_funnel_cutoff_utc=self.CUTOFF)
        self._assert_legacy_blanked(r)

    def test_post_marker_row_current(self) -> None:
        # Control: a valid post-marker row still computes current aspects.
        r = _decision_row(
            self._row(created_at="2026-08-02T13:00:00Z"),
            execution_funnel_cutoff_utc=self.CUTOFF,
        )
        self.assertEqual(r["execution_funnel_scope"], "current")
        self.assertIs(r["final_executable"], True)
        self.assertIs(r["llm_plan_confirmed"], True)
