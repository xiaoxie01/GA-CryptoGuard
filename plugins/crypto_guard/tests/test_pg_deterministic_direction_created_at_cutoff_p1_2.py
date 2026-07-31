# -*- coding: utf-8 -*-
"""终审返工 P1-1 + P1-2 (2026-07-27): failed-only contract narrowing AND
created_at-based marker cutoff for ``deterministic_direction_from_failed_llm``.

Two coupled Codex final-review findings on the Phase-2 P2-1 (07-27) work:

P1-1 — failed/disabled contract inconsistency
---------------------------------------------
``risk_engine.apply_risk_to_decision`` already only fail-closes
``market_bias="unknown"`` for ``llm_status=="failed"`` (the correct contract —
the risk_engine scoping is untouched here). The diagnostic
``_check_deterministic_direction_from_failed_llm`` previously ALSO checked
``llm_status in {"failed", "disabled"}`` (state_consistency.py ~line 2957). That
is inconsistent: ``disabled`` is the ``CRYPTO_GUARD_LLM_ANALYSIS=0``
deterministic-only operating mode — the deterministic direction IS the intended
product there, so a ``disabled`` row with bullish/bearish bias MUST NOT be
flagged as a current warning NOR as a historical ``legacy_info``. The diagnostic
guard is narrowed to ``{"failed"}`` ONLY.

P1-2 — marker cutoff MUST use persisted time (created_at)
---------------------------------------------------------
The previous implementation classified current vs historical by parsing
``analysis_time_utc`` (a TEXT ISO-8601 column) against the marker's
``applied_at``. That cross-format comparison is unreliable. The contract
boundary is the row's PERSISTED creation time: ``ga_decisions.created_at``
(``TIMESTAMPTZ DEFAULT NOW()``, schema_postgres.sql:233). The SQL now filters
``raw_decision_json->>'llm_status' = 'failed'`` FIRST, then
``market_bias IN ('bullish','bearish')`` (a column, not JSONB), then splits
current vs historical purely by ``created_at >= cutoff`` (both aware
datetimes from psycopg — direct comparison, no Python ``_coerce_iso`` helper).
``analysis_time_utc`` is kept ONLY in the issue's ``time_window`` for audit
display — it is NOT the deployment-contract boundary.

The four P1-2 regression tests here drive the REAL diagnostic chain
(``diagnose_state_consistency``) on isolated PG scratch schemas with seeded
``ga_decisions`` rows whose ``created_at`` is set explicitly (the scratch schema
is the only place ``created_at`` is ever UPDATEd; production rows are immutable).
The P1-1 behavior test proves a ``disabled`` + bullish row AFTER the marker is
invisible to this diagnostic (no current warning, no historical legacy_info),
while a ``failed`` + bullish row after the marker IS reported as a current
warning. A revert-fail control pins the seed shape so a future re-broadening of
the guard back to ``{"failed", "disabled"}`` flips the disabled-row test RED.

No production DB mutation, no marker write to production (``make_repo`` runs
``initialize_database`` into the scratch schema only), no service restart, no
commit/push/release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.diagnostics.state_consistency import (
    diagnose_state_consistency,
    LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _marker_applied_at_dt(conn) -> datetime:
    """Read the fail-closed marker applied_at as an aware UTC datetime.

    The marker is seeded by ``initialize_database`` into the scratch schema.
    ``applied_at`` is ``TIMESTAMPTZ`` so psycopg returns an aware datetime; if
    somehow naive, assume UTC. Raises if absent — the marker MUST be present
    after init.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key = %s LIMIT 1",
            (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "GREEN: initialize_database must seed "
        f"{LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY!r}"
    )
    dt = row["applied_at"]
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _seed_decision_row(
    repo, *,
    symbol: str,
    analysis_time_dt: datetime,
    llm_status: str,
    bias: str,
) -> int:
    """Insert one ``ga_decisions`` row with the given llm_status + market_bias.

    The row's ``created_at`` is left to ``DEFAULT NOW()`` here; callers that
    need a specific ``created_at`` (relative to the marker) UPDATE it
    immediately after insert via ``_set_created_at``. ``analysis_time`` /
    ``analysis_time_utc`` are derived from ``analysis_time_dt`` so the audit
    ``time_window`` is populated, but per P1-2 they are NOT the cutoff basis.
    """
    decision = {
        "symbol": symbol,
        "analysis_time": _ms(analysis_time_dt),
        "analysis_time_utc": _iso(analysis_time_dt),
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
        "llm_status": llm_status,  # stored inside raw_decision_json
    }
    return repo.create_ga_decision(decision)


