# -*- coding: utf-8 -*-
"""07-31 production fix P0-1 (2026-07-31): preset candidates must be
consumed BEFORE the breaker gate — RED-first behavioral test + revert-fail.

Production evidence (batch 15m:1785487499999): the fair coordinator ran
13 attempts (8 ok + 5 schema-fail); the schema failures polluted the
breaker rate window (P0-2) and opened the breaker; then every symbol's
preset candidate was DISCARDED by ``run_agent_sop_decision`` because the
breaker gate (llm_agent_judge.py:158-185) runs BEFORE the preset block
(198-238). All 10 persisted rows became breaker_skipped with
provider_call_count=0 — 8 coordinator successes destroyed.

Fix: in ``run_agent_sop_decision``, consume the fair coordinator's
``llm_preset_candidate`` / ``llm_preset_attempt_meta`` BEFORE applying
``breaker.should_call``:

- preset candidate non-None: return the candidate via the risk gate (the
  coordinator already ran the provider call + recorded breaker events —
  the controller path must NOT re-call the provider, re-record the
  breaker, or double-count; ``record_skip`` must NOT fire for a symbol
  the coordinator already processed).
- preset candidate None: keep the coordinator's REAL terminal reason in
  the §8 envelope (symbol_timeout / breaker_skipped / budget violation /
  single_flight_skipped ...) instead of overwriting it with
  breaker_skipped.
- legacy path (no preset in context): stays breaker-gated exactly as
  before.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision
from plugins.crypto_guard.reasoning.llm_breaker import CircuitBreaker

_ANALYSIS_TIME_UTC = 1785487499999


def _snapshot() -> dict:
    """Healthy closed-TF snapshot (mirrors the 07-27 test helper) so no
    data_incomplete fail-closed path fires and the risk gate passes through."""
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


def _preset_candidate() -> dict:
    """A schema-valid coordinator success candidate (monitor_only / B grade,
    no plan — the shape that must survive the open breaker)."""
    return {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": "monitor_only",
        "signal_grade": "B",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.5,
        "summary": "SOL 观察.",
        "evidence": ["1H 反弹"],
        "counter_evidence": ["1D 仍下行"],
        "risk_notes": ["LLM 候选"],
        "has_trade_plan": False,
        "opportunity_watch": None,
        "suggested_actions": ["monitor_only"],
        "llm_status": "ok",
        "llm_terminal_reason": None,
        "llm_fallback_reason": None,
        "llm_attempt_count": 1,
        "llm_provider_call_count": 1,
        "llm_latency_ms": 1200,
        "plan_origin": "deterministic_sop",
        "plan_execution_state": "no_candidate",
    }


def _open_breaker() -> CircuitBreaker:
    """A real CircuitBreaker in open state (3 consecutive transport
    failures — the P0-2 driving path)."""
    b = CircuitBreaker(
        enabled=True,
        consecutive_threshold=3,
        rate_threshold=0.5,
        rate_window=10,
        min_rate_samples=5,
    )
    for _ in range(3):
        b.record_attempt(category="llm_transport_error", ok=False)
    assert b.state == "open"
    return b


class TestPresetConsumedBeforeBreakerGate:
    """P0-1 RED: open breaker + valid preset candidate -> the candidate must
    survive. Pre-fix the breaker gate discards it and returns a
    breaker_skipped deterministic fallback."""

    def test_open_breaker_preset_candidate_survives(self) -> None:
        breaker = _open_breaker()
        candidate = _preset_candidate()
        ctx = {
            "llm_breaker": breaker,
            "llm_preset_candidate": candidate,
            "llm_preset_attempt_meta": {
                "llm_status": "ok",
                "llm_terminal_reason": None,
                "llm_fallback_reason": None,
                "llm_attempt_count": 1,
                "llm_provider_call_count": 1,
            },
        }
        decision = run_agent_sop_decision(_snapshot(), use_llm=True, context=ctx)

        assert decision.get("llm_status") == "ok", (
            "P0-1 RED: an open breaker must NOT convert a coordinator success "
            f"into a failure; got llm_status={decision.get('llm_status')!r}"
        )
        assert decision.get("llm_terminal_reason") != "breaker_skipped", (
            "P0-1 RED: preset success must not be rewritten to breaker_skipped"
        )
        assert decision.get("decision") == "monitor_only", (
            "P0-1 RED: the preset candidate decision must survive; got "
            f"{decision.get('decision')!r}"
        )
        assert decision.get("llm_provider_call_count") == 1, (
            "the coordinator's single provider call must not be re-counted"
        )

    def test_open_breaker_preset_path_no_skip_recorded(self) -> None:
        # The coordinator already recorded the skip for its own skipped
        # symbols; the controller preset path must not record AGAIN.
        breaker = _open_breaker()
        ctx = {
            "llm_breaker": breaker,
            "llm_preset_candidate": _preset_candidate(),
            "llm_preset_attempt_meta": {"llm_status": "ok"},
        }
        run_agent_sop_decision(_snapshot(), use_llm=True, context=ctx)
        snap = breaker.snapshot()
        assert snap["skipped_by_breaker"] == 0, (
            "P0-1 RED: preset path must not call breaker.record_skip(); "
            f"skipped_by_breaker={snap['skipped_by_breaker']}"
        )
        assert snap["total_attempts"] == 3, (
            "preset path must not re-record attempts (the coordinator owns "
            "breaker records); total_attempts=%d" % snap["total_attempts"]
        )

    def test_preset_none_keeps_coordinator_terminal_reason(self) -> None:
        # The coordinator's terminal outcome was symbol_timeout (deadline
        # exhausted) — the controller must preserve that real reason, NOT
        # overwrite it with breaker_skipped just because the breaker is open.
        breaker = _open_breaker()
        ctx = {
            "llm_breaker": breaker,
            "llm_preset_candidate": None,
            "llm_preset_attempt_meta": {
                "llm_status": "failed",
                "llm_terminal_reason": "symbol_timeout",
                "llm_fallback_reason": "symbol_timeout",
                "llm_attempt_count": 1,
                "llm_provider_call_count": 1,
                "llm_latency_ms": 90000,
            },
        }
        decision = run_agent_sop_decision(_snapshot(), use_llm=True, context=ctx)

        assert decision.get("llm_terminal_reason") == "symbol_timeout", (
            "P0-1 RED: preset-None must preserve the coordinator's real "
            f"terminal reason; got {decision.get('llm_terminal_reason')!r}"
        )
        assert decision.get("llm_fallback_reason") == "symbol_timeout", (
            f"got {decision.get('llm_fallback_reason')!r}"
        )
        assert decision.get("llm_status") == "failed"
        # Fail-closed direction (07-27 requirement C) still applies.
        assert decision.get("market_bias") == "unknown", (
            "failed path must stay direction-fail-closed (07-27 C)"
        )

    def test_closed_breaker_preset_still_works(self) -> None:
        # Regression guard: with a closed breaker the preset path is the
        # normal fair path and must work unchanged.
        breaker = CircuitBreaker(
            enabled=True, consecutive_threshold=3,
            rate_threshold=0.5, rate_window=10, min_rate_samples=5,
        )
        ctx = {
            "llm_breaker": breaker,
            "llm_preset_candidate": _preset_candidate(),
            "llm_preset_attempt_meta": {"llm_status": "ok"},
        }
        decision = run_agent_sop_decision(_snapshot(), use_llm=True, context=ctx)
        assert decision.get("llm_status") == "ok"
        assert decision.get("decision") == "monitor_only"


class TestLegacyPathStaysBreakerGated:
    """P0-1: WITHOUT a preset (legacy serial path) the breaker gate must
    keep working exactly as before."""

    def test_open_breaker_legacy_path_skips(self) -> None:
        breaker = _open_breaker()
        ctx = {"llm_breaker": breaker}  # no preset keys
        decision = run_agent_sop_decision(_snapshot(), use_llm=True, context=ctx)

        assert decision.get("llm_status") == "failed"
        assert decision.get("llm_terminal_reason") == "breaker_skipped", (
            "legacy path must stay breaker-gated; got "
            f"{decision.get('llm_terminal_reason')!r}"
        )
        assert decision.get("llm_fallback_reason") == "circuit_breaker_open"
        assert decision.get("llm_provider_call_count") == 0
        assert breaker.snapshot()["skipped_by_breaker"] == 1, (
            "legacy breaker gate must record exactly one skip"
        )
        # 07-27 requirement C fail-closed direction still applies.
        assert decision.get("market_bias") == "unknown"
        assert decision.get("llm_error") == "circuit breaker open; LLM call skipped"
