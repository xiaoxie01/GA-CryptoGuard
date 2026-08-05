# -*- coding: utf-8 -*-
"""08-04 Phase 6 residual P1 fix (2026-08-04): ``opportunity_watch_not_materialized``
must treat ANY matching ``opportunity_watches`` row as proof of materialization —
not only rows still ``status = 'active'``.

RED-first + revert-fail:

A watch that was materialized (created by the P0-2 wire-in) and LATER moved to a
terminal state is still a materialized watch. The watcher
(``opportunity_watcher.py``) transitions ``active`` -> ``triggered`` (condition
hit, recheck enqueued), ``invalidated`` (continuity broken) or ``expired`` (TTL
elapsed). The pre-fix diagnostic SQL in
``report_diagnostics._check_opportunity_watch_not_materialized`` (line ~3852)
guarded with ``WHERE status = 'active'``, so a triggered/invalidated/expired
watch that matched the dedupe_key / ga_decision_id was NOT found and the
decision was false-flagged ``opportunity_watch_not_materialized`` — a broken
funnel claim for a funnel that DID materialize and then completed.

Phase 6 regression (implement.md 6.1): drop the ``status='active'`` predicate;
any matching watch row proves materialization.

RED: against the pre-fix SQL all three terminal-status tests fail (the code
emits ``opportunity_watch_not_materialized`` for a materialized-but-terminal
watch). GREEN: after dropping the predicate they pass. The no-watch positive
control stays green on both — the check still fires when nothing was ever
materialized.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.diagnostics.report_diagnostics import (
    OPPORTUNITY_WATCH_NOT_MATERIALIZED,
    diagnose_report_accuracy,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo

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


def _insert_decision(conn, *, symbol: str, raw: dict) -> int:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
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
                    symbol, now_ms, now.isoformat().replace("+00:00", "Z"), "scheduled",
                    "A", 0.6, "neutral", "middle", "hold",
                    json.dumps([]), json.dumps({}), json.dumps([]),
                    json.dumps({}), json.dumps([]), "summary",
                    json.dumps(raw), now,
                ),
            )
            return int(cur.fetchone()["id"])


def _insert_watch(
    conn, *, symbol: str, status: str, dedupe_key: str,
) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_watches"
                "(symbol, direction, watch_reason, watch_condition_json, "
                " invalid_condition_json, status, dedupe_key, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    symbol, "LONG", "test",
                    json.dumps(_STRUCTURED_WATCH["conditions"]),
                    json.dumps(_STRUCTURED_WATCH["invalid_condition"]),
                    status, dedupe_key, datetime.now(timezone.utc),
                ),
            )
            return int(cur.fetchone()["id"])


def _codes(result: dict) -> set[str]:
    return {i["type"] for i in result["issues"]}


class TestWatchMaterializationTerminalStatus(unittest.TestCase):
    """Phase 6: a materialized watch in ANY terminal status proves the funnel
    completed — the diagnostic must NOT flag the decision."""

    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _seed_gate_satisfied_decision(self, symbol: str) -> int:
        return _insert_decision(self.conn, symbol=symbol, raw=_MATERIALIZE_RAW)

    def test_no_fire_on_triggered_watch(self) -> None:
        # The condition hit; the watcher moved the watch to 'triggered' and
        # enqueued the recheck. The watch WAS materialized — no broken funnel.
        self._seed_gate_satisfied_decision("BTCUSDT")
        _insert_watch(
            self.conn, symbol="BTCUSDT", status="triggered",
            dedupe_key="auto:BTCUSDT:LONG",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result), result)

    def test_no_fire_on_invalidated_watch(self) -> None:
        # Continuity broke; the watcher moved the watch to 'invalidated' with an
        # invalidated_reason. Materialization still happened.
        self._seed_gate_satisfied_decision("ETHUSDT")
        _insert_watch(
            self.conn, symbol="ETHUSDT", status="invalidated",
            dedupe_key="auto:ETHUSDT:LONG",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result), result)

    def test_no_fire_on_expired_watch(self) -> None:
        # TTL elapsed; the watcher moved the watch to 'expired'. Materialization
        # still happened.
        self._seed_gate_satisfied_decision("SOLUSDT")
        _insert_watch(
            self.conn, symbol="SOLUSDT", status="expired",
            dedupe_key="auto:SOLUSDT:LONG",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertNotIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result), result)

    def test_fires_when_no_watch_ever_materialized(self) -> None:
        # Positive control: no watch row at all must STILL fire (the funnel
        # really broke). Stays green before AND after the fix.
        self._seed_gate_satisfied_decision("DOGEUSDT")
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result), result)

    def test_fires_on_active_status_match_after_terminal_row_for_other_symbol(self) -> None:
        # Mixed-set control: a triggered row for symbol A must NOT satisfy the
        # dedupe_key of symbol B — a genuinely unmaterialized B decision fires
        # even while A's terminal watch exists.
        self._seed_gate_satisfied_decision("XRPUSDT")
        _insert_watch(
            self.conn, symbol="OTHERUSDT", status="triggered",
            dedupe_key="auto:OTHERUSDT:LONG",
        )
        result = diagnose_report_accuracy(self.repo)
        self.assertIn(OPPORTUNITY_WATCH_NOT_MATERIALIZED, _codes(result), result)
