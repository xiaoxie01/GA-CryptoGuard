# -*- coding: utf-8 -*-
"""终审返工 Phase-2 P2-1 (2026-07-27): current-vs-historical diagnostic split
for ``deterministic_direction_from_failed_llm`` (requirement F verbatim):

  "add marker ``llm_failed_direction_fail_closed_v1``; marker-after violations
  = current error/warning; marker-before = historical audit; marker-missing =
  fail-closed; update migrations/Phase H/Phase I/EXPECTED_MARKERS/report tests;
  do NOT write marker to production."

Symptom #6: historical direction alerts repeat every hour; the report does not
distinguish current violations from legacy rows written before the
requirement-C fail-closed fix deployed. The marker ``applied_at`` is the split
point:

  - marker-AFTER  (analysis_time_utc >= applied_at): CURRENT warning — the
    requirement-C fail-closed block in ``apply_risk_to_decision`` was reverted
    or bypassed.
  - marker-BEFORE (analysis_time_utc <  applied_at): historical audit only —
    ``severity=legacy_info``, type
    ``deterministic_direction_from_failed_llm_historical``; does NOT inflate
    the current issue count and does NOT fail the gate (ok stays True,
    error_count stays 0).
  - marker-MISSING: fail-closed — ``_check_llm_failed_direction_fail_closed_
    marker_missing`` emits an ``error`` so callers detect the missing contract
    rather than receiving a silently-healthy report (req F: "marker 缺失必须
    fail-closed").

This test drives the REAL diagnostic chain on isolated PG
(``diagnose_state_consistency``) with seeded ``ga_decisions`` rows on either
side of the marker. No production DB mutation, no marker write to production
(``make_repo`` runs ``initialize_database`` into the scratch schema — the
marker is written in the TEST schema only, which is the release action
simulated for testing), no service restart, no commit/push/finish-work.

Marker registration + EXPECTED_MARKERS + summary keys are also asserted here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.diagnostics.state_consistency import (
    diagnose_state_consistency,
    LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _marker_applied_at(conn) -> str:
    """Read the fail-closed marker applied_at seeded by initialize_database
    into the scratch schema. Raises if absent — the marker MUST be present
    after init (EXPECTED_MARKERS asserts this in test_pg_migrations.py)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key = %s LIMIT 1",
            (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "GREEN: initialize_database must seed "
        f"{LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY!r} (req F + migrations)"
    )
    return str(row["applied_at"])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _seed_failed_bias_row(
    repo, *,
    symbol: str,
    at_dt: datetime,
    bias: str,
    created_at_dt: datetime | None = None,
) -> int:
    """Insert one ``ga_decisions`` row with llm_status=failed + market_bias=bias
    at the given UTC datetime. Mirrors the P8
    ``_seed_deterministic_direction_from_failed_llm`` helper shape so the
    diagnostic's row-scan picks it up.

    P1-2 (07-27 final review): the current-vs-historical cutoff is the row's
    PERSISTED ``created_at`` (``TIMESTAMPTZ DEFAULT NOW()``), NOT
    ``analysis_time_utc``. Tests that need a specific before/after marker
    relationship pass ``created_at_dt``; the row's ``created_at`` is UPDATEd
    immediately after insert (scratch schema only — production rows are
    immutable). When ``created_at_dt`` is None, ``created_at`` is left to
    ``DEFAULT NOW()`` (which is AFTER the marker, so the row is CURRENT).
    ``at_dt`` still seeds ``analysis_time`` / ``analysis_time_utc`` (display-
    only audit fields in the issue's ``time_window``).
    """
    decision = {
        "symbol": symbol,
        "analysis_time": _ms(at_dt),
        "analysis_time_utc": _iso(at_dt),
        "decision_type": "scheduled_analysis",
        "signal_grade": "D",
        "confidence": 0.3,
        "market_bias": bias,  # bullish/bearish: the leak signature
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": None,
        "previous_grade": "D",
        "llm_status": "failed",  # stored inside raw_decision_json
    }
    ga_id = repo.create_ga_decision(decision)
    if created_at_dt is not None:
        aware = created_at_dt if created_at_dt.tzinfo is not None else created_at_dt.replace(tzinfo=timezone.utc)
        with repo.conn.transaction():
            with repo.conn.cursor() as cur:
                cur.execute(
                    "UPDATE ga_decisions SET created_at = %s::timestamptz WHERE id = %s",
                    (aware.astimezone(timezone.utc), int(ga_id)),
                )
    return ga_id


