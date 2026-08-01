# -*- coding: utf-8 -*-
"""07-31 production fix P1-1 (2026-07-31): decision emitted as ARRAY
(production evidence #1: "SOL/ETH 多次把 decision 字符串错误输出成数组")
— RED-first behavioral test + revert-fail.

Root cause: models sometimes emit ``"decision": ["monitor_only"]`` (a
single-element array) or ``"decision": ["trade_plan_available",
"wait_for_pullback"]`` (multi-element). The schema requires a flat string
enum and must NOT be loosened (requirement D): the repair must happen in
``_try_repair_decision``, which runs FIRST in the schema-repair chain
(BEFORE suggested_actions repair).

Repair contract (P1-1):
- single-element array whose value is a legal enum value -> collapse to
  the bare string (safe, no semantic loss).
- multi-element array of legal enum values -> semantic ambiguity:
  conservative repair to ``monitor_only``, cancel any executable
  trade_plan, ``has_trade_plan=False``, grade capped at B,
  ``suggested_actions=["monitor_only"]``.
- illegal value / empty array / mixed types / unknown shapes -> do NOT
  repair (fail-closed; the row keeps the hard schema failure).
- audit: ``original_decision`` + ``decision_repaired=True`` land in
  ``llm_parse_meta``; ONE physical success + ONE repair, no extra
  provider call.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _try_repair_decision,
    _run_single_llm_attempt,
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
    run_agent_sop_decision,
)
from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker

_ANALYSIS_TIME_UTC = 1785487499999

_LEGAL_DECISIONS = (
    "trade_plan_available", "wait_for_pullback", "wait_for_breakout",
    "wait_for_reclaim", "avoid_chop", "no_edge", "monitor_only",
)


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


def _decision(**overrides) -> dict:
    tf_ctx = {
        tf: {"bias": "bullish", "structure": "bullish", "closed": True, "close_time": _ANALYSIS_TIME_UTC - 60_000}
        for tf in ("1d", "4h", "1h", "15m")
    }
    d = {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": "monitor_only",
        "signal_grade": "B",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.5,
        "summary": "观察.",
        "evidence": ["1H 反弹"],
        "counter_evidence": ["1D 仍下行"],
        "risk_notes": [],
        "has_trade_plan": False,
        "opportunity_watch": None,
        "suggested_actions": ["monitor_only"],
        "timeframe_context": tf_ctx,
        "alignment": "neutral",
        "htf_conflict": False,
        "market_reason_codes": [],
    }
    d.update(overrides)
    return d


def _with_plan(**overrides) -> dict:
    d = _decision(
        decision="trade_plan_available",
        signal_grade="A",
        has_trade_plan=True,
        trade_plan={
            "side": "LONG",
            "entry_type": "limit",
            "entry_price": 180.0,
            "stop_loss": 172.0,
            "take_profits": [{"price": 196.0, "ratio": 1.0}],
            "invalid_condition": "1H 跌破 170",
        },
        suggested_actions=["create_paper_order"],
    )
    d.update(overrides)
    return d


class TestTryRepairDecision:
    """P1-1 unit contract for the new repair function."""

    def test_single_legal_element_collapses(self) -> None:
        d = _decision(decision=["monitor_only"])
        repaired, notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is True, "single legal element must be repaired"
        assert repaired["decision"] == "monitor_only", (
            f"single-element array must collapse to the bare string; got "
            f"{repaired['decision']!r}"
        )
        ok, err = validate_json("ga_decision.schema.json", repaired)
        assert ok, f"collapsed decision must be schema-valid; {err}"

    def test_single_legal_trade_plan_available_collapses_keeps_plan(self) -> None:
        d = _with_plan(decision=["trade_plan_available"])
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is True
        assert repaired["decision"] == "trade_plan_available"
        assert repaired["has_trade_plan"] is True
        assert isinstance(repaired.get("trade_plan"), dict), (
            "single-element fold must not cancel a confirmed plan"
        )
        ok, err = validate_json("ga_decision.schema.json", repaired)
        assert ok, f"{err}"

    def test_every_legal_value_collapses(self) -> None:
        for value in _LEGAL_DECISIONS:
            d = _decision(decision=[value])
            repaired, _notes, changed = _try_repair_decision(d, _snapshot())
            assert changed is True, f"[{value}] must collapse"
            assert repaired["decision"] == value

    def test_multi_legal_elements_conservative_downgrade(self) -> None:
        d = _with_plan(decision=["trade_plan_available", "wait_for_pullback"])
        repaired, notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is True, "multi-element ambiguity must be repaired conservatively"
        assert repaired["decision"] == "monitor_only", (
            "multi-element arrays must conservatively downgrade to monitor_only"
        )
        assert repaired["has_trade_plan"] is False, (
            "multi-element ambiguity must cancel the executable trade_plan"
        )
        assert repaired.get("trade_plan") is None
        assert repaired["signal_grade"] in ("B", "C", "D"), (
            "grade must be capped at B (never S/A)"
        )
        assert repaired["suggested_actions"] == ["monitor_only"]
        assert notes, "conservative downgrade must emit an audit note"
        ok, err = validate_json("ga_decision.schema.json", repaired)
        assert ok, f"conservative downgrade must stay schema-valid; {err}"

    def test_illegal_value_fails_closed(self) -> None:
        d = _decision(decision=["banana"])
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is False, "illegal enum value must NOT be repaired (fail-closed)"
        ok, _err = validate_json("ga_decision.schema.json", d)
        assert ok is False, "the original must remain schema-invalid"

    def test_empty_array_fails_closed(self) -> None:
        d = _decision(decision=[])
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is False

    def test_mixed_types_fail_closed(self) -> None:
        d = _decision(decision=["monitor_only", 123])
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is False

    def test_bare_string_untouched(self) -> None:
        d = _decision(decision="monitor_only")
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is False
        assert repaired is d

    def test_missing_decision_untouched(self) -> None:
        d = _decision()
        del d["decision"]
        repaired, _notes, changed = _try_repair_decision(d, _snapshot())
        assert changed is False


class TestRepairChainIntegration:
    """P1-1 chain integration through the real single-attempt unit with the
    provider boundary patched (``_call_ga_llm`` is the provider call)."""

    def _run_attempt(self, raw_json: str) -> tuple[dict | None, dict]:
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

    def _raw(self, **overrides) -> str:
        payload = {
            "symbol": "SOLUSDT",
            "analysis_time_utc": _ANALYSIS_TIME_UTC,
            "decision": "monitor_only",
            "signal_grade": "B",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.5,
            "summary": "观察.",
            "evidence": ["1H 反弹"],
            "counter_evidence": ["1D 仍下行"],
            "has_trade_plan": False,
            "opportunity_watch": None,
            "suggested_actions": ["monitor_only"],
        }
        payload.update(overrides)
        return json.dumps(payload, ensure_ascii=False)

    def test_single_element_array_repaired_success(self) -> None:
        candidate, meta = self._run_attempt(
            self._raw(decision=["wait_for_pullback"])
        )
        assert candidate is not None, (
            "single-element decision array must be REPAIRED to a success, "
            f"not a hard failure; meta={meta}"
        )
        assert meta.get("llm_terminal_reason") == "schema_repaired"
        assert meta.get("llm_repair_event") is True
        assert meta.get("llm_status") == "ok"
        assert meta.get("llm_error") is None
        assert candidate["decision"] == "wait_for_pullback", (
            f"got {candidate['decision']!r}"
        )
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("decision_repaired") is True, (
            "audit flag decision_repaired must be set; "
            f"parse_meta={parse_meta}"
        )
        assert parse_meta.get("original_decision") == ["wait_for_pullback"], (
            "audit original_decision must preserve the raw array"
        )
        # ONE physical provider call: provider_call_count stays 1.
        assert meta.get("llm_provider_call_count") == 1
        ok, err = validate_json("ga_decision.schema.json", candidate)
        assert ok, f"repaired decision must be schema-valid; {err}"

    def test_multi_element_array_conservative_success(self) -> None:
        candidate, meta = self._run_attempt(
            self._raw(
                decision=["trade_plan_available", "wait_for_breakout"],
                signal_grade="A",
                has_trade_plan=True,
                trade_plan={
                    "side": "LONG",
                    "entry_type": "limit",
                    "entry_price": 180.0,
                    "stop_loss": 172.0,
                    "take_profits": [{"price": 196.0, "ratio": 1.0}],
                    "invalid_condition": "1H 跌破 170",
                },
                suggested_actions=["create_paper_order"],
            )
        )
        assert candidate is not None, (
            "multi-element decision array must be conservatively repaired "
            f"to a success; meta={meta}"
        )
        assert meta.get("llm_terminal_reason") == "schema_repaired"
        assert candidate["decision"] == "monitor_only"
        assert candidate["has_trade_plan"] is False
        assert candidate.get("trade_plan") is None
        assert candidate["signal_grade"] in ("B", "C", "D")
        assert candidate["suggested_actions"] == ["monitor_only"]
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("decision_repaired") is True
        assert meta.get("llm_provider_call_count") == 1

    def test_illegal_array_stays_hard_failure(self) -> None:
        candidate, meta = self._run_attempt(self._raw(decision=["banana"]))
        assert candidate is None, "illegal enum value must stay fail-closed"
        assert meta.get("llm_terminal_reason") == "llm_schema_validation_failed"
        assert meta.get("llm_repair_event") is not True

    def test_string_decision_plain_success_no_repair_flag(self) -> None:
        candidate, meta = self._run_attempt(
            self._raw(decision="wait_for_pullback")
        )
        assert candidate is not None
        assert meta.get("llm_terminal_reason") in (None, "ok"), (
            f"plain string decision must NOT be flagged schema_repaired; "
            f"got {meta.get('llm_terminal_reason')!r}"
        )
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("decision_repaired") is not True
