# -*- coding: utf-8 -*-
"""07-31 production fix P1-2 (2026-07-31): trade_plan.take_profits
numeric items violating the object schema — RED-first behavioral test +
revert-fail.

Production evidence #2: "另有 trade_plan.take_profits 数字项违反 object
schema". The schema requires ``take_profits`` items to be objects with
``price`` / ``ratio`` numbers (ga_decision.schema.json items -> object);
models sometimes emit bare numbers instead.

Repair contract (P1-2 + 07-31 final review P1-1), via the new
``_try_repair_take_profits`` in the schema-repair chain (after
entry-trigger, alongside decision repair):

- take_profits EXACTLY ``[single finite positive number]`` -> safely
  repaired to ``{"price": <n>, "ratio": 1.0}`` (the number is the target
  price; a SOLE position is unambiguous).
- numbers MIXED with object items, multiple numeric items, non-finite
  (nan/inf), non-positive (0/negative), or non-numeric junk -> MUST NOT
  guess position ratios: conservatively cancel the plan and downgrade to
  ``monitor_only``.
- object contract (final review P1-1): ``price > 0``, ``0 < ratio <= 1``,
  both finite, and the object ratios must sum to ~1.0 — an object outside
  the contract (non-positive / non-finite price or ratio, ratio sum != 1.0)
  downgrades the WHOLE list; never guess missing or overlapping ratios.
- valid object items are untouched; a fully-valid list is not repaired.
- any repair result must still pass the strict schema (no risk-gate
  bypass: the repaired decision is re-validated by the chain before the
  row counts as a success).
"""
from __future__ import annotations

import json
import math
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _try_repair_take_profits,
    _run_single_llm_attempt,
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
    run_agent_sop_decision,
)
from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker

_ANALYSIS_TIME_UTC = 1785487499999


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


def _decision(*, take_profits: object) -> dict:
    tf_ctx = {
        tf: {"bias": "bullish", "structure": "bullish", "closed": True, "close_time": _ANALYSIS_TIME_UTC - 60_000}
        for tf in ("1d", "4h", "1h", "15m")
    }
    return {
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
        "risk_notes": [],
        "has_trade_plan": True,
        "trade_plan": {
            "side": "LONG",
            "entry_type": "limit",
            "entry_price": 180.0,
            "stop_loss": 172.0,
            "take_profits": take_profits,
            "invalid_condition": "1H 跌破 170",
        },
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
        "timeframe_context": tf_ctx,
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
    }


def _assert_downgraded(repaired: dict, notes: list[str], changed: bool) -> None:
    assert changed is True, "unsafe take_profits shape must be repaired"
    assert repaired["decision"] == "monitor_only", (
        "unsafe take_profits must conservatively downgrade to monitor_only"
    )
    assert repaired["has_trade_plan"] is False
    assert repaired.get("trade_plan") is None
    assert repaired["signal_grade"] in ("B", "C", "D"), (
        "grade must be capped at B (never S/A)"
    )
    assert repaired["suggested_actions"] == ["monitor_only"]
    assert notes, "conservative downgrade must emit an audit note"
    ok, err = validate_json("ga_decision.schema.json", repaired)
    assert ok, f"downgrade must stay schema-valid; {err}"