# ── tests ───────────────────────────────────────────────────────────────────


class TestPgLLMFailedDirectionFailClosedMarkerP2_1:
    """Requirement F: the ``llm_failed_direction_fail_closed_v1`` marker is
    registered by ``initialize_database`` and gates the current-vs-historical
    split of ``deterministic_direction_from_failed_llm``. Marker absence is
    fail-closed (error), marker-before rows are historical audit (legacy_info),
    marker-after rows are current warnings."""

    def test_marker_key_constant_and_registration(self) -> None:
        """RED→GREEN: the marker key constant exists and the marker is written
        by ``initialize_database`` into the scratch schema. Lock-step with
        ``EXPECTED_MARKERS`` in test_pg_migrations.py (requirement F:
        "update ... EXPECTED_MARKERS")."""
        handle = make_repo()
        try:
            conn = handle.conn
            assert LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY == (
                "llm_failed_direction_fail_closed_v1"
            ), (
                "GREEN: marker key constant must be "
                "'llm_failed_direction_fail_closed_v1' (req F)"
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key FROM _migration_state WHERE key = %s",
                    (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1, (
                "GREEN: initialize_database must register the marker exactly "
                "once (req F + migrations.py registration sequence)"
            )
        finally:
            handle.close()

    def test_marker_after_row_is_current_warning_not_historical(self) -> None:
        """RED→GREEN: a failed/bias row whose ``created_at`` is AT OR AFTER
        the marker applied_at is a CURRENT warning
        (``deterministic_direction_from_failed_llm``, severity=warning), NOT a
        historical audit record. The gate stays True (warnings never fail ok),
        error_count stays 0.

        P1-2 (07-27 final review): the cutoff basis is the row's PERSISTED
        ``created_at`` (``TIMESTAMPTZ DEFAULT NOW()``), NOT ``analysis_time_utc``
        (TEXT ISO-8601). This test sets ``created_at`` explicitly AFTER the
        marker so the row is classified current regardless of its
        ``analysis_time_utc``."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            # created_at comfortably AFTER the marker (the contract boundary).
            after_dt = cutoff_dt + timedelta(hours=1)
            at_dt = cutoff_dt + timedelta(hours=1)
            ga_id = _seed_failed_bias_row(
                repo, symbol="SOLUSDT", at_dt=at_dt, bias="bullish",
                created_at_dt=after_dt,
            )
            result = diagnose_state_consistency(repo)
            cur = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm"
            ]
            hist = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm_historical"
            ]
            assert len(cur) >= 1, (
                "GREEN: marker-AFTER violation must surface as a CURRENT "
                "deterministic_direction_from_failed_llm warning (req F)"
            )
            assert len(hist) == 0, (
                "GREEN: marker-AFTER row must NOT be classified historical (req F)"
            )
            d = cur[0]
            assert d["severity"] == "warning", (
                f"GREEN: current violation severity must be warning, got "
                f"{d['severity']!r} (req F — not loosened, gate stays open)"
            )
            assert int(d["details"]["decision_id"]) == ga_id
            assert d["details"]["classification"] == "current"
            assert d["details"]["market_bias"] == "bullish"
            assert d["details"]["llm_status"] == "failed"
            # Gate invariants (P8 pin): warnings do not fail the gate.
            assert result["ok"] is True, (
                "GREEN: warning must NOT fail the gate (ok stays True)"
            )
            assert result["error_count"] == 0, (
                "GREEN: current warning must not add to error_count"
            )
            assert result["warning_count"] >= 1
        finally:
            handle.close()

    def test_marker_before_row_is_historical_audit_not_current(self) -> None:
        """RED→GREEN: a failed/bias row whose ``created_at`` is BEFORE the
        marker applied_at is HISTORICAL audit
        (``deterministic_direction_from_failed_llm_historical``,
        severity=legacy_info). It does NOT surface as a current warning, does
        NOT inflate warning_count, and does NOT fail the gate (ok stays True,
        error_count stays 0). This is the symptom #6 fix: the historical row no
        longer repeats as a current risk event each hour.

        P1-2 (07-27 final review): the cutoff basis is the row's PERSISTED
        ``created_at`` (``TIMESTAMPTZ DEFAULT NOW()``), NOT ``analysis_time_utc``
        (TEXT ISO-8601). This test sets ``created_at`` explicitly BEFORE the
        marker so the row is classified historical regardless of its
        ``analysis_time_utc``. The ``at_dt`` (analysis_time) is also set before
        the marker for display consistency."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            # created_at BEFORE the marker — the row was persisted before the
            # requirement-C fail-closed fix deployed (the contract boundary).
            created_dt = cutoff_dt - timedelta(hours=24)
            at_dt = cutoff_dt - timedelta(hours=24)
            ga_id = _seed_failed_bias_row(
                repo, symbol="ETHUSDT", at_dt=at_dt, bias="bearish",
                created_at_dt=created_dt,
            )
            result = diagnose_state_consistency(repo)
            cur = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm"
            ]
            hist = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm_historical"
            ]
            assert len(hist) >= 1, (
                "GREEN: marker-BEFORE row must surface as historical audit "
                "(deterministic_direction_from_failed_llm_historical, req F)"
            )
            assert len(cur) == 0, (
                "GREEN: marker-BEFORE row must NOT surface as a current "
                "warning (req F, symptom #6 — no hourly repeat)"
            )
            h = hist[0]
            assert h["severity"] == "legacy_info", (
                f"GREEN: historical severity must be legacy_info, got "
                f"{h['severity']!r} (req F)"
            )
            assert int(h["details"]["decision_id"]) == ga_id
            assert h["details"]["classification"] == "historical"
            assert h["details"]["market_bias"] == "bearish"
            # legacy_info does NOT count as a warning or an error.
            assert result["ok"] is True, (
                "GREEN: historical audit must NOT fail the gate"
            )
            assert result["error_count"] == 0, (
                "GREEN: historical audit must not add to error_count"
            )
            assert result["legacy_info_count"] >= 1, (
                "GREEN: historical audit must be counted in legacy_info_count"
            )
            # The current-warning count must NOT include the historical row.
            cur_warning_types = {
                i["type"] for i in result["issues"] if i.get("severity") == "warning"
            }
            assert "deterministic_direction_from_failed_llm" not in cur_warning_types, (
                "GREEN: historical row must not appear in current warning_count "
                "(symptom #6 — historical not counted as current)"
            )
        finally:
            handle.close()

    def test_marker_after_and_before_split_in_one_run(self) -> None:
        """RED→GREEN: both a marker-AFTER current row and a marker-BEFORE
        historical row in the same run are classified independently — one
        current warning + one historical legacy_info. The report can
        distinguish current from legacy (symptom #6).

        P1-2 (07-27 final review): the split basis is ``created_at``
        (persisted row creation time), NOT ``analysis_time_utc``. The before
        row's ``created_at`` is set BEFORE the marker (historical); the after
        row's ``created_at`` is set AFTER the marker (current)."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            before_dt = cutoff_dt - timedelta(hours=24)
            after_dt = cutoff_dt + timedelta(hours=1)
            # created_at BEFORE marker → historical; created_at AFTER → current.
            _seed_failed_bias_row(
                repo, symbol="ETHUSDT", at_dt=before_dt, bias="bearish",
                created_at_dt=before_dt,
            )
            _seed_failed_bias_row(
                repo, symbol="SOLUSDT", at_dt=after_dt, bias="bullish",
                created_at_dt=after_dt,
            )
            result = diagnose_state_consistency(repo)
            cur = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm"
            ]
            hist = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm_historical"
            ]
            assert len(cur) == 1, (
                f"GREEN: exactly one current violation expected, got {len(cur)}"
            )
            assert len(hist) == 1, (
                f"GREEN: exactly one historical audit expected, got {len(hist)}"
            )
            assert cur[0]["details"]["symbol"] == "SOLUSDT"
            assert hist[0]["details"]["symbol"] == "ETHUSDT"
            assert result["ok"] is True
            assert result["error_count"] == 0
            assert result["warning_count"] >= 1
            assert result["legacy_info_count"] >= 1
        finally:
            handle.close()

    def test_marker_missing_is_fail_closed_error(self) -> None:
        """RED→GREEN: when the marker is absent from ``_migration_state`` the
        directional check is skipped (no rows flagged against an undeployed
        contract) and ``_check_llm_failed_direction_fail_closed_marker_missing``
        emits a fail-closed ``error`` (req F: "marker 缺失必须 fail-closed"). The
        gate goes False (error_count > 0) so callers cannot receive a silently-
        healthy report. We DELETE the marker row in the scratch schema, run the
        check, then RESTORE it (test-only; no production mutation)."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            # Seed a marker-AFTER row that WOULD be a current warning — but the
            # marker is missing so it must NOT be flagged (undeployed contract).
            cutoff = _marker_applied_at(conn)
            at_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) + timedelta(hours=1)
            _seed_failed_bias_row(repo, symbol="BTCUSDT", at_dt=at_dt, bias="bullish")
            # Simulate marker absence: DELETE the marker row (test schema only).
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM _migration_state WHERE key = %s "
                        "RETURNING applied_at",
                        (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,),
                    )
                    deleted = cur.fetchone()
            assert deleted is not None, "GREEN: marker row existed before deletion"
            try:
                result = diagnose_state_consistency(repo)
                missing = [
                    i for i in result["issues"]
                    if i["type"] == "llm_failed_direction_fail_closed_marker_missing"
                ]
                cur_dir = [
                    i for i in result["issues"]
                    if i["type"] == "deterministic_direction_from_failed_llm"
                ]
                hist = [
                    i for i in result["issues"]
                    if i["type"] == "deterministic_direction_from_failed_llm_historical"
                ]
                assert len(missing) >= 1, (
                    "GREEN: marker absence must emit a fail-closed "
                    "llm_failed_direction_fail_closed_marker_missing error (req F)"
                )
                assert missing[0]["severity"] == "error", (
                    "GREEN: marker-missing must be severity=error (fail-closed, req F)"
                )
                # The directional check must be SKIPPED — no rows flagged.
                assert cur_dir == [], (
                    "GREEN: marker missing → directional check skipped, no "
                    "current warnings (req F)"
                )
                assert hist == [], (
                    "GREEN: marker missing → directional check skipped, no "
                    "historical audit either (req F)"
                )
                # Fail-closed: the gate goes False on the marker-missing error.
                assert result["ok"] is False, (
                    "GREEN: marker absence must fail the gate (fail-closed, req F)"
                )
                assert result["error_count"] >= 1, (
                    "GREEN: marker-missing error counted in error_count"
                )
            finally:
                # RESTORE the marker so the schema is left intact for teardown.
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO _migration_state(key, applied_at) "
                            "VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                            (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY, deleted["applied_at"]),
                        )
        finally:
            handle.close()

    def test_revert_fail_pre_split_treated_every_row_as_current(self) -> None:
        """Positive control (revert-fail): the pre-fix diagnostic used the
        ``market_data_contract_v1`` cutoff and classified EVERY failed/bias row
        as the SAME ``deterministic_direction_from_failed_llm`` warning with no
        historical split. This test seeds one marker-BEFORE row and asserts that
        the NEW diagnostic classifies it as historical (NOT current) — proving
        the split is load-bearing. If the fix were reverted (cutoff back to
        market_data_contract_v1, no historical branch), the marker-BEFORE row
        would surface as a current warning and this assertion would fail.

        P1-2 (07-27 final review): the split basis is ``created_at`` (persisted
        row creation time), NOT ``analysis_time_utc``. This test sets
        ``created_at`` explicitly BEFORE the marker so the row is classified
        historical regardless of its ``analysis_time_utc``."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            before_dt = cutoff_dt - timedelta(hours=24)
            _seed_failed_bias_row(
                repo, symbol="ADAUSDT", at_dt=before_dt, bias="bearish",
                created_at_dt=before_dt,
            )
            result = diagnose_state_consistency(repo)
            # The historical row must be legacy_info, NOT warning — the
            # pre-fix code would have emitted a warning here.
            hist = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm_historical"
            ]
            cur = [
                i for i in result["issues"]
                if i["type"] == "deterministic_direction_from_failed_llm"
            ]
            assert len(hist) == 1 and len(cur) == 0, (
                "GREEN: marker-BEFORE row classified historical not current "
                "(revert-fail: pre-split code would have made this a current warning)"
            )
            assert hist[0]["severity"] == "legacy_info"
            # Cross-check: the row is genuinely before the fail-closed marker.
            assert hist[0]["details"]["classification"] == "historical"
            assert hist[0]["details"]["market_bias"] == "bearish"
        finally:
            handle.close()