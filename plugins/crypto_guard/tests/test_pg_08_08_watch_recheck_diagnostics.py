# -*- coding: utf-8 -*-
"""08-08 (PRD Step 7): three new watch-recheck diagnostics + independent markers.

Each diagnostic is marker-gated (self-skips when its contract marker is absent),
queries ONLY rows with ``created_at >= marker.applied_at`` (exclude-only, so
pre-marker history never triggers), and has a fail-closed marker-missing check
in ``diagnose_state_consistency``:

  - ``watch_recheck_risk_shape_mismatch`` | marker
    ``watch_recheck_risk_shape_contract_v1`` | ``ga_decisions``
    (``decision_type='opportunity_watch_recheck'``) | fires when
    ``risk_check_json`` is NULL/JSON-null or ``risk_check_json->'ok'`` is not a
    boolean (wrong shape / non-bool / missing ok).
  - ``watch_review_payload_serialization_failure`` | marker
    ``watch_review_payload_serialization_contract_v1`` | ``agent_jobs``
    (``job_type='opportunity_watch_recheck'``) | fires when
    ``payload_json->'result'->'agent_review'->>'llm_failure_category' =
    'payload_serialization_failed'`` (structured field, no string matching).
  - ``watch_recheck_funnel_starvation`` | marker
    ``watch_recheck_funnel_contract_v1`` | error: ``ga_decisions`` +
    ``paper_orders.ga_decision_id``; warning: ``agent_jobs.result_json``.

RED-first + revert-fail: each behavior fails against the pre-implementation
diagnostics (no check exists, so no code fires) and passes after the GREEN
wiring. Marker-missing fail-closed is asserted by deleting the marker row from
``_migration_state`` and confirming ``diagnose_state_consistency`` emits the
marker-missing error.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo

_SYMBOL = "BTCUSDT"

# Marker keys (must match the GREEN constants in report_diagnostics.py /
# state_consistency.py / migrations.py).
RISK_SHAPE_MARKER = "watch_recheck_risk_shape_contract_v1"
SERIALIZATION_MARKER = "watch_review_payload_serialization_contract_v1"
FUNNEL_MARKER = "watch_recheck_funnel_contract_v1"

# Issue codes.
RISK_SHAPE_MISMATCH = "watch_recheck_risk_shape_mismatch"
RISK_SHAPE_MARKER_MISSING = "watch_recheck_risk_shape_contract_marker_missing"
SERIALIZATION_FAILURE = "watch_review_payload_serialization_failure"
SERIALIZATION_MARKER_MISSING = "watch_review_payload_serialization_contract_marker_missing"
FUNNEL_STARVATION = "watch_recheck_funnel_starvation"
FUNNEL_MARKER_MISSING = "watch_recheck_funnel_contract_marker_missing"

# The warning fires only on a run of >= this many consecutive rejections.
REJECTION_STREAK_MIN = 3


def _delete_marker(conn, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM _migration_state WHERE key = %s", (key,))
    conn.commit()


def _marker_ts(conn, key: str) -> datetime:
    row = conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key = %s", (key,)
    ).fetchone()
    assert row and row["applied_at"], f"marker {key} must be present"
    return row["applied_at"]


def _insert_recheck_decision(conn, *, risk_check, **overrides) -> int:
    """Insert an ``opportunity_watch_recheck`` ga_decisions row via the real
    repository path (``create_ga_decision``), returning its id.

    ``risk_check`` is stored into the ``risk_check_json`` column AND the whole
    decision dict into ``raw_decision_json`` (the persisted spec gates the
    funnel check reads live at the top level of ``raw_decision_json``).
    """
    from plugins.crypto_guard.storage.repository import CryptoGuardRepository

    repo = CryptoGuardRepository(conn)
    decision = {
        "symbol": _SYMBOL,
        "analysis_time": 1_700_000_000_000,
        "analysis_time_utc": "2033-05-18T08:33:20Z",
        "decision_type": "opportunity_watch_recheck",
        "signal_grade": "A",
        "confidence": 0.8,
        "decision": "enter_long",
        "market_bias": "bullish",
        "trend_stage": "middle",
        "risk_check": risk_check,
        "evidence": [],
        "counter_evidence": [],
        "skill_result_refs": {},
        "feishu_actions": [],
        "final_summary": "test",
        "summary": "test",
        "effective_signal_grade": "A",
        "has_trade_plan": False,
        "trade_plan": None,
        "plan_execution_state": "confirmed",
        "plan_status": "executable",
    }
    decision.update(overrides)
    return repo.create_ga_decision(decision)


def _insert_recheck_job(conn, *, payload=None, result=None, created_at=None) -> int:
    """Insert an ``opportunity_watch_recheck`` agent_jobs row, returning its id.

    ``created_at`` defaults to now (UTC) when omitted. The column default
    ``NOW()`` only applies when the column is absent from the INSERT, so an
    explicit NULL would leave ``created_at IS NULL`` and the diagnostic's
    ``created_at >= marker.applied_at`` gate would never match.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_jobs(job_type, source, session_id, payload_json,
                                   result_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                "opportunity_watch_recheck",
                "test",
                "test-session",
                json.dumps(payload or {}),
                json.dumps(result) if result is not None else None,
                created_at,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return int(row["id"])


def _insert_paper_order_for(conn, ga_decision_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_orders(ga_decision_id, symbol, side, order_type, status)
            VALUES (%s, %s, 'LONG', 'market', 'pending')
            """,
            (ga_decision_id, _SYMBOL),
        )
    conn.commit()


def _issue_types(result: dict) -> list[str]:
    return [i["type"] for i in result["issues"]]


# ── marker-missing fail-closed (diagnose_state_consistency) ────────────────


class TestMarkerMissingFailClosed:
    def test_risk_shape_marker_missing_fail_closed(self) -> None:
        """Deleting the risk-shape marker makes diagnose_state_consistency emit
        ``watch_recheck_risk_shape_contract_marker_missing`` (error)."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )

        handle = make_repo()
        try:
            _delete_marker(handle.conn, RISK_SHAPE_MARKER)
            result = diagnose_state_consistency(handle.repo)
            types = _issue_types(result)
            assert RISK_SHAPE_MARKER_MISSING in types, (
                f"marker-missing must fire; got {types}"
            )
            issue = next(i for i in result["issues"] if i["type"] == RISK_SHAPE_MARKER_MISSING)
            assert issue["severity"] == "error"
        finally:
            handle.close()

    def test_serialization_marker_missing_fail_closed(self) -> None:
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )

        handle = make_repo()
        try:
            _delete_marker(handle.conn, SERIALIZATION_MARKER)
            result = diagnose_state_consistency(handle.repo)
            types = _issue_types(result)
            assert SERIALIZATION_MARKER_MISSING in types, (
                f"marker-missing must fire; got {types}"
            )
        finally:
            handle.close()

    def test_funnel_marker_missing_fail_closed(self) -> None:
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )

        handle = make_repo()
        try:
            _delete_marker(handle.conn, FUNNEL_MARKER)
            result = diagnose_state_consistency(handle.repo)
            types = _issue_types(result)
            assert FUNNEL_MARKER_MISSING in types, (
                f"marker-missing must fire; got {types}"
            )
        finally:
            handle.close()


