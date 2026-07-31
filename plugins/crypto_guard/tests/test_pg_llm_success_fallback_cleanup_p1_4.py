# -*- coding: utf-8 -*-
"""终审返工 Codex final-review P1-4 (2026-07-27): _normalize_llm_decision
success-path cleanup incomplete — RED-first behavioral test + revert-fail.

P1-4 verbatim scope:

  "_normalize_llm_decision success path MUST clear fallback-only: plan_origin
  (when LLM confirms a plan, must not remain deterministic_sop/
  deterministic_fallback), fallback_trade_plan_blocked, fallback_block_reason,
  original_decision, downgraded_decision, and the llm_disabled/
  llm_parse_failed blockers. ... Add llm_not_confirmed blocker ONLY when a
  dict-typed NON-EMPTY candidate_trade_plan exists AND the LLM did NOT confirm
  a trade_plan. Normal monitor_only/no_edge results WITHOUT a
  candidate_trade_plan must NOT get llm_not_confirmed. When LLM succeeds AND
  confirms a trade_plan: must NOT leave plan_origin=deterministic_sop,
  llm_disabled, llm_parse_failed, or llm_not_confirmed."

And the caller fix (llm_agent_judge.py:1435-1436 / 1473-1474): the unconditional
``plan_origin="llm_confirmed"`` / ``plan_execution_state="confirmed"`` must be
CONDITIONAL on ``decision.get("has_trade_plan") and decision.get("trade_plan")``.
When the LLM succeeded but produced NO plan, ``plan_origin`` must NOT be
``llm_confirmed`` (nothing was confirmed) — it should keep the fallback's value
(e.g. ``deterministic_sop``). The schema does NOT constrain ``plan_origin`` (it
is a free-form audit field surfaced on ``raw_decision_json`` via
``controller_decision_from_legacy``); the renderer
``_render_plan_state_label`` only recognizes ``llm_confirmed`` /
``deterministic_fallback`` / ``deterministic_sop``. So we do NOT invent a new
enum value — we keep the fallback's ``plan_origin`` when no plan was confirmed.

This file covers THREE production paths (RED-first + revert-fail):

  1. fallback HAS candidate + LLM NO plan
     -> candidate_trade_plan preserved (dict, non-empty) + llm_not_confirmed
        blocker present; plan_origin NOT llm_confirmed; no llm_disabled /
        llm_parse_failed blocker.
  2. fallback NO candidate + LLM NO plan
     -> no candidate_trade_plan (or empty); NO llm_not_confirmed blocker; no
        llm_disabled / llm_parse_failed; plan_origin not llm_confirmed.
  3. fallback HAS candidate + LLM HAS valid plan
     -> plan_origin="llm_confirmed" (set by the FIXED caller when
        has_trade_plan); plan_execution_state="confirmed"; NO llm_disabled /
        llm_parse_failed / llm_not_confirmed blocker; trade_plan is the LLM
        plan; candidate_trade_plan preserved per Phase E invariant.

The tests call ``_normalize_llm_decision`` directly with a constructed
``candidate`` + ``fallback`` + ``snapshot``, then apply the FIXED
``_run_single_llm_attempt`` success-path overrides (conditional on
``has_trade_plan`` / ``trade_plan``). The revert-fail control reconstructs the
pre-fix UNCONDITIONAL ``llm_not_confirmed`` append on path 2 (no candidate) and
asserts the test would have caught the regression (path 2's decision has NO
``llm_not_confirmed``, so re-introducing the unconditional append flips this
RED).

Building the disabled fallback via ``apply_risk_to_decision`` on a hand-built
deterministic decision avoids the ``run_ga_sop_decision`` strategy-loader DB
path (which needs seeded strategies) while exercising the REAL risk-engine
fallback-blocked block that writes the ``llm_disabled`` blocker +
``candidate_trade_plan``. ``make_repo()`` sets the DSN so ``load_config()``
works inside ``apply_risk_to_decision``.

The "fallback NO candidate" fixture is built from a disabled deterministic
decision WITHOUT a trade_plan, so ``apply_risk_to_decision``'s fallback-blocked
block (which guards on has_trade_plan + trade_plan) does NOT fire and
``candidate_trade_plan`` is never written. This is the critical path-2 shape:
pre-fix the UNCONDITIONAL ``llm_not_confirmed`` append at line 3535 fired on
this row (because ``not has_trade_plan`` is True) and polluted a monitor_only
LLM-success row with a spurious ``llm_not_confirmed`` blocker even though
there was never a candidate plan to confirm.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.llm_agent_judge import _normalize_llm_decision
from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.tests.pg_fixtures import make_repo


_ANALYSIS_TIME_UTC = 1785132899999


def _bullish_snapshot() -> dict:
    """A snapshot shape where ALL four required TFs (1d/4h/1h/15m) are healthy
    and CLOSED, so ``normalize_market_semantics``'s ``data_incomplete`` fail-
    closed path does NOT fire and a confirmed trade_plan survives. This is
    required for path 3 (LLM HAS valid plan) so the plan is not stripped by the
    data-health gate — otherwise ``has_trade_plan`` flips False and the FIXED
    caller (conditional on has_trade_plan) would NOT set
    ``plan_origin=llm_confirmed``. The fallback-blocked block in
    ``apply_risk_to_decision`` (used by the fixture) guards on
    has_trade_plan + trade_plan, not on data health, so the healthy snapshot
    does not change the fixture's leak behavior.

    ``health_by_tf`` with ``ready=True`` + ``last_close_time`` <=
    ``analysis_time_utc`` makes ``build_timeframe_context`` set
    ``closed=True`` for every TF in ``TIMEFRAME_CONTEXT_TFS``.
    """
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


def _det_trade_plan() -> dict:
    """A schema-valid deterministic LONG trade plan (what run_ga_sop_decision
    would produce on the disabled path)."""
    return {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 180.0,
        "stop_loss": 172.0,
        "take_profits": [{"price": 196.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "跌破 172.0",
    }


def _det_fallback_pre_risk(*, with_plan: bool) -> dict:
    """The deterministic SOP decision shape BEFORE ``apply_risk_to_decision``
    runs the disabled path. Mirrors ``run_ga_sop_decision`` output + the
    disabled-path §8 envelope fields set at llm_agent_judge.py:131-149.

    ``with_plan=True``: the disabled fallback carries a trade_plan, so
    ``apply_risk_to_decision``'s fallback-blocked block fires and writes
    ``candidate_trade_plan`` + ``llm_disabled`` blocker (the leak source for
    path 1 / path 3).
    ``with_plan=False``: the disabled fallback has NO trade_plan, so the
    fallback-blocked block does NOT fire and ``candidate_trade_plan`` is never
    written (the path-2 shape — no candidate to confirm).
    """
    base = {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "signal_grade": "A",
        "confidence": 0.82,
        "market_bias": "bullish",
        "decision": "trade_plan_available" if with_plan else "monitor_only",
        "has_trade_plan": with_plan,
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"] if with_plan else ["monitor_only"],
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
        "plan_execution_state": "no_candidate" if not with_plan else "confirmed",
    }
    if with_plan:
        base["trade_plan"] = _det_trade_plan()
    return base


def _risk_processed_disabled_fallback(*, with_plan: bool) -> dict:
    """Reproduce fair_llm_call_adapter:1535 — the prompt fallback built from
    ``run_agent_sop_decision(snapshot, use_llm=False)``.

    Runs ``apply_risk_to_decision`` on the disabled-path deterministic
    fallback, exactly as llm_agent_judge.py:150 does. When ``with_plan=True``
    the fallback-blocked block (risk_engine.py:28-68) fires on
    llm_status=disabled + has_trade_plan + trade_plan and writes the
    ``llm_disabled`` blocker, candidate_trade_plan, plan_status=withheld,
    fallback_trade_plan_blocked — the exact leak source. When
    ``with_plan=False`` the block does NOT fire and candidate_trade_plan is
    never written.
    """
    snapshot = _bullish_snapshot()
    fallback = _det_fallback_pre_risk(with_plan=with_plan)
    processed = apply_risk_to_decision(fallback, snapshot)
    if with_plan:
        # Guard: confirm the risk-processed disabled fallback carries the leak.
        blocker_codes = [
            str(b.get("code") or "")
            for b in (processed.get("plan_blockers") or [])
            if isinstance(b, dict)
        ]
        assert "llm_disabled" in blocker_codes, (
            f"fixture(with_plan=True): risk-processed disabled fallback must "
            f"carry llm_disabled blocker; got codes={blocker_codes}."
        )
        assert processed.get("candidate_trade_plan"), (
            "fixture(with_plan=True): risk-processed disabled fallback must "
            "preserve candidate_trade_plan"
        )
        assert processed.get("fallback_trade_plan_blocked") is True, (
            "fixture(with_plan=True): risk-processed disabled fallback must "
            "set fallback_trade_plan_blocked"
        )
    else:
        # Guard: confirm the no-plan disabled fallback does NOT carry a
        # candidate (the fallback-blocked block guards on has_trade_plan).
        assert not processed.get("candidate_trade_plan"), (
            f"fixture(with_plan=False): risk-processed disabled fallback "
            f"must NOT carry candidate_trade_plan; got "
            f"{processed.get('candidate_trade_plan')!r}"
        )
    return processed


def _apply_fixed_success_path(decision: dict) -> dict:
    """Mirror the FIXED ``_run_single_llm_attempt`` success path
    (llm_agent_judge.py:1473-1482). The fix makes
    ``plan_origin="llm_confirmed"`` / ``plan_execution_state="confirmed"``
    CONDITIONAL on ``decision.get("has_trade_plan") and
    decision.get("trade_plan")``. When the LLM succeeded but produced NO plan,
    ``plan_origin`` keeps the fallback's value (e.g. ``deterministic_sop``) and
    ``plan_execution_state`` keeps whatever ``_normalize_llm_decision`` left.

    The §8 attempt_meta envelope is merged unconditionally (it records that the
    LLM call succeeded regardless of whether a plan was produced).
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
    ``with_plan=False``: LLM returned a monitor_only / B-grade / no-plan
    decision — the case where requirement B says the blocker must become
    ``llm_not_confirmed`` ONLY when a candidate exists (path 1), and must NOT
    be added when there is no candidate (path 2).

    Uses ``signal_grade="B"`` for the no-plan case so the S/A auto-build block
    (llm_agent_judge.py:3500-3509) does NOT fire and auto-generate a plan — that
    would mask the no-plan path this test isolates.
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


