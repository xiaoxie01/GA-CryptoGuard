# -*- coding: utf-8 -*-
"""07-31 production fix P1-4 (2026-07-31): diagnostics & reporting integrity
for the schema-repair / breaker / preset chain — RED-first behavioral test +
revert-fail.

Production evidence #4: the current hourly report shows
``llm_failure_rate_high`` + ``llm_circuit_breaker_open`` as CURRENT errors
driven by the pre-fix batch 15m:1785487499999 (5 schema failures polluting
the breaker rate window -> breaker open -> 10 breaker_skipped rows with
provider_call_count=0). Post-fix those failures are repairable/isolated, so
historical pre-deployment batches must NOT repeat as current errors, and a
schema-repair must render as a SUCCESS (with repair count incremented), never
as a failure/retry.

Four contracts (P1-4):

1. Independent marker ``llm_schema_breaker_preset_integrity_v1``:
   - registered EXACTLY ONCE by ``initialize_database`` (release path only);
   - marker-missing is fail-closed (``llm_schema_breaker_preset_integrity_
     marker_missing`` error + the two LLM checks SKIPPED — no silent green);
   - marker-BEFORE batches (analysis_time < applied_at) demote to
     ``legacy_info`` — pre-deployment historical batches are archived as
     legacy, never as current errors (symptom #4: no hourly repeat);
   - marker-AFTER batches stay CURRENT errors.

2. Repair aggregation (get_batch_llm_health): a ``schema_repaired`` row is a
   SUCCESS (llm_symbols_success +1) WITH ``llm_repair_count`` +1 — never a
   failure, never a retry. Counts stay conserved: 10 symbols, 8 plain ok + 2
   repaired -> success=10, repair=2, failed=0, provider_calls=10,
   coverage=1.0.

3. Compact ``llm_error`` for schema hard failures: Feishu recent-failure
   renders ``llm_error[:100]`` — it must carry the compact field path + type
   error ONLY (e.g. ``trade_plan/take_profits: 'take_profits' is not of type
   'array'``), never the multi-line jsonschema traceback
   (``Failed validating ...``). The full traceback stays in the new
   ``llm_error_detail`` audit field (raw_decision_json §8 envelope).

4. No risk-gate bypass: the compact-error hard failure keeps the existing
   fail-closed envelope (llm_status=failed, llm_error_category=
   llm_schema_validation_failed, llm_fallback_reason=schema_validation_failed,
   candidate=None).

07-31 final review extensions (same file, RED-first):

- P1-3: the marker cutoff must compare the batch RUNTIME/outcome timeline
  (COALESCE(started_at, finished_at, created_at)) against the marker
  applied_at — never ``analysis_time`` (a market-data snapshot that can
  disagree with the runtime clock). Unparseable or NULL runtime timestamps
  fail closed: the finding stays a current error, never silently archived.
- P2-1: ``_check_llm_failure_rate_high`` must evaluate the recent_10_* family
  by FIELD PRESENCE (a present 0 is data, not "missing"). A present family
  governs exclusively — no whole-batch fallback, never labelled legacy; the
  legacy whole-batch fallback runs only when the fields are genuinely absent.
  The breaker-driving rate (recent_10_failure_rate) and the overall LLM
  outcome rate (whole_batch_failure_rate) are separately named with an
  explicit rate_source.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.diagnostics.report_diagnostics import (
    LLM_CIRCUIT_BREAKER_OPEN,
    LLM_FAILURE_RATE_HIGH,
    LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,
    LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING,
    _apply_llm_schema_breaker_preset_integrity_marker_cutoff,
    diagnose_report_accuracy,
)
from plugins.crypto_guard.ga_master.controller import GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import (
    controller_decision_from_legacy,
)
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _run_single_llm_attempt,
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
    run_agent_sop_decision,
)
from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker

_ANALYSIS_TIME_UTC = 1785487499999

# Post-fix breaker-adjacent llm_health shapes that MUST fire the two LLM
# diagnostics when seen on a marker-AFTER batch: >= 50% failure rate over the
# latest 10 calls + breaker_state=open (production evidence #4 shape, minus
# the pre-fix pollution that P0-2 eliminates).
_LLM_HEALTH_FAILURE_RATE = {
    "total_attempts": 13,
    "successful": 8,
    "failed": 5,
    "recent_10_calls": 10,
    "recent_10_failed": 10,
    "recent_10_failure_rate": 1.0,
    "dominant_error_category": "llm_transport_error",
    "breaker_state": "closed",
}
_LLM_HEALTH_BREAKER_OPEN = {
    "total_attempts": 13,
    "successful": 8,
    "failed": 5,
    "recent_10_calls": 10,
    "recent_10_failed": 10,
    "recent_10_failure_rate": 1.0,
    "dominant_error_category": "llm_transport_error",
    "breaker_state": "open",
}


# ── DB helpers ─────────────────────────────────────────────────────────────


def _marker_applied_at(conn) -> str:
    """Read the P1-4 marker applied_at seeded by initialize_database into the
    scratch schema. Raises if absent — the marker MUST be present after init
    (EXPECTED_MARKERS asserts this in test_pg_migrations.py)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key = %s LIMIT 1",
            (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "GREEN: initialize_database must seed "
        f"{LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY!r} (P1-4)"
    )
    return str(row["applied_at"])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _seed_llm_health_batch(
    repo, *, batch_id: str, at_ms: int, llm_health: dict,
    started_at_ms: int | None = None,
    finished_at_ms: int | None = None,
    created_at_ms: int | None = None,
) -> None:
    """Insert one ``analysis_batches`` row carrying the given
    ``summary_json.llm_health`` at ``analysis_time``. status='completed'
    (NOT 'success') so the fair-scheduling / coverage / healthy-claim checks
    that only scan ``status='success'`` batches never fire on this fixture —
    the ONLY checks that read it are the two P1-4-scoped LLM diagnostics.

    The runtime timestamps (started_at / finished_at / created_at) default
    to the insert time; pass them explicitly to split ``analysis_time`` from
    the runtime/outcome timeline (07-31 final review P1-3: the deployment
    cutoff must use COALESCE(started_at, finished_at, created_at), never
    analysis_time)."""
    with repo.conn.transaction():
        with repo.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_batches(
                    batch_id, primary_interval, analysis_time,
                    started_at, finished_at, created_at, status, summary_json
                ) VALUES (%s, '15m', %s, %s, %s, %s, 'completed', %s)
                """,
                (batch_id, int(at_ms),
                 _iso_ms(started_at_ms) if started_at_ms is not None else None,
                 _iso_ms(finished_at_ms) if finished_at_ms is not None else None,
                 _iso_ms(created_at_ms) if created_at_ms is not None else None,
                 json.dumps({"llm_health": llm_health}, ensure_ascii=False)),
            )


def _seed_decision_row(
    repo, *, symbol: str, at_ms: int, llm_status: str,
    llm_terminal_reason: str | None, provider_call_count: int,
    batch_id: str | None = None,
) -> int:
    """Insert one ga_decisions row carrying the §8 envelope top-level keys
    (stored inside raw_decision_json) for the get_batch_llm_health aggregation
    contract.

    ``batch_id`` must be set to link the row to its analysis batch —
    ``list_ga_decisions_for_batch`` filters on the batch_id column, and the
    production controller always persists decisions with the batch linkage
    (hourly-report-accuracy contract)."""
    decision = {
        "symbol": symbol,
        "analysis_time": at_ms,
        "analysis_time_utc": _iso(datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)),
        "decision_type": "scheduled_analysis",
        "signal_grade": "B",
        "confidence": 0.5,
        "market_bias": "neutral",
        "trend_stage": "range",
        "decision": "monitor_only",
        "skill_result_refs": {"trend": 1},
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "rendered_summary": "canonical",
        "batch_id": batch_id,
        "llm_status": llm_status,          # §8 envelope, inside raw_decision_json
        "llm_terminal_reason": llm_terminal_reason,
        "llm_provider_call_count": provider_call_count,
        "llm_attempt_count": 1,
        "llm_error": None,
        "llm_fallback_reason": None,
    }
    return repo.create_ga_decision(decision)


def _snapshot() -> dict:
    at = _ANALYSIS_TIME_UTC
    health = {
        tf: {"ready": True, "last_close_time": at - 60_000}
        for tf in ("1d", "4h", "1h", "15m")
    }
    profiles = {
        tf: {"market_structure": "bullish", "momentum": "bullish"}
        for tf in ("1d", "4h", "1h", "15m")
    }
    return {
        "symbol": "SOLUSDT",
        "analysis_time_utc": at,
        "profiles": profiles,
        "modules": {"momentum": {"direction": "bullish"}},
        "data_quality": {"health_by_tf": health},
    }


def _raw_with_take_profits_dict() -> str:
    payload = {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": "trade_plan_available",
        "signal_grade": "A",
        "market_bias": "bullish",
        "trend_stage": "early",
        "confidence": 0.82,
        "summary": "突破.",
        "evidence": ["1H 反弹"],
        "counter_evidence": ["1D 仍下行"],
        "has_trade_plan": True,
        "trade_plan": {
            "side": "LONG",
            "entry_type": "limit",
            "entry_price": 180.0,
            "stop_loss": 172.0,
            # NOT a list -> P1-2 repair leaves it unchanged -> hard schema fail.
            "take_profits": {"price": 196.0, "ratio": 1.0},
            "invalid_condition": "1H 跌破 170",
        },
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
    }
    return json.dumps(payload, ensure_ascii=False)


def _run_single_attempt(raw_json: str) -> tuple[dict | None, dict]:
    fallback = run_agent_sop_decision(_snapshot(), use_llm=False)
    with mock.patch(
        "plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
        return_value=raw_json,
    ):
        return _run_single_llm_attempt(
            snapshot=_snapshot(),
            fallback=fallback,
            context=None,
            attempt=1,
            max_attempts=1,
            breaker=_NullBreaker(),
            cfg_name="test_cfg",
            model_name="test-model",
            prompt_builders=(
                build_llm_decision_prompt,
                build_llm_strict_json_prompt,
                build_llm_minimal_safe_prompt,
            ),
            last_category=None,
            budget_violation_is_skip=True,
            provider_timeout_seconds=None,
            subprocess_hard_timeout=False,
            deadline=None,
        )


# ── marker contract (P1-4 #1) ──────────────────────────────────────────────


class TestPgSchemaBreakerPresetIntegrityMarkerP1_4:
    """P1-4 #1: the ``llm_schema_breaker_preset_integrity_v1`` marker is
    registered by ``initialize_database`` (release path) and gates the
    current-vs-historical split of the two LLM diagnostics. Marker absence is
    fail-closed (error + checks skipped); marker-before batches are archived
    as legacy_info; marker-after batches stay current errors."""

    def test_marker_key_constant_and_registration(self) -> None:
        """RED→GREEN: the marker key constant exists and the marker is written
        by ``initialize_database`` into the scratch schema exactly once. Lock-
        step with ``EXPECTED_MARKERS`` in test_pg_migrations.py."""
        handle = make_repo()
        try:
            conn = handle.conn
            assert LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY == (
                "llm_schema_breaker_preset_integrity_v1"
            ), (
                "GREEN: marker key constant must be "
                "'llm_schema_breaker_preset_integrity_v1' (P1-4)"
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, applied_at FROM _migration_state WHERE key = %s",
                    (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1, (
                "GREEN: initialize_database must register the marker exactly "
                f"once; got {len(rows)} rows (P1-4 + migrations.py)"
            )
            assert rows[0]["applied_at"], (
                "GREEN: the registered marker must carry a non-null applied_at"
            )
        finally:
            handle.close()

    def test_marker_after_batches_are_current_errors(self) -> None:
        """RED→GREEN: a batch whose runtime timeline is AT OR AFTER the marker
        applied_at surfaces BOTH diagnostics as CURRENT errors (severity=error,
        gate False) — the post-deployment breach is real and must fail the
        gate. Runtime timestamps are passed explicitly (07-31 final review
        P1-3: the cutoff compares COALESCE(started_at, finished_at,
        created_at), NOT analysis_time — analysis_time is only a market-data
        snapshot)."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            after_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) + timedelta(hours=1)
            _seed_llm_health_batch(
                repo, batch_id="p14-after-rate", at_ms=_ms(after_dt),
                llm_health=_LLM_HEALTH_FAILURE_RATE,
                started_at_ms=_ms(after_dt),
                finished_at_ms=_ms(after_dt),
                created_at_ms=_ms(after_dt),
            )
            _seed_llm_health_batch(
                repo, batch_id="p14-after-breaker", at_ms=_ms(after_dt),
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
                started_at_ms=_ms(after_dt),
                finished_at_ms=_ms(after_dt),
                created_at_ms=_ms(after_dt),
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
            assert len(rate) >= 1, (
                "GREEN: marker-AFTER high-failure-rate batch must be a current "
                "llm_failure_rate_high issue (P1-4)"
            )
            assert rate[0]["severity"] == "error", (
                f"GREEN: marker-AFTER severity must be error, got "
                f"{rate[0]['severity']!r}"
            )
            assert len(brk) >= 1, (
                "GREEN: marker-AFTER breaker-open batch must be a current "
                "llm_circuit_breaker_open issue (P1-4)"
            )
            assert brk[0]["severity"] == "error"
            assert result["ok"] is False, (
                "GREEN: current LLM errors must fail the gate"
            )
            assert result["error_count"] >= 2
        finally:
            handle.close()

    def test_marker_before_batches_are_legacy_audit_not_current(self) -> None:
        """RED→GREEN: pre-deployment historical batches (runtime timeline
        BEFORE the marker) must be archived as legacy_info — NEVER current
        errors (symptom #4: the pre-fix batch 15m:1785487499999 must not
        repeat every hour). error_count stays 0, ok stays True,
        legacy_info_count >= 2. Runtime timestamps are explicit so the P1-3
        cutoff (COALESCE(started_at, finished_at, created_at)) demotes them.
        Pre-fix (no cutoff) both fire as errors -> RED."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            # Comfortably before the marker yet inside the 24h diagnostic
            # window (so the checks DO see the batch and the cutoff must
            # demote it — a silently-dropped batch would be a different bug).
            before_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) - timedelta(hours=6)
            _seed_llm_health_batch(
                repo, batch_id="p14-before-rate", at_ms=_ms(before_dt),
                llm_health=_LLM_HEALTH_FAILURE_RATE,
                started_at_ms=_ms(before_dt),
                finished_at_ms=_ms(before_dt),
                created_at_ms=_ms(before_dt),
            )
            _seed_llm_health_batch(
                repo, batch_id="p14-before-breaker", at_ms=_ms(before_dt),
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
                started_at_ms=_ms(before_dt),
                finished_at_ms=_ms(before_dt),
                created_at_ms=_ms(before_dt),
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
            assert len(rate) >= 1, (
                "GREEN: the marker-BEFORE batch is still found and archived "
                "(demoted, not dropped) as llm_failure_rate_high legacy_info"
            )
            assert rate[0]["severity"] == "legacy_info", (
                f"GREEN: marker-BEFORE severity must be legacy_info, got "
                f"{rate[0]['severity']!r} (P1-4, symptom #4 — no hourly repeat)"
            )
            assert rate[0]["layer"] == "legacy_audit"
            assert len(brk) >= 1
            assert brk[0]["severity"] == "legacy_info"
            # legacy_info does NOT fail the gate.
            assert result["ok"] is True, (
                "GREEN: historical audit must not fail the gate"
            )
            assert result["error_count"] == 0, (
                "GREEN: marker-BEFORE batches must not add to error_count"
            )
            assert result["legacy_info_count"] >= 2
            current_types = {
                i["type"] for i in result["issues"] if i.get("severity") == "error"
            }
            assert LLM_FAILURE_RATE_HIGH not in current_types, (
                "GREEN: marker-BEFORE batch must not appear as a current error "
                "(symptom #4 — historical not counted as current)"
            )
            assert LLM_CIRCUIT_BREAKER_OPEN not in current_types
        finally:
            handle.close()

    def test_marker_missing_is_fail_closed_and_checks_skipped(self) -> None:
        """RED→GREEN: when the marker is absent from ``_migration_state``:
        (a) ``_check_llm_schema_breaker_preset_integrity_marker_missing``
        emits a fail-closed error (gate False); (b) the two LLM diagnostics
        are SKIPPED — no llm_failure_rate_high / llm_circuit_breaker_open
        issues at all (an undeployed contract must not be evaluated as
        current). We DELETE the marker row in the scratch schema, run, then
        RESTORE it (test-only; no production mutation)."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            after_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) + timedelta(hours=1)
            _seed_llm_health_batch(
                repo, batch_id="p14-missing-rate", at_ms=_ms(after_dt),
                llm_health=_LLM_HEALTH_FAILURE_RATE,
            )
            _seed_llm_health_batch(
                repo, batch_id="p14-missing-breaker", at_ms=_ms(after_dt),
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
            )
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM _migration_state WHERE key = %s "
                        "RETURNING applied_at",
                        (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,),
                    )
                    deleted = cur.fetchone()
            assert deleted is not None, "GREEN: marker row existed before deletion"
            try:
                result = diagnose_report_accuracy(repo)
                missing = [
                    i for i in result["issues"]
                    if i["type"] == LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_MISSING
                ]
                assert len(missing) >= 1, (
                    "GREEN: marker absence must emit a fail-closed "
                    "llm_schema_breaker_preset_integrity_marker_missing error "
                    "(P1-4)"
                )
                assert missing[0]["severity"] == "error"
                assert missing[0]["details"].get("marker_key") == (
                    LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY
                )
                # The two LLM diagnostics must be SKIPPED entirely.
                rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
                brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
                assert rate == [], (
                    "GREEN: marker missing -> llm_failure_rate_high check "
                    "skipped, no issues (P1-4, no silent green / no false "
                    "current errors)"
                )
                assert brk == [], (
                    "GREEN: marker missing -> llm_circuit_breaker_open check "
                    "skipped, no issues (P1-4)"
                )
                # Fail-closed: the gate goes False on the marker-missing error.
                assert result["ok"] is False
                assert result["error_count"] >= 1
            finally:
                # RESTORE the marker (scratch schema only).
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO _migration_state(key, applied_at) "
                            "VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                            (LLM_SCHEMA_BREAKER_PRESET_INTEGRITY_MARKER_KEY,
                             deleted["applied_at"]),
                        )
        finally:
            handle.close()


