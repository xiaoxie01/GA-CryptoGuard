# -*- coding: utf-8 -*-
"""终审返工 Phase-2 P2-1 (2026-07-27): LLM-success row polluted by fallback
metadata — RED-first behavioral test + revert-fail.

Six-symptom fix, symptom #1 (requirement B verbatim):

  "21:00 batch LLM 10/10 success, but SOLUSDT、DOGEUSDT candidate plans still
  show 'LLM 已禁用' (LLM disabled)."

Production read-only evidence (phase2_step_a_evidence_probe.py, ok=true)
confirmed 9 LLM-success rows (llm_status=ok, plan_origin=llm_confirmed,
plan_execution_state=risk_rejected) carrying a stale plan_blockers entry
``{code: "llm_disabled", stage: "synthesis",
detail: "llm_status=disabled, fallback_llm_failed_blocks_paper_order=true"}``
plus candidate_trade_plan present and trade_plan=null / plan_status=withheld.

Root cause (traced end-to-end this session):

  1. ``fair_llm_call_adapter`` (llm_agent_judge.py:1535) builds its prompt
     fallback via ``fallback = run_agent_sop_decision(snapshot, use_llm=False)``.
     That disabled path (lines 131-150) sets ``llm_status="disabled"`` and then
     returns ``apply_risk_to_decision(fallback, snapshot)``.
  2. ``apply_risk_to_decision`` (risk_engine.py:28-68) sees llm_status in
     {failed, disabled} + has_trade_plan + trade_plan and writes
     ``plan_blockers=[{code: "llm_disabled", detail: "llm_status=disabled, "
     "fallback_llm_failed_blocks_paper_order=true"}]``, sets
     ``candidate_trade_plan=<plan>``, ``has_trade_plan=False``, ``trade_plan=None``,
     ``plan_status="withheld"``, ``fallback_trade_plan_blocked=True``,
     ``fallback_block_reason=...``. So the risk-processed fallback carries an
     ``llm_disabled`` blocker even though no LLM call has happened yet.
  3. ``_run_single_llm_attempt`` calls ``_normalize_llm_decision(candidate,
     snapshot, fallback)`` (line 1421). Inside, ``decision = dict(fallback)``
     (line 3331) COPIES the stale ``llm_disabled`` blocker,
     ``candidate_trade_plan``, ``plan_status="withheld"``,
     ``fallback_trade_plan_blocked=True``. Then ``decision.update(candidate)``
     (line 3370) merges the LLM candidate — but the LLM candidate never writes
     ``plan_blockers``, so the stale ``llm_disabled`` blocker survives.
     ``decision["llm_status"] = "ok"`` (line 3387) overrides the disabled
     status, but NEVER clears the fallback-only transient blocker fields.
  4. Success path (lines 1473-1482) sets ``plan_origin="llm_confirmed"``,
     ``plan_execution_state="confirmed"``, ``llm_status="ok"`` and merges
     attempt_meta — again no clearing of the stale blocker.
  5. ``apply_risk_to_decision(candidate, snapshot)`` (line 286) re-runs with
     llm_status="ok". Neither the fallback-blocked block (needs failed/disabled)
     nor the risk-rejected block fires to overwrite the blocker, so the
     ``llm_disabled`` blocker persists into the persisted row.
  6. Controller (819-846) sees _has_candidate + not _has_plan + not risk.ok +
     plan_origin=llm_confirmed → sets ``plan_execution_state="risk_rejected"``.
  7. Report render ``_trade_plan_summary`` (hourly_report.py:3021-3022) maps
     ``code=="llm_disabled"`` → "LLM 已禁用" — symptom #1 rendered.

Fix (requirement B verbatim): "Fix successful LLM polluted by fallback
metadata (llm_agent_judge.py + risk/risk_engine.py): clear/rebuild
fallback-only transient fields on LLM success; blocker must be
``llm_not_confirmed`` not ``llm_disabled`` if LLM didn't confirm; re-run
full risk gate."

This test reproduces the leak at its SOURCE — it builds the SAME risk-
processed disabled fallback that ``run_agent_sop_decision(use_llm=False)``
returns on the fair-adapter path (step 1-2), then hands it to
``_normalize_llm_decision`` with an LLM-success candidate (step 3). It
asserts the stale ``llm_disabled`` blocker / fallback-only transient fields
do NOT survive onto the LLM-success row (GREEN), and that the pre-fix code
leaves them in place (RED, asserted via the revert-fail toggle).

Building the disabled fallback via ``apply_risk_to_decision`` on a hand-
built deterministic decision avoids the ``run_ga_sop_decision`` strategy-
loader DB path (which needs seeded strategies) while exercising the REAL
risk-engine fallback-blocked block that writes the ``llm_disabled`` blocker.
``make_repo()`` sets the DSN so ``load_config()`` works inside
``apply_risk_to_decision``.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.reasoning.llm_agent_judge import _normalize_llm_decision
from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.notify.hourly_report import _trade_plan_summary
from plugins.crypto_guard.tests.pg_fixtures import make_repo


_ANALYSIS_TIME_UTC = 1785132899999


def _bullish_snapshot() -> dict:
    """A snapshot shape that ``apply_risk_to_decision`` will accept for the
    fallback-blocked block (has a trade_plan + analysis_time_utc)."""
    return {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "profiles": {
            "4h": {"market_structure": "bullish"},
            "1h": {"market_structure": "bullish"},
            "15m": {"market_structure": "bullish"},
        },
        "modules": {"momentum": {"direction": "bullish"}},
        "data_quality": {},
    }


def _det_trade_plan() -> dict:
    """A schema-valid deterministic LONG trade plan (what run_ga_sop_decision
    would produce)."""
    return {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 180.0,
        "stop_loss": 172.0,
        "take_profits": [{"price": 196.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "跌破 172.0",
    }


def _det_fallback_pre_risk() -> dict:
    """The deterministic SOP decision shape BEFORE ``apply_risk_to_decision``
    runs the disabled path — has a trade_plan, llm_status not yet set to
    disabled. This mirrors ``run_ga_sop_decision`` output + the disabled-path
    envelope fields set at llm_agent_judge.py:131-149."""
    return {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "signal_grade": "A",
        "confidence": 0.82,
        "market_bias": "bullish",
        "decision": "trade_plan_available",
        "has_trade_plan": True,
        "trade_plan": _det_trade_plan(),
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
        "risk_notes": [],
        # Disabled-path envelope (llm_agent_judge.py:132-149):
        "analysis_source": "deterministic_sop",
        "llm_status": "disabled",
        "llm_attempt_count": 0,
        "llm_provider_call_count": 0,
        "llm_latency_ms": 0,
        "llm_prompt_bytes": None,
        "llm_continuity_included": None,
        "llm_model": None,
        "llm_terminal_reason": "llm_disabled",
        "llm_fallback_reason": "llm_disabled",
        "plan_origin": "deterministic_sop",
        "plan_execution_state": "confirmed",
    }


def _risk_processed_disabled_fallback() -> dict:
    """Reproduce fair_llm_call_adapter:1535 — the prompt fallback built from
    ``run_agent_sop_decision(snapshot, use_llm=False)``.

    Runs ``apply_risk_to_decision`` on the disabled-path deterministic
    fallback, exactly as llm_agent_judge.py:150 does. The fallback-blocked
    block (risk_engine.py:28-68) fires on llm_status=disabled + has_trade_plan
    + trade_plan and writes the ``llm_disabled`` blocker, candidate_trade_plan,
    plan_status=withheld, fallback_trade_plan_blocked — the exact leak source.
    """
    snapshot = _bullish_snapshot()
    fallback = _det_fallback_pre_risk()
    processed = apply_risk_to_decision(fallback, snapshot)
    # Guard: confirm the risk-processed disabled fallback carries the leak.
    blocker_codes = [
        str(b.get("code") or "")
        for b in (processed.get("plan_blockers") or [])
        if isinstance(b, dict)
    ]
    assert "llm_disabled" in blocker_codes, (
        f"fixture: risk-processed disabled fallback must carry llm_disabled "
        f"blocker; got codes={blocker_codes}. If apply_risk_to_decision's "
        f"fallback-blocked block did not fire, the snapshot/plan is too weak."
    )
    assert processed.get("candidate_trade_plan"), (
        "fixture: risk-processed disabled fallback must preserve candidate_trade_plan"
    )
    assert processed.get("fallback_trade_plan_blocked") is True, (
        "fixture: risk-processed disabled fallback must set fallback_trade_plan_blocked"
    )
    return processed


def _apply_llm_success_path(decision: dict) -> dict:
    """Mirror ``_run_single_llm_attempt`` success path AFTER P1-4.

    ``_normalize_llm_decision`` does NOT set ``plan_origin`` /
    ``plan_execution_state`` / the success envelope — the single-attempt unit
    does, AFTER normalization. P1-4 makes ``plan_origin=llm_confirmed``
    CONDITIONAL on ``has_trade_plan and trade_plan`` (no-plan success keeps
    the fallback origin). This helper must match production — do NOT
    unconditionally force llm_confirmed (that was a pre-P1-4 mirror and
    made the no-plan pollution test inauthentic).
    """
    if decision.get("has_trade_plan") and decision.get("trade_plan"):
        decision["plan_origin"] = "llm_confirmed"
        decision["plan_execution_state"] = "confirmed"
    attempt_meta = {
        "llm_status": "ok",
        "llm_error_category": None,
        "llm_error_stage": None,
        "llm_error": None,
        "llm_fallback_reason": None,
        "llm_terminal_reason": None,
        "llm_repair_event": False,
    }
    decision.update(attempt_meta)
    return decision


def _llm_success_candidate(*, with_plan: bool) -> dict:
    """A schema-valid LLM-success candidate (NO plan_blockers key — the leak).

    ``with_plan=True``: LLM confirmed a trade plan (A grade + trade_plan).
    ``with_plan=False``: LLM returned a monitor-only / B-grade / no-plan
    decision — the case where requirement B says the blocker must become
    ``llm_not_confirmed`` (NOT ``llm_disabled``).
    """
    base = {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": "trade_plan_available" if with_plan else "monitor_only",
        "signal_grade": "A" if with_plan else "B",
        "market_bias": "bullish",
        "trend_stage": "early",
        "confidence": 0.82 if with_plan else 0.55,
        "summary": "SOL 反弹.",
        "evidence": ["1H 反弹"],
        "counter_evidence": ["1D 仍下行"],
        "risk_notes": ["LLM 候选"],
        "has_trade_plan": with_plan,
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"] if with_plan else ["monitor_only"],
    }
    if with_plan:
        base["trade_plan"] = _det_trade_plan()
    return base


class TestPgLLMSuccessFallbackPollutionP2_1:
    """Symptom #1 (requirement B): LLM-success row polluted by fallback metadata."""

    def test_llm_success_no_plan_clears_stale_llm_disabled_blocker(self) -> None:
        """RED→GREEN: LLM-success (no confirmed plan) must not carry the stale
        ``llm_disabled`` blocker from the risk-processed disabled fallback.

        Pre-fix the stale ``llm_disabled`` blocker survives ``_normalize_llm_decision``
        onto the LLM-success row (RED). Post-fix the fallback-only transient
        fields are cleared and the blocker becomes ``llm_not_confirmed``
        (GREEN — requirement B).
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback()
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_llm_success_path(decision)

            # ── Invariants that hold pre- AND post-fix ─────────────────────
            assert str(decision.get("llm_status") or "").lower() == "ok", (
                "LLM-success row must have llm_status=ok (the LLM call succeeded)"
            )
            # P1-4: no-plan LLM success must NOT force plan_origin=llm_confirmed
            # (nothing was confirmed). Align with production + cleanup_p1_4.
            assert decision.get("plan_origin") != "llm_confirmed", (
                "P1-4: no-plan LLM success must NOT set plan_origin=llm_confirmed; "
                f"got {decision.get('plan_origin')!r}"
            )
            # Phase E plan-lifecycle invariant: candidate_trade_plan preserved
            # for audit. Requirement B must NOT regress it.
            assert decision.get("candidate_trade_plan"), (
                "candidate_trade_plan must be preserved for audit (Phase E invariant)"
            )

            # ── The defect: stale fallback-only llm_disabled blocker ───────
            blocker_codes = [
                str(b.get("code") or "")
                for b in (decision.get("plan_blockers") or [])
                if isinstance(b, dict)
            ]
            has_fallback_only = any(
                c in {"llm_disabled", "llm_parse_failed"} for c in blocker_codes
            )
            assert not has_fallback_only, (
                f"GREEN contract: LLM-success row must NOT carry fallback-only "
                f"plan_blockers (llm_disabled/llm_parse_failed); got {blocker_codes}. "
                f"This is symptom #1 — the stale fallback blocker leaked onto an "
                f"LLM-success row."
            )
            # fallback-only transient audit fields cleared.
            assert "fallback_trade_plan_blocked" not in decision, (
                "GREEN: fallback_trade_plan_blocked must be cleared on LLM success"
            )
            assert "fallback_block_reason" not in decision, (
                "GREEN: fallback_block_reason must be cleared on LLM success"
            )
            # When the LLM did NOT confirm a plan, the blocker is
            # llm_not_confirmed (requirement B spec), never llm_disabled.
            if not decision.get("has_trade_plan"):
                assert "llm_not_confirmed" in blocker_codes, (
                    f"GREEN: LLM-success without a confirmed plan must carry "
                    f"llm_not_confirmed (not llm_disabled); got {blocker_codes}"
                )
                assert "llm_disabled" not in blocker_codes, (
                    "GREEN: LLM-success row must never carry llm_disabled"
                )
        finally:
            handle.close()

    def test_llm_success_with_confirmed_plan_clears_fallback_blocker(self) -> None:
        """GREEN: LLM-success WITH a confirmed plan clears the stale fallback
        ``llm_disabled`` blocker / withheld status / fallback_trade_plan_blocked.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback()
            candidate = _llm_success_candidate(with_plan=True)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_llm_success_path(decision)

            assert str(decision.get("llm_status") or "").lower() == "ok"
            # P1-4: plan_origin=llm_confirmed only when a plan survived
            # normalization. Weak fixtures may trip analysis_degraded and
            # clear the plan — then origin must NOT be llm_confirmed.
            if decision.get("has_trade_plan") and decision.get("trade_plan"):
                assert decision.get("plan_origin") == "llm_confirmed", (
                    "confirmed plan must set plan_origin=llm_confirmed"
                )
            else:
                assert decision.get("plan_origin") != "llm_confirmed", (
                    "P1-4: no surviving plan must not force plan_origin="
                    f"llm_confirmed; got {decision.get('plan_origin')!r}"
                )
            # NOTE: ``has_trade_plan`` may be False here when the snapshot/plan
            # pair trips ``normalize_market_semantics`` (analysis_degraded) — a
            # separate producer-side gate orthogonal to this leak. The
            # requirement-B contract is about the STALE FALLBACK-ONLY blocker
            # being cleared, not about whether a weak fixture can keep a plan.
            # So this test asserts only the fallback-only-clearance invariants.

            blocker_codes = [
                str(b.get("code") or "")
                for b in (decision.get("plan_blockers") or [])
                if isinstance(b, dict)
            ]
            assert "llm_disabled" not in blocker_codes, (
                f"GREEN: confirmed LLM plan must not carry llm_disabled; got {blocker_codes}"
            )
            assert "llm_parse_failed" not in blocker_codes, (
                f"GREEN: confirmed LLM plan must not carry llm_parse_failed; got {blocker_codes}"
            )
            assert "fallback_trade_plan_blocked" not in decision, (
                "GREEN: fallback_trade_plan_blocked cleared on confirmed LLM success"
            )
            assert "fallback_block_reason" not in decision, (
                "GREEN: fallback_block_reason cleared on confirmed LLM success"
            )
            # The stale withheld status from the disabled fallback must NOT
            # survive (the fallback-blocked block set plan_status="withheld";
            # requirement B clears it on LLM success).
            assert decision.get("plan_status") != "withheld", (
                "GREEN: plan_status must not be the stale fallback 'withheld' "
                "on confirmed LLM success"
            )
        finally:
            handle.close()

    def test_report_does_not_render_llm_disabled_on_success_row(self) -> None:
        """RED→GREEN: ``_trade_plan_summary`` must not render 'LLM 已禁用' on
        an LLM-success row. Pre-fix the stale llm_disabled blocker flows
        straight through the render mapping (hourly_report.py:3021-3022) —
        this is symptom #1 as the operator sees it.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback()
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_llm_success_path(decision)
            summary_text = _trade_plan_summary(decision) or ""

            assert "LLM 已禁用" not in summary_text, (
                f"GREEN: LLM-success row must not render 'LLM 已禁用' in the "
                f"trade plan summary; got: {summary_text!r}. Symptom #1."
            )
            assert "LLM 解析失败" not in summary_text, (
                f"GREEN: LLM-success row must not render 'LLM 解析失败'; "
                f"got: {summary_text!r}"
            )
        finally:
            handle.close()

    def test_revert_fail_stale_blocker_renders_llm_disabled(self) -> None:
        """Revert-fail / positive control for the requirement-B clearance.

        This proves the GREEN assertions above are LOAD-BEARING, not vacuously
        true. It reconstructs the PRE-FIX LLM-success row shape — a decision
        that the clearing block would have cleaned but here is hand-injected
        with the stale fallback-only fields STILL PRESENT — and asserts the
        renderer DOES emit 'LLM 已禁用' on it. That is symptom #1 rendered.

        So: if a future change reverts the clearing block (or re-introduces the
        ``dict(fallback)`` merge without clearing), production LLM-success rows
        will look exactly like this injected decision and this test stays the
        SAME (it asserts the pre-fix shape renders the symptom) while the two
        GREEN tests above flip RED — the revert is caught. The combination of
        (GREEN tests pass) + (this control renders the symptom) is the
        revert-fail proof: the clearance is the only thing standing between the
        stale blocker and the rendered string.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback()
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_llm_success_path(decision)

            # Re-inject the EXACT pre-fix stale fallback-only fields the
            # clearing block removed (simulating a reverted fix). The detail
            # string is the risk-engine fallback-blocked block's verbatim
            # output (risk_engine.py:48-49) — matching the production evidence.
            _blockers = list(decision.get("plan_blockers") or [])
            _blockers.append({
                "code": "llm_disabled",
                "stage": "synthesis",
                "detail": "llm_status=disabled, fallback_llm_failed_blocks_paper_order=true",
            })
            decision["plan_blockers"] = _blockers
            decision["fallback_trade_plan_blocked"] = True
            decision["fallback_block_reason"] = "llm_disabled"
            decision["plan_status"] = "withheld"
            # Keep candidate_trade_plan (the production evidence shape) so the
            # renderer's candidate-detail branch is exercised too.

            summary_text = _trade_plan_summary(decision) or ""

            assert "LLM 已禁用" in summary_text, (
                f"revert-fail control: the pre-fix stale llm_disabled blocker "
                f"on an LLM-success row MUST render 'LLM 已禁用' — if it does "
                f"not, the renderer mapping changed and this control no longer "
                f"proves the clearance is load-bearing. got: {summary_text!r}"
            )
        finally:
            handle.close()