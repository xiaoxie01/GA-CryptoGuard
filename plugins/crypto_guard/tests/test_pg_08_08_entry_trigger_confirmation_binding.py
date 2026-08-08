# -*- coding: utf-8 -*-
"""08-08 P1-2 (PRD): trusted ``entry_trigger_confirmation`` binding.

The LLM must never generate/overwrite/forge ``trade_plan.entry_trigger_confirmation``.
When the LLM confirms a same-direction plan and the deterministic candidate
already has a legal closed-candle confirmation, ``_normalize_llm_decision``
binds the TRUSTED confirmation BEFORE schema/risk validation — but ONLY after
validating symbol/side consistency, entry/trigger geometry compatibility, and
real snapshot/module provenance. The LLM's own confirmation output is
unconditionally ignored: when no trusted candidate confirmation survives the
checks, ANY LLM-provided value (bare string OR forged object) is cleared to
None so the risk gate correctly reports "缺少入场确认" instead of trusting a
fabricated value.

RED-first: pre-fix, an LLM-provided plan that omits ``entry_trigger_confirmation``
stays without it (the production 55/55 funnel drop — risk deems it missing),
and an LLM-forged OBJECT confirmation survives (the old BTC#9 block only cleared
bare strings). Both are proven RED below.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.llm_agent_judge import _normalize_llm_decision

_ANALYSIS = 1_700_000_100_000
_CLOSE = 1_700_000_000_000


def _event(**overrides: dict) -> dict:
    event = {
        "event": "bullish_bos",
        "timeframe": "15m",
        "direction": "bullish",
        "candle_close_time": _CLOSE,
        "price": 60000.0,
        "closed": True,
    }
    event.update(overrides)
    return event


def _snapshot() -> dict:
    """A healthy BTCUSDT snapshot whose ``modules.price_action`` carries a
    legal closed-candle bullish BOS event — the real provenance source for the
    trusted confirmation. ``data_quality.health_by_tf`` keeps
    ``normalize_market_semantics`` from firing ``data_incomplete``."""
    at = _ANALYSIS
    health = {
        tf: {"ready": True, "last_close_time": at - 60_000}
        for tf in ("1d", "4h", "1h", "15m")
    }
    profiles = {
        tf: {"market_structure": "bullish", "momentum": "bullish"}
        for tf in ("1d", "4h", "1h", "15m")
    }
    return {
        "symbol": "BTCUSDT",
        "analysis_time_utc": at,
        "profiles": profiles,
        "modules": {
            "price_action": {"structure_events": [_event()]},
            "momentum": {"direction": "bullish"},
        },
        "data_quality": {"health_by_tf": health},
    }


def _trusted_confirmation() -> dict:
    """The exact confirmation ``_extract_structured_entry_confirmation``
    produces from ``_snapshot()`` for a LONG entry near 60000."""
    return {
        "type": "closed_candle_confirmation",
        "timeframe": "15m",
        "event_type": "BOS",
        "direction": "bullish",
        "candle_close_time": _CLOSE,
        "price": 60000.0,
        "source": "price_action",
        "symbol": "BTCUSDT",
    }


def _candidate_plan(*, confirmation: dict | None = None, **overrides: dict) -> dict:
    """A deterministic LONG candidate plan (what ``_build_trade_plan`` would
    produce). ``confirmation=None`` omits the key entirely (the production
    shape where the deterministic candidate HAS the confirmation)."""
    plan = {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 60050.0,
        "trigger_price": 60050.0,
        "stop_loss": 59900.0,
        "take_profits": [{"price": 60400.0, "ratio": 0.5}, {"price": 60600.0, "ratio": 0.5}],
        "risk_percent": 0.5,
        "invalid_condition": "15m 收盘跌破 59800",
        "reason": "结构偏多，等待回踩确认；仅用于模拟盘",
    }
    if confirmation is not None:
        plan["entry_trigger_confirmation"] = confirmation
    plan.update(overrides)
    return plan


def _fallback(*, candidate_confirmation: dict | None = None, has_candidate: bool = True) -> dict:
    """Deterministic disabled-path fallback. ``candidate_confirmation=None``
    means the candidate plan omits the confirmation (no trusted source)."""
    plan = _candidate_plan(confirmation=candidate_confirmation)
    base = {
        "symbol": "BTCUSDT",
        "analysis_time_utc": _ANALYSIS,
        "signal_grade": "A",
        "confidence": 0.82,
        "market_bias": "bullish",
        "decision": "trade_plan_available",
        "has_trade_plan": True,
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
        "risk_notes": [],
        "analysis_source": "deterministic_sop",
        "llm_status": "disabled",
        "plan_origin": "deterministic_sop",
        "trade_plan": plan,
    }
    if has_candidate:
        base["candidate_trade_plan"] = plan
    return base


def _llm_plan(**overrides: dict) -> dict:
    """An LLM-confirmed LONG plan. By default it OMITS
    ``entry_trigger_confirmation`` (the production 55/55 shape)."""
    plan = {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 60050.0,
        "trigger_price": 60050.0,
        "stop_loss": 59900.0,
        "take_profits": [{"price": 60400.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "15m 收盘跌破 59800",
        "reason": "强势看涨",
    }
    plan.update(overrides)
    return plan


def _llm_candidate(*, trade_plan: dict) -> dict:
    return {
        "symbol": "BTCUSDT",
        "analysis_time_utc": _ANALYSIS,
        "decision": "trade_plan_available",
        "signal_grade": "A",
        "market_bias": "bullish",
        "trend_stage": "early",
        "confidence": 0.82,
        "summary": "BTC 强势看涨",
        "evidence": ["15m 突破"],
        "counter_evidence": [],
        "risk_notes": [],
        "has_trade_plan": True,
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
        "trade_plan": trade_plan,
    }


def _result_confirmation(decision: dict):
    tp = decision.get("trade_plan")
    if not isinstance(tp, dict):
        return None
    return tp.get("entry_trigger_confirmation")


class TestBindTrustedEntryConfirmation:
    """P1-2 positive: the trusted deterministic confirmation is bound into the
    LLM-confirmed same-direction plan BEFORE risk validation."""

    def test_llm_omits_confirmation_candidate_has_trusted(self) -> None:
        """RED: pre-fix, an LLM plan that omits entry_trigger_confirmation stays
        without it (production 55/55 drop). GREEN: the trusted candidate
        confirmation is bound."""
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan()),
            _snapshot(),
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        result = _result_confirmation(decision)
        assert result is not None, (
            "P1-2 RED: LLM-confirmed same-direction plan with a legal trusted "
            "candidate confirmation must bind it; got None"
        )
        assert result == _trusted_confirmation(), (
            f"P1-2: bound confirmation must equal the trusted deterministic "
            f"one; got {result!r}"
        )

    def test_llm_overwrites_with_own_object_trusted_wins(self) -> None:
        """RED: pre-fix, an LLM-forged OBJECT confirmation survives (only bare
        strings were cleared). GREEN: the trusted candidate confirmation
        overwrites the LLM's object."""
        llm_forged = {
            "type": "closed_candle_confirmation",
            "timeframe": "15m",
            "event_type": "BOS",
            "direction": "bullish",
            "candle_close_time": _CLOSE,
            "price": 60000.0,
            "source": "price_action",
            "symbol": "BTCUSDT",
            "llm_injected": True,
        }
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        result = _result_confirmation(decision)
        assert result is not None, "P1-2: trusted confirmation must be bound"
        assert result == _trusted_confirmation(), (
            f"P1-2: the trusted deterministic confirmation must overwrite the "
            f"LLM's object; got {result!r}"
        )
        assert result.get("llm_injected") is None, (
            "P1-2: the LLM's forged field must not survive the overwrite"
        )