class TestTryRepairTakeProfits:
    """P1-2 unit contract for the new repair function."""

    def test_single_number_repaired_to_object(self) -> None:
        d = _decision(take_profits=[123.45])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        assert changed is True
        assert repaired["trade_plan"]["take_profits"] == [
            {"price": 123.45, "ratio": 1.0}
        ], f"got {repaired['trade_plan']['take_profits']!r}"
        assert repaired["decision"] == "trade_plan_available", (
            "safe single-number repair must NOT downgrade the decision"
        )
        ok, err = validate_json("ga_decision.schema.json", repaired)
        assert ok, f"{err}"

    def test_single_number_plus_valid_objects(self) -> None:
        # 07-31 final review P1-1: numbers MIXED with object items can no
        # longer be guessed into a ratio — the old repair produced a total
        # ratio of 1.5 here (0.5 + guessed 1.0). Conservative downgrade.
        d = _decision(take_profits=[{"price": 100.0, "ratio": 0.5}, 200.0])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_ratio_zero_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 100.0, "ratio": 0.0}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_negative_ratio_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 100.0, "ratio": -0.5}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_ratio_above_one_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 100.0, "ratio": 1.5}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_nan_ratio_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 100.0, "ratio": float("nan")}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_inf_ratio_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 100.0, "ratio": float("inf")}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_zero_price_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": 0.0, "ratio": 1.0}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_negative_price_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": -5.0, "ratio": 1.0}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_nan_price_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": float("nan"), "ratio": 1.0}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_inf_price_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[{"price": float("inf"), "ratio": 1.0}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_ratio_sum_above_one_conservative_downgrade(self) -> None:
        # Total ratio 1.2 > 1: exits overlap the position — unreliable.
        d = _decision(take_profits=[
            {"price": 100.0, "ratio": 0.7},
            {"price": 200.0, "ratio": 0.5},
        ])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_object_ratio_sum_below_one_conservative_downgrade(self) -> None:
        # Total ratio 0.5 < 1: exit coverage is incomplete — cannot be
        # interpreted as a reliable full-position plan.
        d = _decision(take_profits=[{"price": 100.0, "ratio": 0.5}])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_multiple_numbers_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[100.0, 200.0])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_zero_number_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[0.0])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_negative_number_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[-5.0])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_nan_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[float("nan")])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_inf_conservative_downgrade(self) -> None:
        d = _decision(take_profits=[float("inf")])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_bool_not_treated_as_number(self) -> None:
        d = _decision(take_profits=[True])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_string_junk_conservative_downgrade(self) -> None:
        d = _decision(take_profits=["abc"])
        repaired, notes, changed = _try_repair_take_profits(d, _snapshot())
        _assert_downgraded(repaired, notes, changed)

    def test_valid_objects_untouched(self) -> None:
        d = _decision(take_profits=[{"price": 196.0, "ratio": 1.0}])
        repaired, _notes, changed = _try_repair_take_profits(d, _snapshot())
        assert changed is False
        assert repaired is d

    def test_valid_object_pair_ratio_sum_one_untouched(self) -> None:
        # Two objects whose ratios sum to exactly 1.0 are a valid plan.
        d = _decision(take_profits=[
            {"price": 100.0, "ratio": 0.5},
            {"price": 200.0, "ratio": 0.5},
        ])
        repaired, _notes, changed = _try_repair_take_profits(d, _snapshot())
        assert changed is False
        assert repaired is d

    def test_no_trade_plan_untouched(self) -> None:
        d = _decision(take_profits=[123.45])
        d["has_trade_plan"] = False
        del d["trade_plan"]
        repaired, _notes, changed = _try_repair_take_profits(d, _snapshot())
        assert changed is False

    def test_take_profits_not_a_list_untouched(self) -> None:
        d = _decision(take_profits={"price": 196.0, "ratio": 1.0})
        repaired, _notes, changed = _try_repair_take_profits(d, _snapshot())
        assert changed is False


class TestTakeProfitsChainIntegration:
    """P1-2 chain integration through the real single-attempt unit with the
    provider boundary patched."""

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

    def _raw(self, take_profits: object) -> str:
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
                "take_profits": take_profits,
                "invalid_condition": "1H 跌破 170",
            },
            "opportunity_watch": None,
            "suggested_actions": ["create_paper_order"],
        }
        return json.dumps(payload, ensure_ascii=False)

    def test_single_number_repaired_success(self) -> None:
        candidate, meta = self._run_attempt(self._raw([196.0]))
        assert candidate is not None, (
            "single numeric take_profits must be REPAIRED to a success, "
            f"not a hard failure; meta={meta}"
        )
        assert meta.get("llm_terminal_reason") == "schema_repaired"
        assert meta.get("llm_repair_event") is True
        assert meta.get("llm_status") == "ok"
        assert meta.get("llm_error") is None
        assert candidate["trade_plan"]["take_profits"] == [
            {"price": 196.0, "ratio": 1.0}
        ], f"got {candidate['trade_plan']['take_profits']!r}"
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("take_profits_repaired") is True, (
            "audit flag take_profits_repaired must be set; "
            f"parse_meta={parse_meta}"
        )
        assert parse_meta.get("original_take_profits") == [196.0]
        assert meta.get("llm_provider_call_count") == 1
        ok, err = validate_json("ga_decision.schema.json", candidate)
        assert ok, f"repaired decision must be schema-valid; {err}"

    def test_multiple_numbers_conservative_success(self) -> None:
        candidate, meta = self._run_attempt(self._raw([100.0, 200.0]))
        assert candidate is not None, (
            "multiple numeric take_profits must be conservatively repaired "
            f"to a success (monitor_only downgrade); meta={meta}"
        )
        assert meta.get("llm_terminal_reason") == "schema_repaired"
        assert candidate["decision"] == "monitor_only"
        assert candidate["has_trade_plan"] is False
        assert candidate.get("trade_plan") is None
        assert candidate["signal_grade"] in ("B", "C", "D")
        assert candidate["suggested_actions"] == ["monitor_only"]
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("take_profits_repaired") is True
        assert meta.get("llm_provider_call_count") == 1

    def test_valid_objects_plain_success(self) -> None:
        candidate, meta = self._run_attempt(
            self._raw([{"price": 196.0, "ratio": 1.0}])
        )
        assert candidate is not None
        assert meta.get("llm_terminal_reason") in (None, "ok"), (
            "valid take_profits must NOT be flagged schema_repaired; got "
            f"{meta.get('llm_terminal_reason')!r}"
        )
        parse_meta = candidate.get("llm_parse_meta") or {}
        assert parse_meta.get("take_profits_repaired") is not True