# ── watch_recheck_risk_shape_mismatch ──────────────────────────────────────


class TestRiskShapeMismatch:
    def test_fires_on_non_boolean_ok(self) -> None:
        """risk_check_json->'ok' is a string -> fires."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_decision(handle.conn, risk_check={"ok": "true"})
            result = diagnose_report_accuracy(handle.repo)
            assert RISK_SHAPE_MISMATCH in _issue_types(result)
        finally:
            handle.close()

    def test_fires_on_missing_ok(self) -> None:
        """risk_check_json is an object with no 'ok' key -> fires."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_decision(handle.conn, risk_check={})
            result = diagnose_report_accuracy(handle.repo)
            assert RISK_SHAPE_MISMATCH in _issue_types(result)
        finally:
            handle.close()

    def test_fires_on_json_null_risk_check(self) -> None:
        """risk_check_json is JSON null (decodes to None) -> fires."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            # create_ga_decision stores ``risk_check or {}``; a JSON-null
            # risk_check must be seeded directly so the column holds 'null'.
            with handle.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ga_decisions(
                        symbol, analysis_time, analysis_time_utc, decision_type,
                        signal_grade, confidence, decision, skill_result_refs_json,
                        evidence_json, counter_evidence_json, risk_check_json,
                        feishu_actions_json, final_summary, raw_decision_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        _SYMBOL, 1_700_000_000_000, "2033-05-18T08:33:20Z",
                        "opportunity_watch_recheck", "A", 0.8, "enter_long",
                        "{}", "[]", "[]", "null", "[]", "test",
                        json.dumps({"decision_type": "opportunity_watch_recheck"}),
                    ),
                )
            handle.conn.commit()
            result = diagnose_report_accuracy(handle.repo)
            assert RISK_SHAPE_MISMATCH in _issue_types(result)
        finally:
            handle.close()

    def test_negative_boolean_ok_does_not_fire(self) -> None:
        """risk_check_json->'ok' is a real boolean true -> no fire."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_decision(handle.conn, risk_check={"ok": True})
            result = diagnose_report_accuracy(handle.repo)
            assert RISK_SHAPE_MISMATCH not in _issue_types(result)
        finally:
            handle.close()