class TestBindFailClosedNoTrustedCandidate:
    """P1-2 fail-closed: when there is NO trusted candidate confirmation, ANY
    LLM-provided value (bare string OR forged object) is cleared to None."""

    def test_llm_forged_object_no_candidate_cleared(self) -> None:
        """RED: pre-fix, an LLM-forged OBJECT confirmation survives (the old
        BTC#9 block only cleared bare strings). GREEN: cleared to None."""
        llm_forged = {
            "type": "closed_candle_confirmation",
            "timeframe": "15m",
            "event_type": "BOS",
            "direction": "bullish",
            "candle_close_time": _CLOSE,
            "price": 60000.0,
            "source": "price_action",
            "symbol": "BTCUSDT",
        }
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=None),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2 RED: an LLM-forged object confirmation with no trusted "
            "candidate must be cleared to None"
        )

    def test_llm_forged_object_no_candidate_plan_cleared(self) -> None:
        """No ``candidate_trade_plan`` at all — the LLM's forged object is
        cleared (never trusted)."""
        llm_forged = {
            "type": "closed_candle_confirmation",
            "timeframe": "15m",
            "event_type": "BOS",
            "direction": "bullish",
            "candle_close_time": _CLOSE,
            "price": 60000.0,
            "source": "price_action",
            "symbol": "BTCUSDT",
        }
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=None, has_candidate=False),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: LLM-forged object with no candidate plan must be cleared"
        )