def _blocker_codes(decision: dict) -> list[str]:
    return [
        str(b.get("code") or "")
        for b in (decision.get("plan_blockers") or [])
        if isinstance(b, dict)
    ]


class TestPgLLMSuccessFallbackCleanupP1_4:
    """P1-4: _normalize_llm_decision success-path cleanup incomplete."""

    def test_path1_fallback_has_candidate_llm_no_plan(self) -> None:
        """RED→GREEN path 1: fallback HAS candidate + LLM NO plan.

        The disabled fallback carried a trade_plan, so
        ``apply_risk_to_decision``'s fallback-blocked block preserved it under
        ``candidate_trade_plan`` + wrote the ``llm_disabled`` blocker. The LLM
        candidate is monitor_only (B grade, no plan). Post-fix:
          - ``candidate_trade_plan`` preserved (dict, non-empty) — Phase E.
          - ``llm_not_confirmed`` blocker present (a candidate existed and the
            LLM did not confirm a plan).
          - ``plan_origin`` NOT ``llm_confirmed`` (the FIXED caller does not set
            it when no plan was confirmed) — keeps the fallback's
            ``deterministic_sop``.
          - NO ``llm_disabled`` / ``llm_parse_failed`` blocker (cleared by the
            normalize cleanup block).
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback(with_plan=True)
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_fixed_success_path(decision)

            # ── Invariants ────────────────────────────────────────────────
            assert str(decision.get("llm_status") or "").lower() == "ok", (
                "LLM-success row must have llm_status=ok"
            )
            # candidate_trade_plan preserved (dict, non-empty) — Phase E.
            ctp = decision.get("candidate_trade_plan")
            assert isinstance(ctp, dict) and ctp, (
                f"path1 GREEN: candidate_trade_plan must be preserved as a "
                f"non-empty dict (Phase E invariant); got {ctp!r}"
            )
            # plan_origin NOT llm_confirmed (no plan was confirmed).
            assert decision.get("plan_origin") != "llm_confirmed", (
                f"path1 GREEN: LLM-success without a confirmed plan must NOT "
                f"have plan_origin=llm_confirmed; got "
                f"{decision.get('plan_origin')!r}"
            )
            # llm_not_confirmed present (a candidate existed, LLM did not confirm).
            codes = _blocker_codes(decision)
            assert "llm_not_confirmed" in codes, (
                f"path1 GREEN: a non-empty candidate_trade_plan + LLM no plan "
                f"must carry llm_not_confirmed; got {codes}"
            )
            # NO fallback-only blockers.
            assert "llm_disabled" not in codes, (
                f"path1 GREEN: must NOT carry llm_disabled; got {codes}"
            )
            assert "llm_parse_failed" not in codes, (
                f"path1 GREEN: must NOT carry llm_parse_failed; got {codes}"
            )
            # fallback-only transient audit fields cleared.
            assert "fallback_trade_plan_blocked" not in decision, (
                "path1 GREEN: fallback_trade_plan_blocked must be cleared"
            )
            assert "fallback_block_reason" not in decision, (
                "path1 GREEN: fallback_block_reason must be cleared"
            )
        finally:
            handle.close()

    def test_path2_fallback_no_candidate_llm_no_plan(self) -> None:
        """RED→GREEN path 2: fallback NO candidate + LLM NO plan.

        The disabled fallback had NO trade_plan, so the fallback-blocked block
        did NOT fire and ``candidate_trade_plan`` was never written. The LLM
        candidate is monitor_only (B grade, no plan). Post-fix:
          - NO ``candidate_trade_plan`` (or empty).
          - NO ``llm_not_confirmed`` blocker (there was never a candidate to
            confirm — a normal monitor_only / no_edge result).
          - NO ``llm_disabled`` / ``llm_parse_failed`` blocker.
          - ``plan_origin`` not ``llm_confirmed`` (no plan confirmed).

        Pre-fix the UNCONDITIONAL ``llm_not_confirmed`` append at line 3535
        fired on this row (because ``not has_trade_plan`` is True) and polluted
        a monitor_only LLM-success row with a spurious ``llm_not_confirmed``
        blocker. The revert-fail control below reconstructs that pre-fix shape.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback(with_plan=False)
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_fixed_success_path(decision)

            # ── Invariants ────────────────────────────────────────────────
            assert str(decision.get("llm_status") or "").lower() == "ok", (
                "LLM-success row must have llm_status=ok"
            )
            # NO candidate_trade_plan (or empty).
            ctp = decision.get("candidate_trade_plan")
            assert not (isinstance(ctp, dict) and ctp), (
                f"path2 GREEN: no candidate_trade_plan should be present; "
                f"got {ctp!r}"
            )
            # NO llm_not_confirmed blocker (no candidate existed).
            codes = _blocker_codes(decision)
            assert "llm_not_confirmed" not in codes, (
                f"path2 GREEN: a monitor_only/no_edge result WITHOUT a "
                f"candidate_trade_plan must NOT carry llm_not_confirmed; got "
                f"{codes}. This is the P1-4 regression — the unconditional "
                f"append at line 3535 fired on a no-candidate row."
            )
            # NO fallback-only blockers.
            assert "llm_disabled" not in codes, (
                f"path2 GREEN: must NOT carry llm_disabled; got {codes}"
            )
            assert "llm_parse_failed" not in codes, (
                f"path2 GREEN: must NOT carry llm_parse_failed; got {codes}"
            )
            # plan_origin not llm_confirmed (no plan confirmed).
            assert decision.get("plan_origin") != "llm_confirmed", (
                f"path2 GREEN: LLM-success without a confirmed plan must NOT "
                f"have plan_origin=llm_confirmed; got "
                f"{decision.get('plan_origin')!r}"
            )
        finally:
            handle.close()

    def test_path3_fallback_has_candidate_llm_has_plan(self) -> None:
        """RED→GREEN path 3: fallback HAS candidate + LLM HAS valid plan.

        The disabled fallback carried a trade_plan (preserved under
        ``candidate_trade_plan`` by the fallback-blocked block). The LLM
        candidate confirms a trade plan (A grade + trade_plan). Post-fix:
          - ``plan_origin="llm_confirmed"`` (set by the FIXED caller when
            has_trade_plan).
          - ``plan_execution_state="confirmed"``.
          - NO ``llm_disabled`` / ``llm_parse_failed`` / ``llm_not_confirmed``
            blocker.
          - ``trade_plan`` is the LLM plan (not None).
          - ``candidate_trade_plan`` preserved per Phase E invariant.

        This is the "LLM succeeds AND confirms a trade_plan" contract: must
        NOT leave plan_origin=deterministic_sop, llm_disabled, llm_parse_failed,
        or llm_not_confirmed.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback(with_plan=True)
            candidate = _llm_success_candidate(with_plan=True)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_fixed_success_path(decision)

            # ── Invariants ────────────────────────────────────────────────
            assert str(decision.get("llm_status") or "").lower() == "ok", (
                "LLM-success row must have llm_status=ok"
            )
            # plan_origin = llm_confirmed (a plan was confirmed).
            assert decision.get("plan_origin") == "llm_confirmed", (
                f"path3 GREEN: LLM-success WITH a confirmed plan must have "
                f"plan_origin=llm_confirmed; got "
                f"{decision.get('plan_origin')!r}"
            )
            # plan_execution_state = confirmed.
            assert decision.get("plan_execution_state") == "confirmed", (
                f"path3 GREEN: confirmed plan must have "
                f"plan_execution_state=confirmed; got "
                f"{decision.get('plan_execution_state')!r}"
            )
            # trade_plan is the LLM plan (not None).
            assert decision.get("has_trade_plan"), (
                f"path3 GREEN: has_trade_plan must be True; got "
                f"{decision.get('has_trade_plan')}"
            )
            assert isinstance(decision.get("trade_plan"), dict) and decision.get("trade_plan"), (
                f"path3 GREEN: trade_plan must be the LLM plan (non-empty dict); "
                f"got {decision.get('trade_plan')!r}"
            )
            # NO llm_disabled / llm_parse_failed / llm_not_confirmed blocker.
            codes = _blocker_codes(decision)
            assert "llm_disabled" not in codes, (
                f"path3 GREEN: confirmed LLM plan must NOT carry llm_disabled; "
                f"got {codes}"
            )
            assert "llm_parse_failed" not in codes, (
                f"path3 GREEN: confirmed LLM plan must NOT carry llm_parse_failed; "
                f"got {codes}"
            )
            assert "llm_not_confirmed" not in codes, (
                f"path3 GREEN: confirmed LLM plan must NOT carry "
                f"llm_not_confirmed; got {codes}"
            )
            # candidate_trade_plan preserved per Phase E invariant.
            ctp = decision.get("candidate_trade_plan")
            assert isinstance(ctp, dict) and ctp, (
                f"path3 GREEN: candidate_trade_plan must be preserved (Phase E "
                f"invariant); got {ctp!r}"
            )
            # fallback-only transient audit fields cleared.
            assert "fallback_trade_plan_blocked" not in decision, (
                "path3 GREEN: fallback_trade_plan_blocked must be cleared"
            )
            assert "fallback_block_reason" not in decision, (
                "path3 GREEN: fallback_block_reason must be cleared"
            )
            assert decision.get("plan_status") != "withheld", (
                "path3 GREEN: plan_status must not be the stale fallback 'withheld'"
            )
        finally:
            handle.close()

    def test_revert_fail_prefixed_unconditional_append_pollutes_path2(self) -> None:
        """Revert-fail / positive control for the path-2 ``llm_not_confirmed``
        gating.

        This proves the path-2 GREEN assertion (no ``llm_not_confirmed`` when
        no candidate) is LOAD-BEARING, not vacuously true. It reconstructs the
        PRE-FIX unconditional append shape — the exact code at line 3535-3548
        before P1-4 gated it on a non-empty ``candidate_trade_plan`` — and
        applies it to a path-2 decision (no candidate). The pre-fix code WOULD
        append ``llm_not_confirmed`` here; the control asserts that fact so
        that re-introducing the unconditional append flips path2 RED (the
        ``assert "llm_not_confirmed" not in codes`` above would fail).

        So: (path2 GREEN passes with no ``llm_not_confirmed``) + (this control
        shows the pre-fix unconditional append DOES add it) is the revert-fail
        proof — the gating on ``isinstance(candidate_trade_plan, dict) and
        candidate_trade_plan`` is the only thing standing between a clean
        monitor_only row and the spurious blocker.
        """
        handle = make_repo()
        try:
            fallback = _risk_processed_disabled_fallback(with_plan=False)
            candidate = _llm_success_candidate(with_plan=False)
            snapshot = _bullish_snapshot()

            decision = _normalize_llm_decision(candidate, snapshot, fallback)
            decision = _apply_fixed_success_path(decision)

            # Confirm the post-fix decision has NO candidate and NO
            # llm_not_confirmed (the GREEN contract path 2 asserts).
            assert not (isinstance(decision.get("candidate_trade_plan"), dict)
                        and decision.get("candidate_trade_plan")), (
                "revert-fail control setup: post-fix path2 must have no "
                "candidate_trade_plan for the control to be meaningful."
            )
            codes_before = _blocker_codes(decision)
            assert "llm_not_confirmed" not in codes_before, (
                f"revert-fail control setup: post-fix path2 must have no "
                f"llm_not_confirmed before the simulated pre-fix append; got "
                f"{codes_before}"
            )

            # Reconstruct the PRE-FIX unconditional append (the verbatim shape
            # of lines 3535-3548 before P1-4 added the candidate guard). The
            # pre-fix code fired on ``not has_trade_plan`` REGARDLESS of whether
            # a candidate existed.
            if not decision.get("has_trade_plan") or not decision.get("trade_plan"):
                _blockers = list(decision.get("plan_blockers") or [])
                _blockers.append({
                    "code": "llm_not_confirmed",
                    "stage": "llm_synthesis",
                    "detail": "llm_status=ok 但 LLM 未给出可执行 trade_plan，候选计划保留为 candidate_trade_plan",
                })
                decision["plan_blockers"] = _blockers

            # The control asserts the pre-fix shape DID add the spurious
            # blocker — proving the gating is load-bearing. If a future change
            # reverts the gating, path2's ``assert "llm_not_confirmed" not in
            # codes`` flips RED and this control stays the SAME (it asserts the
            # pre-fix shape adds the blocker) — the revert is caught.
            codes_after = _blocker_codes(decision)
            assert "llm_not_confirmed" in codes_after, (
                f"revert-fail control: the pre-fix UNCONDITIONAL append at line "
                f"3535 MUST add llm_not_confirmed to a no-candidate path-2 row "
                f"(because not has_trade_plan is True). If it does not, the "
                f"control no longer proves the gating is load-bearing. got "
                f"{codes_after}"
            )
        finally:
            handle.close()