# ── 07-31 final review P1-3: cutoff uses the RUNTIME timeline ──────────────


class TestMarkerCutoffUsesRuntimeTimestampNotAnalysisTime:
    """07-31 final review P1-3: the deployment cutoff must compare the batch
    RUNTIME/outcome timeline — COALESCE(started_at, finished_at, created_at)
    — against the marker applied_at. ``analysis_time`` is ONLY a market-data
    snapshot (it can disagree with the runtime clock by hours) and must never
    drive the current-vs-historical split. RED pre-fix:
    ``_apply_llm_schema_breaker_preset_integrity_marker_cutoff`` reads
    ``details['analysis_time']`` (report_diagnostics.py:721)."""

    def test_analysis_time_before_marker_but_runtime_after_stays_current(self) -> None:
        """P1-3 ①: analysis_time BEFORE the marker but runtime AFTER it must
        stay a CURRENT error — the batch ran post-deployment. RED pre-fix:
        demoted to legacy_info via details['analysis_time']."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            marker_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            at_ms = _ms(marker_dt - timedelta(hours=6))
            runtime_ms = _ms(marker_dt + timedelta(hours=1))
            _seed_llm_health_batch(
                repo, batch_id="p13-runtime-after-rate", at_ms=at_ms,
                llm_health=_LLM_HEALTH_FAILURE_RATE,
                started_at_ms=runtime_ms, finished_at_ms=runtime_ms,
                created_at_ms=runtime_ms,
            )
            _seed_llm_health_batch(
                repo, batch_id="p13-runtime-after-breaker", at_ms=at_ms,
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
                started_at_ms=runtime_ms, finished_at_ms=runtime_ms,
                created_at_ms=runtime_ms,
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
            assert len(rate) >= 1 and len(brk) >= 1, (
                "GREEN: both diagnostics must fire on the marker-after "
                "runtime batch (P1-3 ①)"
            )
            for issue in (rate[0], brk[0]):
                assert issue["severity"] == "error", (
                    "GREEN: runtime AFTER marker must stay a CURRENT error, "
                    f"got {issue['severity']!r} (P1-3 ① — pre-fix demotes via "
                    "details['analysis_time'])"
                )
                details = issue["details"]
                assert details.get("runtime_timestamp_ms") == runtime_ms, (
                    "GREEN: details must carry the named runtime/outcome "
                    f"timestamp (int ms); got "
                    f"{details.get('runtime_timestamp_ms')!r} (P1-3)"
                )
                assert details.get("analysis_time") == at_ms, (
                    "GREEN: analysis_time stays as the market-data snapshot, "
                    "never the cutoff basis (P1-3)"
                )
            assert result["ok"] is False
        finally:
            handle.close()

    def test_runtime_before_marker_demotes_even_if_analysis_time_after(self) -> None:
        """P1-3 ②: runtime BEFORE the marker must demote to legacy_info even
        when analysis_time is AFTER the marker. RED pre-fix: kept as a
        current error via details['analysis_time']."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            marker_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            at_ms = _ms(marker_dt + timedelta(hours=1))
            runtime_ms = _ms(marker_dt - timedelta(hours=6))
            _seed_llm_health_batch(
                repo, batch_id="p13-runtime-before-rate", at_ms=at_ms,
                llm_health=_LLM_HEALTH_FAILURE_RATE,
                started_at_ms=runtime_ms, finished_at_ms=runtime_ms,
                created_at_ms=runtime_ms,
            )
            _seed_llm_health_batch(
                repo, batch_id="p13-runtime-before-breaker", at_ms=at_ms,
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
                started_at_ms=runtime_ms, finished_at_ms=runtime_ms,
                created_at_ms=runtime_ms,
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
            assert len(rate) >= 1 and len(brk) >= 1, (
                "GREEN: both diagnostics must still fire (demoted, not "
                "dropped) on the marker-before runtime batch (P1-3 ②)"
            )
            for issue in (rate[0], brk[0]):
                assert issue["severity"] == "legacy_info", (
                    "GREEN: runtime BEFORE marker must demote to legacy_info "
                    f"even with analysis_time after it; got {issue['severity']!r} "
                    "(P1-3 ② — pre-fix keeps it as a current error)"
                )
            assert result["ok"] is True
            assert result["error_count"] == 0
        finally:
            handle.close()

    def test_unparseable_runtime_timestamp_is_fail_closed_not_archived(self) -> None:
        """P1-3 ④: an unparseable runtime timestamp must NOT silently archive
        the finding. The cutoff is driven directly with an issue whose
        runtime_timestamp_ms is garbage while analysis_time (the pre-fix
        basis) is BEFORE the marker — the cutoff must fail closed and KEEP
        the current error. (Integration note: ``'infinity'::timestamptz``
        cannot even be read back through psycopg — it raises DataError at
        fetchall — so the unparseable-value contract is asserted at the
        cutoff unit itself, where the parse failure actually lives.) RED
        pre-fix: demoted to legacy_info via details['analysis_time']."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            marker_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            issue = {
                "type": LLM_FAILURE_RATE_HIGH,
                "severity": "error",
                "details": {
                    "batch_id": "p13-garbage-runtime",
                    "analysis_time": _ms(marker_dt - timedelta(hours=6)),
                    "runtime_timestamp_ms": "not-a-timestamp",
                },
                "message": "probe",
            }
            _apply_llm_schema_breaker_preset_integrity_marker_cutoff(repo, [issue])
            assert issue["severity"] == "error", (
                "GREEN: an unparseable runtime_timestamp_ms must NOT archive "
                f"the finding (fail-closed); got {issue['severity']!r} (P1-3 ④)"
            )
        finally:
            handle.close()

    def test_started_at_before_marker_governs_over_finished_at_after(self) -> None:
        """P1-3 (08-01 reviewer P2-1 regression): the COALESCE precedence is
        ``started_at`` first — a batch that STARTED before the deployment
        marker but FINISHED after it must be demoted to legacy_info (pre-fix
        data must not re-enter the hourly report even if the run spilled past
        the marker). ``created_at`` is earliest here, so only the contract's
        started_at-first order can produce the demotion. RED (finished_at
        first): the batch would be treated as post-deployment and stay a
        current error."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            marker_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            at_ms = _ms(marker_dt - timedelta(hours=6))
            started_ms = _ms(marker_dt - timedelta(hours=2))
            finished_ms = _ms(marker_dt + timedelta(hours=2))
            created_ms = _ms(marker_dt - timedelta(hours=3))
            _seed_llm_health_batch(
                repo, batch_id="p13-crossing-rate", at_ms=at_ms,
                llm_health=_LLM_HEALTH_FAILURE_RATE,
                started_at_ms=started_ms, finished_at_ms=finished_ms,
                created_at_ms=created_ms,
            )
            _seed_llm_health_batch(
                repo, batch_id="p13-crossing-breaker", at_ms=at_ms,
                llm_health=_LLM_HEALTH_BREAKER_OPEN,
                started_at_ms=started_ms, finished_at_ms=finished_ms,
                created_at_ms=created_ms,
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            brk = [i for i in result["issues"] if i["type"] == LLM_CIRCUIT_BREAKER_OPEN]
            assert len(rate) >= 1 and len(brk) >= 1, (
                "GREEN: both diagnostics must still fire (demoted, not "
                "dropped) on the crossing batch (P1-3)"
            )
            for issue in (rate[0], brk[0]):
                assert issue["severity"] == "legacy_info", (
                    "GREEN: started_at BEFORE marker must demote even when "
                    "finished_at is AFTER it (started_at-first COALESCE); got "
                    f"{issue['severity']!r} (P1-3 — RED with finished_at first)"
                )
                details = issue["details"]
                assert details.get("runtime_timestamp_ms") == started_ms, (
                    "GREEN: runtime_timestamp_ms must be the started_at value "
                    f"(contract precedence); got {details.get('runtime_timestamp_ms')!r} "
                    "(P1-3)"
                )
            assert result["ok"] is True
            assert result["error_count"] == 0
        finally:
            handle.close()

    def test_null_runtime_timestamps_are_fail_closed_not_archived(self) -> None:
        """P1-3 (companion): ALL THREE runtime columns NULL means no runtime
        evidence at all — the finding must stay a CURRENT error (never
        archived on a guess). RED pre-fix: demoted via details['analysis_time']
        (before the marker)."""
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            cutoff = _marker_applied_at(conn)
            marker_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            at_ms = _ms(marker_dt - timedelta(hours=6))
            _seed_llm_health_batch(
                repo, batch_id="p13-null-runtime", at_ms=at_ms,
                llm_health=_LLM_HEALTH_FAILURE_RATE,
            )
            result = diagnose_report_accuracy(repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            assert len(rate) >= 1, "GREEN: the diagnostic must still fire"
            assert rate[0]["severity"] == "error", (
                "GREEN: NULL runtime timestamps must NOT archive the finding "
                f"(fail-closed); got {rate[0]['severity']!r} (P1-3)"
            )
            assert rate[0]["details"].get("runtime_timestamp_ms") is None, (
                "GREEN: no runtime evidence -> runtime_timestamp_ms is None "
                "(P1-3)"
            )
            assert result["ok"] is False
        finally:
            handle.close()


# ── 07-31 final review P2-1: recent_10 presence contract ───────────────────


class TestFailureRateRecent10PresenceContract:
    """07-31 final review P2-1: the recent_10_* family is the breaker-driving
    failure rate and must be evaluated by FIELD PRESENCE, never by the value
    0 masquerading as "missing":

    - recent_10_calls / recent_10_failure_rate PRESENT (any value, incl. 0)
      -> the recent-10 path governs exclusively; the whole-batch fallback is
      NOT allowed (a healthy recent window is a current fact, not a legacy
      batch). No issue when the recent window is healthy or has < 3 samples.
    - family genuinely MISSING (legacy pre-Phase-I shapes) AND total >= 10
      -> whole-batch fallback allowed, explicitly labelled legacy.
    - the two rates are separately named (recent_10_failure_rate vs
      whole_batch_failure_rate) with an explicit rate_source; the ambiguous
      single ``failure_rate`` key is gone.

    RED pre-fix: ``_check_llm_failure_rate_high`` reads
    ``int(health.get('recent_10_calls') or 0)`` and falls through to the
    whole-batch path with window "legacy" even when the recent_10 family is
    present (report_diagnostics.py:2977-2992)."""

    def _seed(self, repo, *, batch_id: str, llm_health: dict) -> None:
        cutoff = _marker_applied_at(repo.conn)
        after_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) + timedelta(hours=1)
        _seed_llm_health_batch(
            repo, batch_id=batch_id, at_ms=_ms(after_dt),
            llm_health=llm_health,
            started_at_ms=_ms(after_dt), finished_at_ms=_ms(after_dt),
            created_at_ms=_ms(after_dt),
        )

    def test_recent_10_present_and_healthy_governs_no_issue(self) -> None:
        """P2-1: recent_10 present & healthy (rate 0.0 over 10 calls) while
        the WHOLE-batch rate is 75% (20 attempts / 15 failed) -> NO issue.
        The healthy recent window is a current fact; the whole-batch fallback
        must not fire and must not be labelled legacy. RED pre-fix: falls
        through to whole-batch -> fires an error."""
        handle = make_repo()
        try:
            self._seed(handle.repo, batch_id="p21-present-healthy",
                       llm_health={
                           "total_attempts": 20, "successful": 5, "failed": 15,
                           "recent_10_calls": 10, "recent_10_failed": 0,
                           "recent_10_failure_rate": 0.0,
                           "dominant_error_category": "llm_transport_error",
                           "breaker_state": "closed",
                       })
            result = diagnose_report_accuracy(handle.repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            assert rate == [], (
                "GREEN: a present + healthy recent_10 family must NOT fall "
                f"back to the whole-batch path; got {rate!r} (P2-1)"
            )
        finally:
            handle.close()

    def test_recent_10_calls_zero_present_is_not_missing(self) -> None:
        """P2-1: recent_10_calls=0 with the field PRESENT is data, not
        absence — the recent-10 path governs (0 samples < 3 -> not enough
        evidence -> no issue), and the whole-batch legacy fallback must NOT
        fire. RED pre-fix: ``int(... or 0)`` collapses present-0 with missing
        -> falls through to whole-batch -> fires an error labelled legacy."""
        handle = make_repo()
        try:
            self._seed(handle.repo, batch_id="p21-present-zero",
                       llm_health={
                           "total_attempts": 20, "successful": 5, "failed": 15,
                           "recent_10_calls": 0, "recent_10_failed": 0,
                           "recent_10_failure_rate": 0.0,
                           "dominant_error_category": "llm_transport_error",
                           "breaker_state": "closed",
                       })
            result = diagnose_report_accuracy(handle.repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            assert rate == [], (
                "GREEN: recent_10_calls=0 with the field present is NOT "
                f"missing; got {rate!r} (P2-1)"
            )
        finally:
            handle.close()

    def test_missing_recent_10_family_uses_whole_batch_fallback_explicitly(self) -> None:
        """P2-1: the recent_10 family genuinely MISSING (legacy shape) with
        total=12 / failed=8 -> whole-batch fallback fires, rate 0.667, named
        whole_batch_failure_rate, rate_source=whole_batch, window carries the
        legacy label; recent_10_failure_rate and the old ambiguous
        ``failure_rate`` key are ABSENT. RED pre-fix: no rate_source key, no
        whole_batch_failure_rate, ambiguous failure_rate present."""
        handle = make_repo()
        try:
            self._seed(handle.repo, batch_id="p21-missing-family",
                       llm_health={
                           "total_attempts": 12, "successful": 4, "failed": 8,
                           "dominant_error_category": "llm_transport_error",
                           "breaker_state": "closed",
                       })
            result = diagnose_report_accuracy(handle.repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            assert len(rate) == 1, (
                "GREEN: genuinely-missing recent_10 family with total>=10 "
                f"must use the whole-batch fallback; got {rate!r} (P2-1)"
            )
            details = rate[0]["details"]
            assert details.get("rate_source") == "whole_batch", (
                "GREEN: rate_source must be 'whole_batch' for the fallback; "
                f"got {details.get('rate_source')!r} (P2-1)"
            )
            assert details.get("whole_batch_failure_rate") == round(8 / 12, 3), (
                "GREEN: whole_batch_failure_rate must carry the overall LLM "
                f"outcome rate; got {details.get('whole_batch_failure_rate')!r} "
                "(P2-1)"
            )
            assert "recent_10_failure_rate" not in details, (
                "GREEN: the breaker-driving recent_10 rate must not appear on "
                "the whole-batch fallback (P2-1 separate naming)"
            )
            assert "failure_rate" not in details, (
                "GREEN: the ambiguous single failure_rate key must be gone "
                "(P2-1)"
            )
            assert "legacy" in details.get("window", ""), (
                "GREEN: the whole-batch fallback window keeps its explicit "
                f"legacy label; got {details.get('window')!r} (P2-1)"
            )
            assert rate[0]["severity"] == "error"
        finally:
            handle.close()

    def test_recent_10_present_drives_rate_and_whole_batch_is_separate(self) -> None:
        """P2-1: the recent_10 family present & bad (10/10 failed, rate 1.0)
        while the whole-batch rate is only 5/13 = 0.385 -> fires with
        rate_source=recent_10 and recent_10_failure_rate=1.0 (the
        breaker-driving rate), NEVER the whole-batch rate, and no legacy
        label. RED pre-fix: ambiguous failure_rate=1.0, no rate_source, no
        recent_10_failure_rate key."""
        handle = make_repo()
        try:
            self._seed(handle.repo, batch_id="p21-present-driving",
                       llm_health=_LLM_HEALTH_FAILURE_RATE)
            result = diagnose_report_accuracy(handle.repo)
            rate = [i for i in result["issues"] if i["type"] == LLM_FAILURE_RATE_HIGH]
            assert len(rate) == 1, (
                "GREEN: a present + bad recent_10 family must fire exactly "
                f"once; got {rate!r} (P2-1)"
            )
            details = rate[0]["details"]
            assert details.get("rate_source") == "recent_10", (
                "GREEN: rate_source must be 'recent_10' when the family is "
                f"present; got {details.get('rate_source')!r} (P2-1)"
            )
            assert details.get("recent_10_failure_rate") == 1.0, (
                "GREEN: recent_10_failure_rate must be the breaker-driving "
                f"rate (1.0), not the whole-batch 5/13; got "
                f"{details.get('recent_10_failure_rate')!r} (P2-1)"
            )
            assert "whole_batch_failure_rate" not in details, (
                "GREEN: the whole-batch rate must not appear when the "
                "recent_10 family governs (P2-1 separate naming)"
            )
            assert "failure_rate" not in details, (
                "GREEN: the ambiguous single failure_rate key must be gone "
                "(P2-1)"
            )
            assert "legacy" not in details.get("window", ""), (
                "GREEN: a present recent_10 family must NOT be labelled "
                f"legacy; got {details.get('window')!r} (P2-1)"
            )
            assert rate[0]["severity"] == "error"
        finally:
            handle.close()


# ── repair aggregation contract (P1-4 #2) ──────────────────────────────────


class TestRepairedRowsAggregateAsSuccessWithRepairCount:
    """P1-4 #2: a schema-repaired row is a SUCCESS with the repair count
    incremented — never a failure, never a retry. This is the reporting
    symptom behind evidence #3 (8 coordinator successes were destroyed into
    10 breaker_skipped rows): post-fix, repaired successes must land in
    llm_symbols_success, and the repair must surface in llm_repair_count."""

    def test_ten_symbols_eight_ok_two_repaired_aggregate_conserved(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            batch_id = "p14-agg-15m"
            symbols = [f"P14SYM{i:02d}" for i in range(10)]
            at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            repo.start_analysis_batch(
                batch_id=batch_id, primary_interval="15m",
                analysis_time=at_ms, enabled_symbols=symbols,
            )
            for i, sym in enumerate(symbols):
                # 8 plain successes + 2 schema_repaired successes — ALL ok.
                terminal = "schema_repaired" if i >= 8 else None
                _seed_decision_row(
                    repo, symbol=sym, at_ms=at_ms,
                    llm_status="ok", llm_terminal_reason=terminal,
                    provider_call_count=1, batch_id=batch_id,
                )
            health = GAMasterController(repo).get_batch_llm_health(batch_id)
            assert health["expected_symbols"] == 10
            assert health["llm_symbols_success"] == 10, (
                "GREEN: repaired rows count as SUCCESS (llm_symbols_success "
                f"=10); got {health['llm_symbols_success']} (P1-4, evidence #3 — "
                "coordinator successes must not be destroyed)"
            )
            assert health["llm_repair_count"] == 2, (
                f"GREEN: llm_repair_count must be 2; got "
                f"{health['llm_repair_count']}"
            )
            assert health["llm_symbols_failed"] == 0, (
                "GREEN: repaired rows must NOT count as failures; got "
                f"{health['llm_symbols_failed']}"
            )
            assert health["llm_physical_provider_calls"] == 10, (
                "GREEN: one physical provider call per symbol; got "
                f"{health['llm_physical_provider_calls']}"
            )
            assert health["llm_symbols_attempted"] == 10
            assert health["llm_first_attempt_coverage"] == 1.0
            assert health["llm_coverage_degraded"] is False
            assert health["llm_breaker_skip_count"] == 0, (
                "GREEN: no breaker skips on the post-fix batch (P0-1/P0-2)"
            )
            assert health["llm_policy_skip_count"] == 0
            assert health["llm_retry_calls"] == 0, (
                "GREEN: a repaired success is NOT a retry (llm_attempt_count "
                "stays 1)"
            )
        finally:
            handle.close()


# ── compact error / full detail contract (P1-4 #3) ─────────────────────────


class TestSchemaHardFailureCompactErrorDetail:
    """P1-4 #3: a non-repairable schema failure (take_profits as a bare dict —
    P1-2 leaves it untouched, fail-closed) must render a COMPACT ``llm_error``
    (field path + type error only, fits the Feishu ``llm_error[:100]``
    display slice) while the FULL jsonschema traceback is preserved in the new
    ``llm_error_detail`` audit field. Pre-fix llm_error carries the whole
    multi-line traceback (RED)."""

    def test_compact_error_and_full_detail_on_hard_schema_failure(self) -> None:
        candidate, meta = _run_single_attempt(_raw_with_take_profits_dict())
        assert candidate is None, (
            "take_profits as a non-list must stay a hard schema failure "
            f"(fail-closed, no risk-gate bypass); meta={meta}"
        )
        assert meta.get("llm_terminal_reason") == "llm_schema_validation_failed"
        assert meta.get("llm_error_category") == "llm_schema_validation_failed"
        assert meta.get("llm_error_stage") == "schema"
        assert meta.get("llm_fallback_reason") == "schema_validation_failed"
        assert meta.get("llm_repair_event") is not True

        err = meta.get("llm_error") or ""
        assert err, "llm_error must be non-empty"
        assert "take_profits" in err, (
            f"compact llm_error must carry the failing field path; got {err!r}"
        )
        assert "is not of type" in err, (
            f"compact llm_error must carry the jsonschema type message; got {err!r}"
        )
        assert "Failed validating" not in err, (
            "llm_error must be COMPACT (field path + type only) — the "
            f"multi-line jsonschema traceback must NOT pollute the Feishu "
            f"summary body; got: {err!r}"
        )
        assert len(err) <= 100, (
            "llm_error must fit the Feishu recent-failure display slice "
            f"llm_error[:100]; got {len(err)} chars"
        )

        detail = meta.get("llm_error_detail") or ""
        assert detail, "llm_error_detail must preserve the full traceback"
        assert "Failed validating" in detail, (
            "llm_error_detail must carry the full jsonschema traceback; got "
            f"{detail!r}"
        )
        assert "take_profits" in detail
        assert len(err) < len(detail), (
            "compact llm_error must be strictly shorter than the full "
            "llm_error_detail traceback"
        )

    def test_persisted_row_surfaces_llm_error_detail_top_level(self) -> None:
        """Reviewer P2-1 closure: the §8 adapter must surface
        ``llm_error_detail`` at the ga_decision top level (raw_decision_json)
        NEXT TO the compact ``llm_error`` — an operator reading the persisted
        row must see the full traceback WITHOUT descending into
        raw_legacy_decision (the standing §8 top-level contract). RED: pre-
        fix the adapter enumerates only llm_error, so the detail key survives
        only nested inside raw_legacy_decision."""
        handle = make_repo()
        try:
            repo = handle.repo
            compact = "trade_plan/take_profits: 'take_profits' is not of type 'array'"
            full = (
                "Failed validating 'type' in schema[0]['properties']['trade_plan']"
                "['properties']['take_profits']:\n"
                "{'price': 196.0, 'ratio': 1.0} is not of type 'array'"
            )
            decision = controller_decision_from_legacy(
                legacy={
                    "symbol": "SOLUSDT",
                    "decision": "monitor_only",
                    "signal_grade": "B",
                    "confidence": 0.5,
                    "summary": "schema hard failure",
                    "risk_check": {"ok": True},
                    "llm_status": "failed",
                    "llm_error": compact,
                    "llm_error_detail": full,
                    "llm_terminal_reason": "llm_schema_validation_failed",
                },
                decision_type="scheduled_analysis",
                analysis_time=_ANALYSIS_TIME_UTC,
                skill_result_refs={"trend": 1},
                feishu_actions=[],
            )
            gid = repo.create_ga_decision(decision)
            row = repo.get_ga_decision(gid)
            raw = row["raw_decision_json"]
            assert isinstance(raw, dict), (
                "GREEN: raw_decision_json must decode to a dict for the "
                "top-level §8 read (P2-1)"
            )
            assert raw.get("llm_error") == compact
            assert raw.get("llm_error_detail") == full, (
                "GREEN: llm_error_detail must reach the top level of "
                "raw_decision_json (P2-1 §8 envelope — no descent into "
                "raw_legacy_decision required)"
            )
            assert "Failed validating" in raw["llm_error_detail"]
            # The compact/full split must survive the round trip too.
            assert len(raw["llm_error"]) < len(raw["llm_error_detail"])
        finally:
            handle.close()
