# -*- coding: utf-8 -*-
"""07-31 production fix P1-3 (2026-07-31): prompt type-contract
disambiguation — RED-first behavioral test + revert-fail.

Production evidence #1: models repeatedly emitted ``"decision": [...]`` as
an array and numeric ``take_profits`` items (evidence #2). The prompt's
``schema_contract.decision`` was rendered as a BARE ARRAY of enum strings
(llm_agent_judge.py:1684), which teaches the model that decision may BE an
array. Fix the prompt contract itself:

- ``schema_contract.decision`` becomes ``{"type": "string", "enum": [...]}``
  (never a bare array).
- ALL THREE real provider tiers (main decision prompt, strict-JSON retry,
  minimal-safe retry) must state the three type contracts verbatim:
  decision is a single string never an array; suggested_actions is a
  string array; take_profits is an object array of {price, ratio}.
- Each rule carries legal + illegal JSON examples inline.
- Tests parse the REAL builder output (not a mock).

The rule texts below are the exact strings the implementation must embed,
so a future regression (or a drift between tiers) fails the test.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.llm_agent_judge import (
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
)

_ANALYSIS_TIME_UTC = 1785487499999

_LEGAL_DECISIONS = [
    "trade_plan_available", "wait_for_pullback", "wait_for_breakout",
    "wait_for_reclaim", "avoid_chop", "no_edge", "monitor_only",
]

# 07-31 final review P1-2: canonical typed contracts for EVERY
# schema_contract field, asserted per-field on all three tiers.
_SCALAR_CONTRACT_ENUMS = {
    "signal_grade": ["S", "A", "B", "C", "D"],
    "market_bias": ["bullish", "bearish", "neutral", "mixed", "unknown"],
    "trend_stage": ["early", "middle", "late", "range", "transition", "unknown"],
}
_SUGGESTED_ACTIONS_CONTRACT_ENUM = [
    "create_paper_order", "create_opportunity_watch",
    "add_to_watchlist", "ignore", "monitor_only",
]

# Verbatim type-contract rules the implementation must embed in every tier.
_DECISION_RULE = (
    "decision 必须是单个字符串，绝不允许输出为数组。"
    "合法示例：\"monitor_only\"。"
    "非法示例：[\"monitor_only\"]、[\"trade_plan_available\",\"wait_for_pullback\"]"
)
_SUGGESTED_RULE = (
    "suggested_actions 必须是字符串数组，每个元素只能是 "
    "create_paper_order、create_opportunity_watch、add_to_watchlist、ignore、"
    "monitor_only 之一。"
    "合法示例：[\"monitor_only\"]。"
    "非法示例：[\"monitor_only\",\"wait_for_breakout\"]"
)
_TAKE_PROFITS_RULE = (
    "take_profits 必须是对象数组，每个元素必须是 {\"price\": 数字, \"ratio\": 数字}。"
    "合法示例：[{\"price\":196.0,\"ratio\":1.0}]。"
    "非法示例：[196.0]、[100.0,200.0]"
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


def _deterministic() -> dict:
    return {
        "decision": "monitor_only",
        "signal_grade": "B",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.5,
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
    }


def _payload(prompt: str) -> dict:
    """Parse the REAL builder output back into its JSON payload.

    Every prompt tier is ``SYSTEM_PROMPT... + "\\n\\n输入：\\n" +
    json.dumps(payload)``; the payload is the part after that separator.
    """
    assert "\n\n输入：\n" in prompt, "prompt must carry the 输入： payload separator"
    raw = prompt.split("\n\n输入：\n", 1)[1]
    return json.loads(raw)


def _hard_rules_text(prompt: str) -> str:
    payload = _payload(prompt)
    rules = payload["hard_rules"]
    assert isinstance(rules, list) and rules, "hard_rules must be a non-empty list"
    return "\n".join(str(r) for r in rules)


def _build_all() -> tuple[str, str, str]:
    snap = _snapshot()
    det = _deterministic()
    fake_cfg = SimpleNamespace(trading_mode={"risk": {}})
    with mock.patch(
        "plugins.crypto_guard.config.loader.load_config",
        return_value=fake_cfg,
    ):
        main_prompt = build_llm_decision_prompt(snap, det, context=None)
        strict_prompt = build_llm_strict_json_prompt(snap, det, context=None)
        minimal_prompt = build_llm_minimal_safe_prompt(snap, det, context=None)
    return main_prompt, strict_prompt, minimal_prompt


class TestSchemaContractDecisionIsTypeString:
    """P1-3: schema_contract.decision must be {type:string, enum:[...]}, NOT a
    bare array. RED pre-fix it is a bare list (llm_agent_judge.py:1684)."""

    def test_main_prompt_decision_contract_is_type_string(self) -> None:
        main_prompt, _strict, _minimal = _build_all()
        contract = _payload(main_prompt)["schema_contract"]["decision"]
        assert isinstance(contract, dict), (
            "schema_contract.decision must be a {type, enum} dict, not a bare "
            f"array; got {contract!r}"
        )
        assert contract.get("type") == "string", (
            f"schema_contract.decision.type must be 'string'; got {contract!r}"
        )
        assert contract.get("enum") == _LEGAL_DECISIONS, (
            f"schema_contract.decision.enum must be the 7 legal values; got "
            f"{contract.get('enum')!r}"
        )

    def test_strict_prompt_shares_type_string_contract(self) -> None:
        # Strict retry reuses the main payload, so its schema_contract must
        # carry the same {type:string, enum} decision contract.
        _main, strict_prompt, _minimal = _build_all()
        contract = _payload(strict_prompt)["schema_contract"]["decision"]
        assert isinstance(contract, dict)
        assert contract.get("type") == "string"
        assert contract.get("enum") == _LEGAL_DECISIONS


class TestSchemaContractEveryFieldIsTyped:
    """07-31 final review P1-2: EVERY scalar contract field must be a
    ``{"type": "string", "enum": [...]}`` dict and ``suggested_actions`` a
    ``{"type": "array", "items": {"type": "string", "enum": [...]}}`` dict —
    asserted per-field (never just ``decision``) on ALL THREE real tiers
    (main / strict / minimal). RED pre-fix: signal_grade / market_bias /
    trend_stage / suggested_actions are BARE ARRAYS and the minimal tier
    has NO schema_contract key at all."""

    def test_all_three_tiers_carry_typed_contract_for_every_field(self) -> None:
        main_prompt, strict_prompt, minimal_prompt = _build_all()
        for tier, prompt in (
            ("main", main_prompt),
            ("strict", strict_prompt),
            ("minimal", minimal_prompt),
        ):
            payload = _payload(prompt)
            contract = payload.get("schema_contract")
            assert isinstance(contract, dict), (
                f"{tier} tier must carry a schema_contract dict; got {contract!r}"
            )
            for field, enum in _SCALAR_CONTRACT_ENUMS.items():
                spec = contract.get(field)
                assert isinstance(spec, dict), (
                    f"{tier}.schema_contract.{field} must be a {{type, enum}} "
                    f"dict, NOT a bare array; got {spec!r}"
                )
                assert spec.get("type") == "string", (
                    f"{tier}.schema_contract.{field}.type must be 'string'; "
                    f"got {spec!r}"
                )
                assert spec.get("enum") == enum, (
                    f"{tier}.schema_contract.{field}.enum must be {enum!r}; "
                    f"got {spec.get('enum')!r}"
                )
            spec = contract.get("suggested_actions")
            assert isinstance(spec, dict), (
                f"{tier}.schema_contract.suggested_actions must be a "
                f"{{type, items}} dict, NOT a bare array; got {spec!r}"
            )
            assert spec.get("type") == "array", (
                f"{tier}.schema_contract.suggested_actions.type must be "
                f"'array'; got {spec!r}"
            )
            items = spec.get("items")
            assert isinstance(items, dict), (
                f"{tier}.schema_contract.suggested_actions.items must be a "
                f"{{type, enum}} dict; got {items!r}"
            )
            assert items.get("type") == "string"
            assert items.get("enum") == _SUGGESTED_ACTIONS_CONTRACT_ENUM, (
                f"{tier}.schema_contract.suggested_actions.items.enum must be "
                f"the 5 legal values; got {items.get('enum')!r}"
            )


class TestAllTiersCarryTypeContracts:
    """P1-3: all three real provider tiers embed the three type-contract
    rules with legal + illegal examples. RED pre-fix none of them exist."""

    def test_each_tier_contains_decision_rule(self) -> None:
        main_prompt, strict_prompt, minimal_prompt = _build_all()
        for name, prompt in (
            ("main", main_prompt),
            ("strict", strict_prompt),
            ("minimal", minimal_prompt),
        ):
            text = _hard_rules_text(prompt)
            assert _DECISION_RULE in text, (
                f"{name} tier must embed the decision type-contract rule; "
                f"hard_rules={text!r}"
            )

    def test_each_tier_contains_suggested_actions_rule(self) -> None:
        main_prompt, strict_prompt, minimal_prompt = _build_all()
        for name, prompt in (
            ("main", main_prompt),
            ("strict", strict_prompt),
            ("minimal", minimal_prompt),
        ):
            text = _hard_rules_text(prompt)
            assert _SUGGESTED_RULE in text, (
                f"{name} tier must embed the suggested_actions type-contract "
                f"rule; hard_rules={text!r}"
            )

    def test_each_tier_contains_take_profits_rule(self) -> None:
        main_prompt, strict_prompt, minimal_prompt = _build_all()
        for name, prompt in (
            ("main", main_prompt),
            ("strict", strict_prompt),
            ("minimal", minimal_prompt),
        ):
            text = _hard_rules_text(prompt)
            assert _TAKE_PROFITS_RULE in text, (
                f"{name} tier must embed the take_profits type-contract rule; "
                f"hard_rules={text!r}"
            )

    def test_rules_carry_legal_and_illegal_examples(self) -> None:
        for rule in (_DECISION_RULE, _SUGGESTED_RULE, _TAKE_PROFITS_RULE):
            assert "合法示例" in rule, f"rule must carry a 合法示例: {rule!r}"
            assert "非法示例" in rule, f"rule must carry a 非法示例: {rule!r}"