def _set_created_at(repo, ga_id: int, dt: datetime) -> None:
    """Override ``ga_decisions.created_at`` for one row (scratch schema only).

    ``created_at`` is ``TIMESTAMPTZ DEFAULT NOW()`` and cannot be set via
    ``create_ga_decision`` (which omits it from the INSERT column list). Tests
    that need a specific ``created_at`` relative to the marker cutoff UPDATE
    the row directly inside a transaction. This is test-only and never touches
    production.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    with repo.conn.transaction():
        with repo.conn.cursor() as cur:
            cur.execute(
                "UPDATE ga_decisions SET created_at = %s::timestamptz WHERE id = %s",
                (aware.astimezone(timezone.utc), int(ga_id)),
            )


def _collect_directional_issues(result: dict) -> tuple[list, list]:
    """Split diagnose_state_consistency issues into (current, historical)."""
    cur = [
        i for i in result["issues"]
        if i["type"] == "deterministic_direction_from_failed_llm"
    ]
    hist = [
        i for i in result["issues"]
        if i["type"] == "deterministic_direction_from_failed_llm_historical"
    ]
    return cur, hist


# ── P1-2: created_at-based marker cutoff ────────────────────────────────────


class TestPgDeterministicDirectionCreatedAtCutoffP1_2:
    """P1-2: the current-vs-historical split MUST use ``ga_decisions.created_at``
    (the persisted row creation time), NOT ``analysis_time_utc`` (TEXT ISO-8601).
    The SQL filters ``llm_status='failed'`` AND ``market_bias IN
    ('bullish','bearish')`` FIRST, then the Python loop classifies by
    ``created_at >= cutoff``. ``analysis_time_utc`` is display-only."""

    def test_created_at_after_marker_analysis_time_before_is_current(self) -> None:
        """RED→GREEN P1-2 case 1: ``created_at`` AFTER marker +
        ``analysis_time_utc`` BEFORE marker → MUST be CURRENT (warning).

        This is the load-bearing case: under the OLD ``analysis_time_utc``-
        based classification this row would have been historical (its
        ``analysis_time_utc`` predates the marker). Under the NEW
        ``created_at``-based classification it is CURRENT — the row was
        persisted after the fail-closed fix deployed, so its leak is a current
        violation regardless of the analysis_time it claims. This proves
        ``created_at`` (not ``analysis_time_utc``) is the cutoff basis.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            # analysis_time_utc BEFORE the marker (would be historical under
            # the old classification).
            analysis_dt = cutoff - timedelta(hours=24)
            # created_at AFTER the marker (persisted after the fix deployed).
            created_dt = cutoff + timedelta(hours=1)
            ga_id = _seed_decision_row(
                repo, symbol="SOLUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="failed", bias="bullish",
            )
            _set_created_at(repo, ga_id, created_dt)
            result = diagnose_state_consistency(repo)
            cur, hist = _collect_directional_issues(result)
            assert len(cur) >= 1, (
                "GREEN P1-2: created_at AFTER marker + analysis_time_utc "
                "BEFORE marker must be CURRENT (warning) — created_at is the "
                "cutoff basis, not analysis_time_utc."
            )
            assert len(hist) == 0, (
                "GREEN P1-2: this row must NOT be classified historical — its "
                "created_at is after the marker even though analysis_time_utc "
                "predates it."
            )
            d = cur[0]
            assert d["severity"] == "warning"
            assert int(d["details"]["decision_id"]) == ga_id
            assert d["details"]["classification"] == "current"
            assert d["details"]["llm_status"] == "failed"
            # analysis_time_utc is kept in time_window for audit display only.
            assert "analysis_time_utc" in d["time_window"]
        finally:
            handle.close()

    def test_created_at_before_marker_analysis_time_after_is_historical(self) -> None:
        """RED→GREEN P1-2 case 2: ``created_at`` BEFORE marker +
        ``analysis_time_utc`` AFTER marker → MUST be HISTORICAL (legacy_info).

        Under the OLD ``analysis_time_utc``-based classification this row would
        have been current (its ``analysis_time_utc`` is after the marker). Under
        the NEW ``created_at``-based classification it is HISTORICAL — the row
        was persisted before the fail-closed fix deployed, so its leak predates
        the contract regardless of the analysis_time it claims.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            # analysis_time_utc AFTER the marker (would be current under the
            # old classification).
            analysis_dt = cutoff + timedelta(hours=1)
            # created_at BEFORE the marker (persisted before the fix deployed).
            created_dt = cutoff - timedelta(hours=24)
            ga_id = _seed_decision_row(
                repo, symbol="ETHUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="failed", bias="bearish",
            )
            _set_created_at(repo, ga_id, created_dt)
            result = diagnose_state_consistency(repo)
            cur, hist = _collect_directional_issues(result)
            assert len(hist) >= 1, (
                "GREEN P1-2: created_at BEFORE marker + analysis_time_utc "
                "AFTER marker must be HISTORICAL (legacy_info) — created_at is "
                "the cutoff basis, not analysis_time_utc."
            )
            assert len(cur) == 0, (
                "GREEN P1-2: this row must NOT be classified current — its "
                "created_at predates the marker even though analysis_time_utc "
                "is after it."
            )
            h = hist[0]
            assert h["severity"] == "legacy_info"
            assert int(h["details"]["decision_id"]) == ga_id
            assert h["details"]["classification"] == "historical"
            assert h["details"]["llm_status"] == "failed"
        finally:
            handle.close()

    def test_created_at_equal_marker_is_current_boundary_inclusive(self) -> None:
        """RED→GREEN P1-2 case 3: ``created_at`` == marker applied_at → MUST be
        CURRENT (boundary inclusive ``>=``).

        The contract is ``created_at >= cutoff`` is current. A row persisted at
        exactly the marker's applied_at is ON the boundary — it is current
        (the fix was deployed at that instant, so a row at that exact time is
        post-deployment). The SQL comparison is ``gd.created_at >= %s::timestamptz``.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            # created_at exactly == marker applied_at (boundary).
            analysis_dt = cutoff + timedelta(hours=1)
            ga_id = _seed_decision_row(
                repo, symbol="BNBUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="failed", bias="bullish",
            )
            _set_created_at(repo, ga_id, cutoff)
            result = diagnose_state_consistency(repo)
            cur, hist = _collect_directional_issues(result)
            assert len(cur) >= 1, (
                "GREEN P1-2: created_at == marker applied_at must be CURRENT "
                "(boundary inclusive >=)."
            )
            assert len(hist) == 0, (
                "GREEN P1-2: boundary row must NOT be historical — >= is "
                "inclusive of the marker time."
            )
            d = cur[0]
            assert d["severity"] == "warning"
            assert d["details"]["classification"] == "current"
        finally:
            handle.close()

    def test_unparseable_created_at_fails_toward_surfacing_current(self) -> None:
        """RED→GREEN P1-2 case 4: when ``created_at`` cannot be compared
        against the cutoff, the check fails toward SURFACING (current), never
        hiding the row.

        ``ga_decisions.created_at`` has ``DEFAULT NOW()`` and is NOT NULL in
        the greenfield schema, so a real NULL is impossible in practice. The
        fail-toward-surfacing contract is exercised by making the marker cutoff
        itself unparseable/None-equivalent: we DELETE the marker's applied_at
        value (set it to NULL) so the cutoff comparison treats the row as
        current. When the cutoff cannot be established as a comparable
        timestamptz, the diagnostic must surface the row rather than silently
        skip it. This mirrors the marker-missing fail-closed posture but at
        the per-row comparison level.

        Implementation note: the production code path resolves the cutoff as a
        timestamptz from the marker; if that resolution yields None the
        directional check is skipped (handled by the marker-missing check).
        The fail-toward-surfacing contract at the row level is: if the row's
        ``created_at`` is non-comparable (None), the row is treated as current.
        We simulate the non-comparable case by setting the row's created_at to
        NULL (the scratch schema permits this even though production never
        would) and asserting the check surfaces it as current.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            analysis_dt = cutoff + timedelta(hours=1)
            ga_id = _seed_decision_row(
                repo, symbol="XRPUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="failed", bias="bullish",
            )
            # Simulate a non-comparable created_at by NULLing it (scratch
            # schema only — production created_at is NOT NULL DEFAULT NOW()).
            with repo.conn.transaction():
                with repo.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ga_decisions SET created_at = NULL WHERE id = %s",
                        (int(ga_id),),
                    )
            result = diagnose_state_consistency(repo)
            cur_issues, hist = _collect_directional_issues(result)
            # Fail-toward-surfacing: the row with non-comparable created_at must
            # be surfaced as CURRENT (not silently hidden, not historical).
            assert len(cur_issues) >= 1, (
                "GREEN P1-2: a failed/bias row with non-comparable (NULL) "
                "created_at must fail toward surfacing as CURRENT, never be "
                "silently hidden."
            )
            d = cur_issues[0]
            assert d["severity"] == "warning"
            assert int(d["details"]["decision_id"]) == ga_id
            assert d["details"]["classification"] == "current"
        finally:
            handle.close()


# ── P1-1: disabled not reported ─────────────────────────────────────────────


class TestPgDisabledNotReportedP1_1:
    """P1-1: ``llm_status="disabled"`` (CRYPTO_GUARD_LLM_ANALYSIS=0
    deterministic-only mode) rows MUST NOT be flagged by
    ``deterministic_direction_from_failed_llm`` — neither as a current warning
    nor as a historical ``legacy_info``. The deterministic direction IS the
    intended product in disabled mode; the diagnostic guard is narrowed to
    ``{"failed"}`` ONLY. A ``failed`` + bias row after the marker IS still
    reported as a current warning."""

    def test_disabled_row_after_marker_not_reported(self) -> None:
        """RED→GREEN P1-1: a ``disabled`` + bullish row with ``created_at``
        AFTER the marker is invisible to this diagnostic — no current warning,
        no historical legacy_info. ``disabled`` is deterministic-only mode; the
        deterministic bullish bias is the intended product, not a leak.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            analysis_dt = cutoff + timedelta(hours=1)
            created_dt = cutoff + timedelta(hours=1)
            ga_id = _seed_decision_row(
                repo, symbol="ADAUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="disabled", bias="bullish",
            )
            _set_created_at(repo, ga_id, created_dt)
            result = diagnose_state_consistency(repo)
            cur, hist = _collect_directional_issues(result)
            # The disabled row must be invisible to BOTH current and historical
            # branches of this diagnostic.
            disabled_cur = [
                i for i in cur if int(i["details"]["decision_id"]) == ga_id
            ]
            disabled_hist = [
                i for i in hist if int(i["details"]["decision_id"]) == ga_id
            ]
            assert disabled_cur == [], (
                "GREEN P1-1: disabled + bullish row after marker must NOT be "
                "flagged as a current warning — disabled is deterministic-only "
                "mode, the bias is the intended product."
            )
            assert disabled_hist == [], (
                "GREEN P1-1: disabled + bullish row after marker must NOT be "
                "flagged as historical legacy_info either — disabled rows are "
                "invisible to this diagnostic entirely."
            )
        finally:
            handle.close()

    def test_failed_row_after_marker_still_reported_current(self) -> None:
        """RED→GREEN P1-1 (control): a ``failed`` + bullish row with
        ``created_at`` AFTER the marker IS still reported as a current warning.
        This proves the failed-only narrowing did not accidentally drop the
        ``failed`` check — the fail-closed contract for ``failed`` rows is
        intact.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            analysis_dt = cutoff + timedelta(hours=1)
            created_dt = cutoff + timedelta(hours=1)
            ga_id = _seed_decision_row(
                repo, symbol="DOTUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="failed", bias="bullish",
            )
            _set_created_at(repo, ga_id, created_dt)
            result = diagnose_state_consistency(repo)
            cur, hist = _collect_directional_issues(result)
            assert len(cur) >= 1, (
                "GREEN P1-1 control: failed + bullish row after marker MUST "
                "still be reported as a current warning — the failed-only "
                "narrowing must not drop the failed check."
            )
            failed_cur = [
                i for i in cur if int(i["details"]["decision_id"]) == ga_id
            ]
            assert len(failed_cur) == 1
            assert failed_cur[0]["severity"] == "warning"
            assert failed_cur[0]["details"]["llm_status"] == "failed"
        finally:
            handle.close()

    def test_revert_fail_disabled_seed_shape_would_be_caught_by_old_guard(self) -> None:
        """Revert-fail / positive control for P1-1.

        The pre-fix diagnostic guard was ``llm_status in {"failed", "disabled"}``
        (state_consistency.py ~line 2957). This test seeds the EXACT shape that
        the old guard would have caught — ``market_bias="bullish"`` AND
        ``llm_status="disabled"`` in the same row — and asserts the NEW
        (failed-only) diagnostic does NOT flag it. If a future change re-
        broadens the guard back to ``{"failed", "disabled"}``, this row would
        be flagged and this assertion would flip RED, catching the revert.

        The seed shape here is identical to the failed-row seed shape used
        elsewhere in this file and in the P2-1 marker test — only
        ``llm_status`` differs (``disabled`` vs ``failed``). This is the
        load-bearing evidence that the guard narrowing is what makes the
        disabled row invisible, not some other difference.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at_dt(conn)
            analysis_dt = cutoff + timedelta(hours=1)
            created_dt = cutoff + timedelta(hours=1)
            # The disabled + bullish seed — the exact shape the old
            # {"failed, disabled} guard would have caught.
            ga_id = _seed_decision_row(
                repo, symbol="LTCUSDT",
                analysis_time_dt=analysis_dt,
                llm_status="disabled", bias="bullish",
            )
            _set_created_at(repo, ga_id, created_dt)
            # Confirm the seed shape: market_bias and llm_status are BOTH set
            # to the values the old guard matched on. This is the revert-fail
            # pin — if the guard is re-broadened, this row is caught.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT market_bias, raw_decision_json->>'llm_status' AS llm_status "
                    "FROM ga_decisions WHERE id = %s",
                    (int(ga_id),),
                )
                row = cur.fetchone()
            assert row is not None
            assert str(row["market_bias"]).lower() == "bullish", (
                "revert-fail control setup: the disabled-row seed must carry "
                "market_bias=bullish (the value the old guard matched on)."
            )
            assert str(row["llm_status"]).lower() == "disabled", (
                "revert-fail control setup: the disabled-row seed must carry "
                "llm_status=disabled (the value the old guard matched on)."
            )
            result = diagnose_state_consistency(repo)
            cur_issues, hist = _collect_directional_issues(result)
            disabled_flagged = [
                i for i in (cur_issues + hist)
                if int(i["details"]["decision_id"]) == ga_id
            ]
            assert disabled_flagged == [], (
                "revert-fail control: the NEW failed-only guard must NOT flag "
                "this disabled+bullish row. If this fails, the guard was re-"
                "broadened back to {failed, disabled} — the P1-1 narrowing "
                "was reverted."
            )
        finally:
            handle.close()