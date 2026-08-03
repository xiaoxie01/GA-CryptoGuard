# -*- coding: utf-8 -*-
"""08-02 P0-1: dedicated raw deterministic reference in the fair adapter.

Audit: ``fair_llm_call_adapter`` pollutes the LLM prompt's
``deterministic_reference`` section by building it from
``run_agent_sop_decision(snapshot, use_llm=False)``. That disabled path sets
``llm_status="disabled"`` / ``llm_terminal_reason="llm_disabled"`` /
``plan_execution_state`` and then ``apply_risk_to_decision`` writes an
``llm_disabled`` blocker + ``plan_status="withheld"`` +
``fallback_trade_plan_blocked=True`` and clears the trade plan. The LLM then
reads "deterministic engine was disabled and its plan withheld" — destroying
the raw deterministic S/A reference the prompt is meant to ground on.

RED-first (prompt-capture): a real fair-adapter call must produce a prompt
whose ``deterministic_reference`` is built from the clean raw SOP
(``run_ga_sop_decision``): no llm_* markers, no llm_disabled blocker, plan
preserved as executable, candidate/trade plan / grade / confidence / direction
intact. Revert-fail: restoring the old ``use_llm=False`` fallback re-pollutes
and flips the GREEN assertions RED (control test proves the old path is
polluted exactly as described).
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning import llm_agent_judge
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision

_AT_MS = 1_783_641_599_999

# Keys that MUST NOT appear on the raw deterministic reference. These are the
# markers the old ``use_llm=False`` fallback path / risk fallback-block inject.
_FORBIDDEN_KEYS = frozenset({
    "llm_status",
    "llm_terminal_reason",
    "llm_fallback_reason",
    "llm_attempt_count",
    "llm_provider_call_count",
    "llm_latency_ms",
    "llm_model",
    "llm_prompt_bytes",
    "llm_continuity_included",
    "llm_schedule_round",
    "llm_schedule_position",
    "plan_execution_state",
    "fallback_trade_plan_blocked",
    "fallback_block_reason",
    "llm_fallback_blocked",
    "analysis_source",
})


def _build_snapshot(*, symbol: str, bias: str, stage: str, structure: str,
                    momentum_dir: str, candles_count: int) -> dict:
    from plugins.crypto_guard.tests.test_smoke import (
        TestPhaseA07_05BaselineFailures,
    )
    helper = TestPhaseA07_05BaselineFailures("__init__")
    return helper._phase_a_helper_build_snapshot(
        symbol=symbol, analysis_time_ms=_AT_MS, bias=bias, stage=stage,
        structure=structure, momentum_dir=momentum_dir,
        candles_count=candles_count,
    )


def _bullish_snapshot() -> dict:
    """Deterministic S/A fixture: raw SOP yields a LONG trade plan at
    ``plan_status="executable"`` (verified: S grade / 0.95 confidence /
    entry 100.0 / stop 95.0)."""
    return _build_snapshot(symbol="ADAUSDT", bias="bullish", stage="middle",
                           structure="bullish", momentum_dir="bullish",
                           candles_count=250)


def _neutral_snapshot() -> dict:
    """Deterministic no-plan fixture: raw SOP yields ``no_plan`` with no trade
    plan (verified: monitor_only / C grade / has_trade_plan=False)."""
    return _build_snapshot(symbol="DOGEUSDT", bias="neutral", stage="range",
                           structure="range", momentum_dir="neutral",
                           candles_count=200)


def _fake_no_plan_call(symbol: str) -> callable:
    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "monitor_only",
            "signal_grade": "C",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.45,
            "summary": f"{symbol} no edge",
            "evidence": ["no edge"],
            "counter_evidence": ["no edge"],
            "risk_notes": [],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["ignore"],
            "timeframe_context": {
                tf: {"bias": "unknown", "structure": "unknown", "closed": True,
                     "close_time": _AT_MS - offset}
                for tf, offset in (("1d", 86_400_000), ("4h", 14_400_000),
                                   ("1h", 3_600_000), ("15m", 900_000))
            },
            "alignment": "unknown",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _capture_adapter_reference(snapshot: dict) -> tuple[dict, dict, dict]:
    """Call ``fair_llm_call_adapter`` exactly as the fair coordinator does and
    capture the ``deterministic_decision`` argument the real
    ``build_llm_decision_prompt`` receives (the prompt's deterministic
    reference). Returns ``(reference, candidate, attempt_meta)``."""
    from plugins.crypto_guard.reasoning.llm_breaker import (
        CircuitBreaker, PerSymbolDeadline,
    )
    from plugins.crypto_guard.reasoning.llm_fair_scheduler import (
        resolve_fair_batch_config,
    )
    clock = {"t": 0}

    def fake_now_ms():
        return clock["t"]

    with mock.patch("plugins.crypto_guard.reasoning.llm_breaker._now_ms",
                    side_effect=fake_now_ms):
        deadline = PerSymbolDeadline(
            per_symbol_timeout_seconds=300,
            per_attempt_timeout_seconds=180,
        )
    breaker = CircuitBreaker(enabled=True, consecutive_threshold=3,
                             rate_threshold=0.5, rate_window=10,
                             min_rate_samples=5)
    breaker.llm_config_name = "test_cfg"
    breaker.llm_model = "test_model"
    cfg = resolve_fair_batch_config({
        "scheduling": {"mode": "fair_pool", "max_concurrency": 4,
                       "per_symbol_timeout_seconds": 300,
                       "per_attempt_timeout_seconds": 180,
                       "batch_completion_guard_seconds": 60,
                       "rotate_start_symbol": True},
        "retry": {"max_attempts_per_symbol": 3},
    })

    captured: dict = {}
    orig_build = llm_agent_judge.build_llm_decision_prompt

    def spy(snapshot_, deterministic_decision, *, context=None):
        captured["deterministic_reference"] = deterministic_decision
        return orig_build(snapshot_, deterministic_decision, context=context)

    with mock.patch.object(llm_agent_judge, "_call_ga_llm",
                           side_effect=_fake_no_plan_call(snapshot["symbol"])), \
            mock.patch.object(llm_agent_judge, "build_llm_decision_prompt",
                              side_effect=spy):
        candidate, attempt_meta = llm_agent_judge.fair_llm_call_adapter(
            snapshot=snapshot, deadline=deadline, breaker=breaker,
            retry_budget=cfg, wall_clock_budget=cfg,
            attempt=1, max_attempts=cfg.max_attempts_per_symbol,
            schedule_position=0, schedule_round=1, context=None,
        )
    assert "deterministic_reference" in captured, (
        "build_llm_decision_prompt must be reached on the fair-adapter path; "
        f"candidate={candidate} meta={attempt_meta}"
    )
    return captured["deterministic_reference"], candidate, attempt_meta


def _blocker_codes(decision: dict) -> list[str]:
    blockers = decision.get("plan_blockers") or []
    return [str(b.get("code") or "") for b in blockers if isinstance(b, dict)]


class TestFairLlmRawReferenceP0_1:
    def test_p0_1_executable_reference_unpolluted_and_preserved(self) -> None:
        """GREEN: with a raw deterministic S/A the prompt's deterministic
        reference must be the CLEAN raw SOP — plan executable, no llm_disabled
        markers, candidate/trade plan / grade / confidence / direction kept."""
        reference, candidate, attempt_meta = _capture_adapter_reference(
            _bullish_snapshot())
        assert attempt_meta.get("llm_status") == "ok", attempt_meta

        for key in sorted(_FORBIDDEN_KEYS):
            assert key not in reference, (
                f"raw deterministic reference must not carry {key}: "
                f"{reference.get(key)!r}")

        assert "llm_disabled" not in _blocker_codes(reference), (
            "reference must not carry the llm_disabled fallback blocker; got "
            f"{reference.get('plan_blockers')!r}")
        assert reference.get("plan_status") == "executable", (
            f"plan must stay executable, not withheld; got "
            f"{reference.get('plan_status')!r}")
        assert reference.get("plan_origin") == "deterministic_sop", (
            f"got {reference.get('plan_origin')!r}")
        assert reference.get("decision") == "trade_plan_available", (
            f"got {reference.get('decision')!r}")
        assert reference.get("has_trade_plan") is True
        tp = reference.get("trade_plan") or {}
        assert isinstance(tp, dict) and tp.get("side") == "LONG", tp
        assert tp.get("entry_price") and tp.get("stop_loss"), (
            f"entry/stop must survive; got {tp!r}")
        assert reference.get("candidate_trade_plan") == tp, (
            "candidate_trade_plan must equal the preserved trade_plan")
        assert reference.get("signal_grade") in ("S", "A"), (
            f"grade must stay S/A; got {reference.get('signal_grade')!r}")
        assert float(reference.get("confidence") or 0) >= 0.72, (
            f"confidence must survive; got {reference.get('confidence')!r}")
        assert reference.get("market_bias") == "bullish", (
            f"got {reference.get('market_bias')!r}")
        assert isinstance(reference.get("evidence"), list) and (
            reference.get("evidence")), "structural evidence must be kept"

    def test_p0_1_no_plan_reference_unpolluted(self) -> None:
        """GREEN: with no raw deterministic plan the reference is a clean
        ``no_plan`` — never a disabled/withheld fallback."""
        reference, candidate, attempt_meta = _capture_adapter_reference(
            _neutral_snapshot())
        assert attempt_meta.get("llm_status") == "ok", attempt_meta

        for key in sorted(_FORBIDDEN_KEYS):
            assert key not in reference, (
                f"raw deterministic reference must not carry {key}: "
                f"{reference.get(key)!r}")
        assert reference.get("plan_status") == "no_plan", (
            f"got {reference.get('plan_status')!r}")
        assert reference.get("plan_origin") == "deterministic_sop", (
            f"got {reference.get('plan_origin')!r}")
        assert reference.get("has_trade_plan") is False

    def test_p0_1_revert_fail_old_use_llm_false_reference_is_polluted(
            self) -> None:
        """Revert-fail control: the OLD fallback path (
        ``run_agent_sop_decision(use_llm=False)``) IS polluted exactly as the
        audit describes. If the adapter ever reverts to it, the two GREEN
        tests above flip RED — proving their assertions are load-bearing."""
        snapshot = _bullish_snapshot()
        old = run_agent_sop_decision(snapshot, use_llm=False)
        assert old.get("llm_status") == "disabled", old.get("llm_status")
        assert old.get("llm_terminal_reason") == "llm_disabled"
        assert old.get("llm_fallback_reason") == "llm_disabled"
        assert old.get("plan_execution_state") in (
            "confirmed", "no_candidate", "unconfirmed"), (
            f"got {old.get('plan_execution_state')!r}")
        assert old.get("plan_status") == "withheld", (
            f"got {old.get('plan_status')!r}")
        assert old.get("fallback_trade_plan_blocked") is True
        assert "llm_disabled" in _blocker_codes(old), (
            "old path must carry the llm_disabled blocker; got "
            f"{old.get('plan_blockers')!r}")