class TestBindFailClosedInvalidTrusted:
    """P1-2 fail-closed: when the trusted candidate confirmation fails a
    validation (symbol/side/geometry/provenance), the LLM's own confirmation is
    cleared to None — never trusted."""

    def test_cross_symbol_candidate_confirmation_cleared(self) -> None:
        """The trusted candidate confirmation's symbol does not match the
        decision symbol → binding fails → the LLM's object is cleared."""
        cross = {**_trusted_confirmation(), "symbol": "ETHUSDT"}
        llm_forged = {**_trusted_confirmation(), "symbol": "BTCUSDT"}
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=cross),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: cross-symbol trusted confirmation must fail binding and "
            "clear the LLM's object"
        )

    def test_direction_flip_candidate_confirmation_cleared(self) -> None:
        """The LLM flips to SHORT while the trusted candidate confirmation is
        bullish → side/direction mismatch → binding fails → cleared."""
        llm_forged = {
            "type": "closed_candle_confirmation",
            "timeframe": "15m",
            "event_type": "BOS",
            "direction": "bearish",
            "candle_close_time": _CLOSE,
            "price": 60000.0,
            "source": "price_action",
            "symbol": "BTCUSDT",
        }
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(side="SHORT", entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: direction-flip plan must not bind the bullish trusted "
            "confirmation; the LLM's object must be cleared"
        )

    def test_geometry_mismatch_candidate_confirmation_cleared(self) -> None:
        """The LLM's entry is 50% away from the confirmation price → geometry
        incompatible → binding fails → the LLM's object is cleared."""
        llm_forged = {**_trusted_confirmation(), "price": 60000.0}
        decision = _normalize_llm_decision(
            _llm_candidate(
                trade_plan=_llm_plan(entry_price=90000.0, trigger_price=90000.0,
                                     entry_trigger_confirmation=llm_forged)
            ),
            _snapshot(),
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: geometry-incompatible entry must fail binding and clear "
            "the LLM's object"
        )

    def test_future_close_time_candidate_confirmation_cleared(self) -> None:
        """The trusted candidate confirmation has a future candle_close_time →
        not re-derivable from the snapshot (provenance fail) → binding fails →
        the LLM's object is cleared."""
        future = {**_trusted_confirmation(), "candle_close_time": _ANALYSIS + 1}
        llm_forged = {**_trusted_confirmation()}
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=future),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: future-leak trusted confirmation must fail provenance and "
            "clear the LLM's object"
        )

    def test_unknown_source_candidate_confirmation_cleared(self) -> None:
        """The trusted candidate confirmation's source is not derivable from
        the snapshot modules → provenance fail → binding fails → cleared."""
        forged_source = {**_trusted_confirmation(), "source": "18h:fake_module"}
        llm_forged = {**_trusted_confirmation()}
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            _snapshot(),
            _fallback(candidate_confirmation=forged_source),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: non-derivable source must fail provenance and clear the "
            "LLM's object"
        )

    def test_unclosed_event_candidate_confirmation_cleared(self) -> None:
        """The snapshot's only price_action event is NOT closed (``closed`` is
        strictly ``False``) → ``_extract_structured_entry_confirmation``
        rejects it (R4-D5 identity check) and returns None → the trusted
        candidate confirmation is not re-derivable (provenance fail) → binding
        fails → the LLM's object is cleared."""
        unclosed_snapshot = _snapshot()
        unclosed_snapshot["modules"]["price_action"]["structure_events"] = [
            _event(closed=False)
        ]
        llm_forged = {**_trusted_confirmation()}
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            unclosed_snapshot,
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: an unclosed-candle event cannot source a trusted "
            "confirmation; the LLM's object must be cleared"
        )

    def test_missing_evidence_candidate_confirmation_cleared(self) -> None:
        """The snapshot's price_action ``structure_events`` are EMPTY → no
        legal event is derivable → ``_extract_structured_entry_confirmation``
        returns None → the trusted candidate confirmation is not re-derivable
        (provenance fail) → binding fails → the LLM's object is cleared."""
        no_evidence_snapshot = _snapshot()
        no_evidence_snapshot["modules"]["price_action"]["structure_events"] = []
        llm_forged = {**_trusted_confirmation()}
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation=llm_forged)),
            no_evidence_snapshot,
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: an empty structure_events cannot source a trusted "
            "confirmation; the LLM's object must be cleared"
        )


class TestLLMBareStringStillCleared:
    """P1-2 regression guard: a bare-string LLM confirmation is never trusted.
    With a valid trusted candidate it is OVERWRITTEN by the trusted value; with
    no trusted candidate it is cleared to None (fail-closed)."""

    def test_llm_bare_string_with_trusted_candidate_bound(self) -> None:
        """A bare-string LLM confirmation is overwritten by the trusted
        deterministic confirmation (never kept as free text)."""
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation="breakout retest")),
            _snapshot(),
            _fallback(candidate_confirmation=_trusted_confirmation()),
        )
        result = _result_confirmation(decision)
        assert result == _trusted_confirmation(), (
            "P1-2: a bare-string LLM confirmation with a valid trusted candidate "
            "must be overwritten by the trusted value"
        )

    def test_llm_bare_string_no_trusted_candidate_cleared(self) -> None:
        """A bare-string LLM confirmation with NO trusted candidate is cleared
        to None (fail-closed — never kept as free text)."""
        decision = _normalize_llm_decision(
            _llm_candidate(trade_plan=_llm_plan(entry_trigger_confirmation="breakout retest")),
            _snapshot(),
            _fallback(candidate_confirmation=None),
        )
        assert _result_confirmation(decision) is None, (
            "P1-2: a bare-string LLM confirmation with no trusted candidate "
            "must be cleared to None"
        )