# ── watch_review_payload_serialization_failure ─────────────────────────────


class TestPayloadSerializationFailure:
    def test_fires_on_structured_category(self) -> None:
        """payload_json->'result'->'agent_review'->>'llm_failure_category' ==
        'payload_serialization_failed' -> fires (structured field)."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_job(
                handle.conn,
                payload={
                    "result": {
                        "agent_review": {
                            "llm_failure_category": "payload_serialization_failed",
                        }
                    }
                },
            )
            result = diagnose_report_accuracy(handle.repo)
            assert SERIALIZATION_FAILURE in _issue_types(result)
        finally:
            handle.close()

    def test_negative_other_category_does_not_fire(self) -> None:
        """A different llm_failure_category (or none) must NOT fire."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_job(
                handle.conn,
                payload={
                    "result": {
                        "agent_review": {
                            "llm_failure_category": "llm_parse_failed",
                        }
                    }
                },
            )
            result = diagnose_report_accuracy(handle.repo)
            assert SERIALIZATION_FAILURE not in _issue_types(result)
        finally:
            handle.close()


# ── watch_recheck_funnel_starvation ────────────────────────────────────────


class TestFunnelStarvation:
    def _executable_recheck_decision(self, **overrides) -> dict:
        decision = {
            "plan_execution_state": "confirmed",
            "plan_status": "executable",
            "has_trade_plan": True,
            "risk_check": {"ok": True},
            "effective_signal_grade": "S",
            "signal_grade": "S",
            "trade_plan": {
                "side": "LONG",
                "entry_type": "market",
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "take_profits": [{"price": 108.0}],
                "quantity": 0.5,
                "reason": "watch recheck",
            },
        }
        decision.update(overrides)
        return decision

    def test_error_fires_on_unbridged_executable_recheck(self) -> None:
        """An executable recheck decision (all persisted spec gates) with NO
        paper_orders row -> error fires."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_decision(
                handle.conn,
                **self._executable_recheck_decision(),
            )
            result = diagnose_report_accuracy(handle.repo)
            types = _issue_types(result)
            assert FUNNEL_STARVATION in types, f"funnel error must fire; got {types}"
            issue = next(i for i in result["issues"] if i["type"] == FUNNEL_STARVATION)
            assert issue["severity"] == "error"
        finally:
            handle.close()

    def test_error_not_fires_when_bridged(self) -> None:
        """The same executable recheck decision WITH a paper_orders row linked
        by ga_decision_id -> no error (the funnel completed)."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            decision_id = _insert_recheck_decision(
                handle.conn,
                **self._executable_recheck_decision(),
            )
            _insert_paper_order_for(handle.conn, decision_id)
            result = diagnose_report_accuracy(handle.repo)
            assert FUNNEL_STARVATION not in _issue_types(result)
        finally:
            handle.close()

    def test_error_not_fires_on_non_executable(self) -> None:
        """A recheck decision that is NOT executable (plan_status withheld) must
        NOT fire the error — legitimate non-executable, not starvation."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            _insert_recheck_decision(
                handle.conn,
                **self._executable_recheck_decision(plan_status="withheld"),
            )
            result = diagnose_report_accuracy(handle.repo)
            assert FUNNEL_STARVATION not in _issue_types(result)
        finally:
            handle.close()

    def test_warning_fires_on_rejection_streak(self) -> None:
        """>= REJECTION_STREAK_MIN consecutive post-marker recheck rejections
        with zero orders -> warning fires with a reason distribution."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            base = _marker_ts(handle.conn, FUNNEL_MARKER)
            reasons = ["broker_verifier_veto", "risk", "grade"]
            for i, reason in enumerate(reasons):
                _insert_recheck_job(
                    handle.conn,
                    result={"rejected": True, "reason": reason},
                    created_at=base + timedelta(seconds=i + 1),
                )
            result = diagnose_report_accuracy(handle.repo)
            types = _issue_types(result)
            assert FUNNEL_STARVATION in types, f"funnel warning must fire; got {types}"
            issue = next(i for i in result["issues"] if i["type"] == FUNNEL_STARVATION)
            assert issue["severity"] == "warning"
            details = issue["details"]
            assert details.get("rejection_reason_distribution"), (
                f"warning must carry a reason distribution; {details}"
            )
        finally:
            handle.close()

    def test_warning_not_fires_on_weak_market(self) -> None:
        """A short rejection run interrupted by a REAL producer success row
        (the handler returns ``{"created": True, ...}`` — NOT an
        ``order_created`` key, which is only the watch ``recheck_status``
        column) must reset the streak (max streak < REJECTION_STREAK_MIN) so
        the warning does NOT fire — weak markets are normal."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            diagnose_report_accuracy,
        )

        handle = make_repo()
        try:
            base = _marker_ts(handle.conn, FUNNEL_MARKER)
            # rejected, rejected, real-success (created=True), rejected ->
            # max streak 2 < 3. Uses the exact shape
            # handle_opportunity_watch_recheck persists to agent_jobs.result_json.
            _insert_recheck_job(
                handle.conn,
                result={"rejected": True, "reason": "risk"},
                created_at=base + timedelta(seconds=1),
            )
            _insert_recheck_job(
                handle.conn,
                result={"rejected": True, "reason": "risk"},
                created_at=base + timedelta(seconds=2),
            )
            _insert_recheck_job(
                handle.conn,
                result={
                    "ok": True, "watch_id": 1, "internal_only": True,
                    "sent": False, "paper_order_id": 1, "created": True,
                    "ga_decision_id": 1, "text": "内部观察上下文已归档（不推送）",
                },
                created_at=base + timedelta(seconds=3),
            )
            _insert_recheck_job(
                handle.conn,
                result={"rejected": True, "reason": "grade"},
                created_at=base + timedelta(seconds=4),
            )
            result = diagnose_report_accuracy(handle.repo)
            assert FUNNEL_STARVATION not in _issue_types(result)
        finally:
            handle.close()
