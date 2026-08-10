# -*- coding: utf-8 -*-
"""终审返工 Phase-2 P2-1 (2026-07-27): LLM failed/disabled direction leakage
— RED-first behavioral test + revert-fail.

Six-symptom fix, symptom #2 (requirement C verbatim):

  "LLM failed decisions still have market_bias=bearish, continuously
  triggering deterministic_direction_from_failed_llm."

Production read-only evidence (phase2_step_a_evidence_probe.py) flagged
LLM-failed/disabled rows with market_bias in {bullish, bearish}. The
``deterministic_direction_from_failed_llm`` state-consistency diagnostic
(state_consistency.py:2832) fires on exactly this shape — llm_status in
{failed, disabled} AND market_bias in {bullish, bearish} — and re-fires
every hour because the rows persist.

Root cause (traced end-to-end this session):

  All four LLM failed/disabled terminal paths in ``run_agent_sop_decision``
  end with ``return apply_risk_to_decision(fallback, snapshot)``:
    - use_llm=False disabled path        (llm_agent_judge.py:150)
    - breaker open skip                  (llm_agent_judge.py:184)
    - fair preset candidate is None      (llm_agent_judge.py:237)
    - retry wrapper returned None        (llm_agent_judge.py:275)
  The deterministic ``fallback`` carries ``market_bias`` from
  ``run_ga_sop_decision`` (bullish/bearish). ``apply_risk_to_decision``
  (risk_engine.py:16) only enters the fallback-blocked block (22-68) when
  llm_status in {failed, disabled} AND has_trade_plan AND trade_plan — and
  even that block writes ``plan_blockers`` / ``candidate_trade_plan`` /
  ``plan_status="withheld"`` but NEVER touches ``market_bias``. So the
  bullish/bearish bias from the deterministic SOP survives onto the
  persisted failed/disabled row, and the diagnostic re-fires hourly.

Fix (requirement C, scoped at 07-27 final review): "Fix LLM FAILED direction
leakage: force market_bias='unknown', no executable trade_plan, effective
grade ≤ B, plan_execution_state=unconfirmed on the LLM-failed terminal paths
BEFORE persistence; keep deterministic direction in
``deterministic_reference``/``candidate_trade_plan``; must NOT loosen
``deterministic_direction_from_failed_llm`` diagnostic."

Scoping (07-27 final review): the fail-closed block fires ONLY for
``llm_status == "failed"``. It does NOT fire for ``llm_status == "disabled"``
(``use_llm=False`` / ``CRYPTO_GUARD_LLM_ANALYSIS=0`` deterministic-only mode),
because the deterministic direction IS the intended product there (07-03
semantic-accuracy tests ``test_doge_countertrend_rebound_not_bullish_middle``
and ``test_sol_short_bullish_but_explains_htf_mixed`` pin that HTF-aware bias
must survive on disabled rows). The breaker-skip / preset-None / retry-None
terminal paths all set ``llm_status="failed"``, so they ARE fail-closed.
Production runs with the LLM enabled, so the leaked rows are ``failed``.

This test reproduces the leak at its SOURCE: it builds the SAME deterministic
fallback that ``run_ga_sop_decision`` returns (bullish bias + trade_plan),
sets the §8 failed/disabled envelope exactly as the four terminal paths do,
and hands it to ``apply_risk_to_decision``. It asserts the fail-closed
shape post-fix (GREEN) for ``failed`` and the deterministic-only-preserving
shape for ``disabled``, plus the leak pre-fix (RED via the revert-fail
control).

It exercises BOTH ``llm_status="failed"`` AND ``llm_status="disabled"``, and
BOTH the has-trade-plan case (fallback-blocked block fires) and the no-plan
case (fallback-blocked block does NOT fire — the leak path that the current
code never closes). The no-plan case is the critical one: the fallback-
blocked block's has_trade_plan guard means a failed decision WITHOUT a
trade_plan bypasses the block entirely, so ``market_bias`` is never forced
to unknown even post-block-fix — requirement C says ALL failed/disabled
terminal paths, so the fix must cover this case too.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.tests.pg_fixtures import make_repo


_ANALYSIS_TIME_UTC = 1785132899999


def _bullish_snapshot() -> dict:
    """A snapshot where ALL four required TFs (1d/4h/1h/15m) are healthy and
    CLOSED — so ``normalize_market_semantics``'s ``data_incomplete`` fail-
    closed path does NOT fire. This isolates symptom #2: when the data is
    healthy but the LLM failed/disabled, the deterministic bullish bias
    leaks onto the failed row. If any required TF were missing/unclosed,
    the ``data_incomplete`` path would force ``market_bias=unknown`` for an
    unrelated reason and mask the LLM-failed leak this test targets.

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
    """A deterministic LONG trade plan (the deterministic direction to
    preserve under candidate_trade_plan)."""
    return {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 180.0,
        "stop_loss": 172.0,
        "take_profits": [{"price": 196.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "跌破 172.0",
    }


def _det_fallback_pre_risk(*, llm_status: str, with_plan: bool) -> dict:
    """The deterministic SOP decision shape BEFORE ``apply_risk_to_decision``
    runs — bullish bias + A grade + (optionally) a trade_plan. The §8
    envelope fields mirror the four terminal paths in
    ``run_agent_sop_decision`` (llm_agent_judge.py:131-275)."""
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
        # §8 envelope (matches the terminal paths):
        "analysis_source": "deterministic_fallback",
        "llm_status": llm_status,
        "llm_attempt_count": 0,
        "llm_provider_call_count": 0,
        "llm_latency_ms": 0,
        "llm_prompt_bytes": None,
        "llm_continuity_included": None,
        "llm_model": None,
        "llm_terminal_reason": "breaker_skipped" if llm_status == "failed" else "llm_disabled",
        "llm_fallback_reason": "circuit_breaker_open" if llm_status == "failed" else "llm_disabled",
        "plan_origin": "deterministic_fallback" if llm_status == "failed" else "deterministic_sop",
        "plan_execution_state": "unconfirmed" if llm_status == "failed" else "no_candidate",
    }
    if with_plan:
        base["trade_plan"] = _det_trade_plan()
    return base


class TestPgLLMFailedDirectionFailClosedP2_1:
    """Symptom #2 (requirement C): LLM failed/disabled direction leakage."""

    @pytest.mark.parametrize("llm_status", ["failed", "disabled"])
    @pytest.mark.parametrize("with_plan", [True, False])
    def test_failed_disabled_market_bias_scoping(
        self, llm_status: str, with_plan: bool
    ) -> None:
        """RED→GREEN (requirement C, scoped at 07-27 final review):

        - ``llm_status="failed"`` (the LLM was enabled and the call FAILED): the
          deterministic bullish/bearish ``market_bias`` is a LEAK — the decision
          was supposed to be LLM-confirmed and was not. The fail-closed block
          (risk_engine.py) forces ``market_bias="unknown"``, no executable
          trade_plan, effective grade ≤ B, ``plan_execution_state=unconfirmed``,
          and preserves the deterministic LONG under ``candidate_trade_plan``.
          This is symptom #2 — the production leak.
        - ``llm_status="disabled"`` (``use_llm=False`` /
          ``CRYPTO_GUARD_LLM_ANALYSIS=0`` deterministic-only mode): the
          deterministic direction IS the intended product (07-03 semantic-
          accuracy tests pin HTF-aware bias must survive). The fail-closed
          block does NOT fire here; ``market_bias`` keeps the HTF-aware value
          (bullish in this fixture). Production runs with the LLM enabled, so
          there are no ``disabled`` production rows to fail-close.

        The breaker-skip path (llm_agent_judge.py:164), preset-None path
        (237), and retry-None path (275) ALL set ``llm_status="failed"``, so
        they ARE fail-closed by the block. Only the ``use_llm=False`` disabled
        path (133) is excluded.

        Pre-fix the bullish bias survived ``apply_risk_to_decision`` on the
        ``failed`` row (RED — the ``deterministic_direction_from_failed_llm``
        diagnostic re-fired hourly). Post-fix the fail-closed block forces
        unknown + caps grade on ``failed`` (GREEN — requirement C).
        """
        handle = make_repo()
        try:
            snapshot = _bullish_snapshot()
            fallback = _det_fallback_pre_risk(llm_status=llm_status, with_plan=with_plan)

            decision = apply_risk_to_decision(fallback, snapshot)

            bias = str(decision.get("market_bias") or "").lower()
            if llm_status == "failed":
                # ── The defect: bullish/bearish bias leaked onto a failed row ─
                assert bias == "unknown", (
                    f"GREEN contract: llm_status=failed with_plan={with_plan} "
                    f"must force market_bias=unknown; got {bias!r}. This is "
                    f"symptom #2 — the deterministic bullish direction leaked "
                    f"onto a failed row and re-fires "
                    f"deterministic_direction_from_failed_llm hourly."
                )
                # No executable trade_plan on a failed row.
                assert not decision.get("has_trade_plan"), (
                    f"GREEN: llm_status=failed with_plan={with_plan} must have "
                    f"no executable trade_plan; has_trade_plan="
                    f"{decision.get('has_trade_plan')}"
                )
                assert decision.get("trade_plan") in (None, {}), (
                    f"GREEN: trade_plan must be null/empty on a failed row; "
                    f"got {decision.get('trade_plan')!r}"
                )
                # No create_paper_order in suggested_actions (fail-closed).
                actions = decision.get("suggested_actions") or []
                assert "create_paper_order" not in actions, (
                    f"GREEN: create_paper_order must not appear in "
                    f"suggested_actions on a failed row; got {actions}"
                )
                # Effective grade ≤ B (order_value ≤ 2). Requirement C verbatim.
                from plugins.crypto_guard.strategy.grade_config import grade_order_value
                grade = str(decision.get("signal_grade") or "D").upper()
                assert grade_order_value(grade) <= grade_order_value("B"), (
                    f"GREEN: effective grade must be ≤ B on a failed row; "
                    f"got {grade}. Requirement C."
                )
                # plan_execution_state must be unconfirmed (not confirmed).
                state = str(decision.get("plan_execution_state") or "").lower()
                assert state == "unconfirmed", (
                    f"GREEN: plan_execution_state must be unconfirmed on a "
                    f"failed row; got {state!r}. Requirement C."
                )
                # Deterministic direction preserved under candidate_trade_plan
                # (when the fallback had a plan). Requirement C: keep
                # deterministic direction in candidate_trade_plan.
                if with_plan:
                    ctp = decision.get("candidate_trade_plan")
                    assert isinstance(ctp, dict) and ctp.get("side") == "LONG", (
                        f"GREEN: deterministic LONG direction must be preserved "
                        f"under candidate_trade_plan on a failed row; got "
                        f"{ctp!r}. Requirement C (keep deterministic direction)."
                    )
            else:
                # llm_status == "disabled" — deterministic-only operating mode.
                # The fail-closed block MUST NOT fire here; the HTF-aware bias
                # survives (the deterministic direction is the product). This
                # pins the scoping so a future over-broad change that re-applies
                # fail-closed to ``disabled`` would break this assertion (and
                # the 07-03 semantic-accuracy tests).
                assert bias == "bullish", (
                    f"GREEN scoping: llm_status=disabled with_plan={with_plan} "
                    f"is deterministic-only mode — market_bias must KEEP the "
                    f"HTF-aware value (bullish), NOT be forced to unknown; got "
                    f"{bias!r}. Over-broad fail-closing of disabled breaks "
                    f"deterministic-only mode (07-03 semantic-accuracy tests)."
                )
        finally:
            handle.close()

    def test_revert_fail_failed_row_with_no_plan_leaks_bias(self) -> None:
        """Revert-fail / positive control for the no-plan failed path.

        The ``with_plan=False`` case is the load-bearing one: the
        fallback-blocked block (risk_engine.py:28) guards on has_trade_plan +
        trade_plan, so a failed decision WITHOUT a plan bypasses the block
        entirely. Pre-fix the bullish bias survives untouched. This test
        reconstructs that pre-fix shape and asserts the diagnostic WOULD
        fire on it — proving the fail-closed block (not the fallback-blocked
        block) is what closes symptom #2 for the no-plan path.

        If a future change reverts the fail-closed block, the
        ``test_failed_disabled_forces_market_bias_unknown[disabled-False]``
        case flips RED and this control stays the same (it asserts the
        pre-fix shape leaks) — the revert is caught.
        """
        handle = make_repo()
        try:
            snapshot = _bullish_snapshot()
            # Build the pre-fix shape directly: a failed decision with no
            # plan and bullish bias that bypassed the fallback-blocked block.
            pre_fix = {
                "symbol": "SOLUSDT",
                "analysis_time_utc": _ANALYSIS_TIME_UTC,
                "signal_grade": "A",
                "confidence": 0.82,
                "market_bias": "bullish",
                "decision": "monitor_only",
                "has_trade_plan": False,
                "trade_plan": None,
                "opportunity_watch": None,
                "suggested_actions": ["monitor_only"],
                "risk_notes": [],
                "llm_status": "failed",
                "llm_terminal_reason": "breaker_skipped",
                "plan_origin": "deterministic_fallback",
                "plan_execution_state": "unconfirmed",
            }
            decision = apply_risk_to_decision(pre_fix, snapshot)
            bias = str(decision.get("market_bias") or "").lower()
            grade = str(decision.get("signal_grade") or "D").upper()
            # The control asserts the pre-fix leak: a failed no-plan row that
            # reaches apply_risk_to_decision with bullish bias and is NOT
            # corrected by the fallback-blocked block (which guards on
            # has_trade_plan) carries the bullish bias out — exactly the
            # symptom #2 shape. Post-fix this control STILL shows the leak
            # is caught (bias=unknown), so it is the positive control that
            # proves the fail-closed block is load-bearing for the no-plan
            # path. Assert the fix DID catch it:
            assert bias == "unknown", (
                f"revert-fail control: the no-plan failed path must be "
                f"fail-closed to market_bias=unknown by the fail-closed "
                f"block (the fallback-blocked block guards on has_trade_plan "
                f"and would let this leak). got {bias!r}. If this is "
                f"bullish/bearish, the fail-closed block was reverted."
            )
            # The positive control: confirm the pre-fix shape (the exact dict
            # handed to apply_risk_to_decision) DOES carry the leak — bullish
            # bias on a failed row. This is the load-bearing evidence: the
            # ``pre_fix`` dict's market_bias is bullish, and only the fix
            # (fail-closed block) turns it into unknown on the way out.
            assert str(pre_fix.get("market_bias") or "").lower() == "bullish", (
                "revert-fail control setup: the pre-fix dict must carry "
                "bullish bias (the leak source) for the control to prove "
                "the fail-closed block is load-bearing."
            )
            from plugins.crypto_guard.strategy.grade_config import grade_order_value
            assert grade_order_value(grade) <= grade_order_value("B"), (
                f"revert-fail control: grade must be capped ≤ B on the "
                f"failed no-plan path; got {grade}. Requirement C."
            )
        finally:
            handle.close()

    def test_p2_r1_c_fail_closed_independent_of_paper_order_flag(self) -> None:
        """P2-R1: C fail-closed must NOT depend on paper-order withhold flag.

        When ``fallback_llm_failed_blocks_paper_order=False``, the BTC#9
        paper-order withhold block is off, but direction fail-closed for
        ``llm_status=failed`` must still force market_bias=unknown, no
        executable plan, grade ≤ B. Coupling C to that flag reopened the
        bias leak under config flip.
        """
        handle = make_repo()
        try:
            from unittest.mock import MagicMock, patch

            snapshot = _bullish_snapshot()
            for with_plan in (True, False):
                fallback = _det_fallback_pre_risk(
                    llm_status="failed", with_plan=with_plan
                )
                mock_cfg = MagicMock()
                mock_cfg.trading_mode = {
                    "risk": {"fallback_llm_failed_blocks_paper_order": False}
                }
                with patch(
                    "plugins.crypto_guard.risk.risk_engine.load_config",
                    return_value=mock_cfg,
                ):
                    decision = apply_risk_to_decision(fallback, snapshot)
                bias = str(decision.get("market_bias") or "").lower()
                assert bias == "unknown", (
                    f"P2-R1: paper-order flag false + llm_status=failed "
                    f"with_plan={with_plan} must still force market_bias="
                    f"unknown; got {bias!r}"
                )
                assert not decision.get("has_trade_plan"), (
                    f"P2-R1: no executable trade_plan; has_trade_plan="
                    f"{decision.get('has_trade_plan')}"
                )
                from plugins.crypto_guard.strategy.grade_config import (
                    grade_order_value,
                )
                grade = str(decision.get("signal_grade") or "D").upper()
                assert grade_order_value(grade) <= grade_order_value("B"), (
                    f"P2-R1: grade ≤ B required; got {grade}"
                )
                # P1-1 residual: when with_plan, candidate_trade_plan must be
                # preserved even though the paper-order withhold block did not
                # run (flag=false). Requirement C keep deterministic direction.
                if with_plan:
                    ctp = decision.get("candidate_trade_plan")
                    assert isinstance(ctp, dict) and ctp.get("side") == "LONG", (
                        f"P1-1/P2-R1: flag=false + failed + with_plan must "
                        f"preserve candidate_trade_plan LONG; got {ctp!r}"
                    )
                    actions = decision.get("suggested_actions") or []
                    assert "create_paper_order" not in actions, (
                        f"P1-1: create_paper_order must not appear; got {actions}"
                    )
            # disabled must still keep HTF bias even with flag false
            disabled_fb = _det_fallback_pre_risk(
                llm_status="disabled", with_plan=False
            )
            mock_cfg = MagicMock()
            mock_cfg.trading_mode = {
                "risk": {"fallback_llm_failed_blocks_paper_order": False}
            }
            with patch(
                "plugins.crypto_guard.risk.risk_engine.load_config",
                return_value=mock_cfg,
            ):
                disabled_out = apply_risk_to_decision(disabled_fb, snapshot)
            assert str(disabled_out.get("market_bias") or "").lower() == "bullish", (
                f"P2-R1: disabled must keep HTF bias; got "
                f"{disabled_out.get('market_bias')!r}"
            )
        finally:
            handle.close()