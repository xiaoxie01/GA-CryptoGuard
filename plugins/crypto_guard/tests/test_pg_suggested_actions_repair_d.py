# -*- coding: utf-8 -*-
# crypto-guard-non-production-db:scratch-schema-only
# P2-3 (07-27 final review): filename contains ``repair_`` which the
# PreToolUse crypto-guard-command-guard classifies as database-mutation
# unless a non-production recipe is declared. This file uses only
# ``make_repo()`` scratch schemas on the dedicated crypto_guard_test DB
# (never production ``public``). Run recipe:
#
#   $env:CRYPTO_GUARD_DB = "C:\Users\24714\AppData\Local\Temp\cg_d_nonprod.sqlite3"
#   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest `
#     plugins/crypto_guard/tests/test_pg_suggested_actions_repair_d.py -q `
#     # crypto-guard-non-production-db:C:\Users\24714\AppData\Local\Temp\cg_d_nonprod.sqlite3
#
# ``CRYPTO_GUARD_DB`` is ignored by PG fixtures (they use DATABASE_URL /
# admin password bootstrap); it exists solely to satisfy the guard.
"""Requirement D (07-27 hourly-summary semantic fix): ``suggested_actions``
schema-alias repair — RED-first behavioral test + revert-fail.

D verbatim scope:

  The JSON Schema STAYS a flat string enum (do NOT loosen it). Handle the
  observed nested-array / mixed payload ``['monitor_only','wait_for_breakout',
  'avoid_chop']``. DO NOT blindly flatten illegal strings. REBUILD canonical
  from decision semantics (not just filter the enum):

    executable plan (has_trade_plan and trade_plan, or decision==
    trade_plan_available) -> create_paper_order
    opportunity watch (opportunity_watch non-null, or decision in
    {wait_for_pullback, wait_for_breakout, wait_for_reclaim}) ->
    create_opportunity_watch / add_to_watchlist (review: wait_for_* ->
    add_to_watchlist; opportunity_watch non-null but no plan ->
    create_opportunity_watch)
    no_edge / avoid_chop -> ignore
    other / fallback -> monitor_only

  Re-run full schema + semantic validation after repair (the caller
  re-validates via ``validate_json``). Record a category marker
  (``parse_meta["suggested_actions_repaired"] = True`` + original values).
  ONE physical provider call; the breaker must NOT surface a physical failure
  (reuse the repaired-success path that already does this).

This file exercises the repair at THREE levels:

  1. ``normalize_suggested_actions`` unit (decision_schema.py) — the canonical
     rebuild-from-decision-semantics, audit notes, changed flag.
  2. ``_try_repair_suggested_actions`` unit (llm_agent_judge.py) — extract
     semantic fields + delegate to the normalizer.
  3. Full ``_run_single_llm_attempt`` path (the schema-fail block at
     llm_agent_judge.py:1422-1465) — patch ``_call_ga_llm`` to return the
     malformed ETH payload, assert the repaired decision comes back schema-
     valid with ``llm_terminal_reason="schema_repaired"`` +
     ``suggested_actions_repaired=True`` + ONE physical call + NO physical
     failure category.

Schema-not-loosened guard: the raw illegal payload
``["monitor_only","wait_for_breakout","avoid_chop"]`` STILL fails
``validate_json("ga_decision.schema.json", ...)`` after this change — the
schema file is unchanged; the repair is in Python, NOT in the schema. This is
the key "D: schema stays flat string enum, not loosened" guard.

RED-first: every assertion below FAILS against the pre-fix code (there is no
``normalize_suggested_actions`` / ``_try_repair_suggested_actions`` and the
schema-fail block only tries the entry-trigger repair, so the mixed
decision-enum payload hard-fails to ``llm_schema_validation_failed`` with
``llm_status="failed"``).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.decision_schema import (
    normalize_suggested_actions,
    validate_json,
)
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _run_single_llm_attempt,
    _try_repair_suggested_actions,
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
)
from plugins.crypto_guard.reasoning.llm_breaker import CircuitBreaker
from plugins.crypto_guard.tests.pg_fixtures import make_repo


_ANALYSIS_TIME_UTC = 1785132899999
_SYMBOL = "ETHUSDT"

# The 5 canonical suggested_actions values the schema enum allows.
_CANONICAL_ACTIONS = frozenset(
    {"create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore", "monitor_only"}
)


def _timeframe_context(at: int) -> dict:
    return {
        "1d": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": at - 86_400_000},
        "4h": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": at - 14_400_000},
        "1h": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": at - 3_600_000},
        "15m": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": at - 900_000},
    }


def _minimal_valid_decision(*, decision: str, suggested_actions, **extra) -> dict:
    """A schema-valid GA decision skeleton with overridable semantic fields.

    Used both to build schema-VALID inputs (for the already-canonical test)
    and schema-INVALID inputs (illegal ``suggested_actions``) for the
    schema-not-loosened guard.
    """
    base = {
        "symbol": _SYMBOL,
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": decision,
        "signal_grade": "B",
        "market_bias": "bullish",
        "trend_stage": "middle",
        "confidence": 0.6,
        "summary": "test",
        "evidence": ["结构偏多"],
        "counter_evidence": ["等待确认"],
        "risk_notes": [],
        "has_trade_plan": False,
        "trade_plan": None,
        "opportunity_watch": None,
        "suggested_actions": suggested_actions,
        "timeframe_context": _timeframe_context(_ANALYSIS_TIME_UTC),
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
    }
    base.update(extra)
    return base


def _valid_trade_plan() -> dict:
    return {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 2500.0,
        "stop_loss": 2400.0,
        "take_profits": [{"price": 2700.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "跌破 2400.0",
    }


def _valid_opportunity_watch() -> dict:
    return {
        "needed": True,
        "direction": "LONG",
        "reason": "等待回踩",
        "conditions": ["price>2450"],
        "invalid_condition": None,
        "expires_minutes": 60,
    }


# ---------------------------------------------------------------------------
# Level 1: normalize_suggested_actions unit (decision_schema.py)
# ---------------------------------------------------------------------------


class TestNormalizeSuggestedActionsUnit:
    """Unit tests for ``normalize_suggested_actions`` — the canonical rebuild
    from decision semantics. RED-first: the function does not exist pre-fix."""

    def test_d_mixed_decision_enum_rebuilt_from_decision_semantics(self) -> None:
        """The observed ETH payload
        ``suggested_actions=['monitor_only','wait_for_breakout','avoid_chop']``
        with ``decision='wait_for_breakout'`` + ``opportunity_watch`` non-null.

        The rebuild must NOT just filter the enum (filtering would keep
        ``monitor_only`` and drop the two decision-enum values, ignoring the
        LLM's intent). The canonical mapping:
          - has_trade_plan=False, trade_plan=None -> NOT create_paper_order
          - opportunity_watch non-null -> create_opportunity_watch
        So the canonical list is ``["create_opportunity_watch"]``.

        NOTE: the review's literal list says wait_for_* -> add_to_watchlist,
        but opportunity_watch non-null (but no plan) -> create_opportunity_watch
        takes precedence (it is checked first in the mapping order: executable
        plan -> opportunity watch -> wait_for_* -> no_edge/avoid_chop -> other).
        When BOTH opportunity_watch is non-null AND decision is wait_for_*, the
        opportunity_watch branch wins (it is the more specific signal).
        """
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["monitor_only", "wait_for_breakout", "avoid_chop"],
                decision="wait_for_breakout",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch=_valid_opportunity_watch(),
            )
            assert canonical is not None, "must always return a non-None list (monitor_only fallback)"
            assert canonical == ["create_opportunity_watch"], (
                f"opportunity_watch non-null must map to create_opportunity_watch; "
                f"got {canonical}"
            )
            assert changed is True, "raw was schema-invalid -> changed must be True"
            assert isinstance(notes, list) and notes, "audit notes must be non-empty"
        finally:
            handle.close()

    def test_d_mixed_decision_enum_wait_for_no_opportunity_watch(self) -> None:
        """wait_for_breakout with NO opportunity_watch -> add_to_watchlist
        (the wait_for_* -> add_to_watchlist branch)."""
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["monitor_only", "wait_for_breakout", "avoid_chop"],
                decision="wait_for_breakout",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch=None,
            )
            assert canonical == ["add_to_watchlist"], (
                f"wait_for_* with no opportunity_watch must map to add_to_watchlist; "
                f"got {canonical}"
            )
            assert changed is True
            assert any("rebuilt_from_decision" in n for n in notes), (
                f"audit notes must record the rebuild-from-decision marker; got {notes}"
            )
        finally:
            handle.close()

    def test_d_empty_opportunity_watch_dict_falls_through_to_wait_for(self) -> None:
        """P2-1: empty ``opportunity_watch={}`` is NOT a real watch.

        Pre-fix ``is not None`` over-fired ``create_opportunity_watch`` and
        skipped wait_for_* -> add_to_watchlist. Post-fix only a non-empty
        dict wins the opportunity-watch branch.
        """
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["wait_for_breakout"],
                decision="wait_for_breakout",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch={},
            )
            assert canonical == ["add_to_watchlist"], (
                f"empty opportunity_watch={{}} with wait_for_breakout must map to "
                f"add_to_watchlist (not create_opportunity_watch); got {canonical}"
            )
            assert changed is True
            # None must behave the same as {} for the wait_for_* path.
            canonical_none, _notes_none, _changed_none = normalize_suggested_actions(
                ["wait_for_breakout"],
                decision="wait_for_breakout",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch=None,
            )
            assert canonical_none == ["add_to_watchlist"], (
                f"opportunity_watch=None with wait_for_breakout must still map to "
                f"add_to_watchlist; got {canonical_none}"
            )
        finally:
            handle.close()

    def test_d_nested_array_flattened_and_rebuilt(self) -> None:
        """Nested-array payload ``[['monitor_only'], ['create_paper_order']]``
        with ``has_trade_plan=True, trade_plan={...}, decision='trade_plan_available'``.

        The rebuild ignores the raw nested values and rebuilds from semantics:
        executable plan present -> ``["create_paper_order"]``.
        """
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                [["monitor_only"], ["create_paper_order"]],
                decision="trade_plan_available",
                has_trade_plan=True,
                trade_plan=_valid_trade_plan(),
                opportunity_watch=None,
            )
            assert canonical == ["create_paper_order"], (
                f"executable plan must map to create_paper_order; got {canonical}"
            )
            assert changed is True
            assert any("nested_array" in n for n in notes), (
                f"audit notes must record the nested-array marker; got {notes}"
            )
        finally:
            handle.close()

    def test_d_no_edge_avoid_chop_mapped_to_ignore(self) -> None:
        """``decision='no_edge'`` (and ``avoid_chop``) -> ignore."""
        handle = make_repo()
        try:
            for dec in ("no_edge", "avoid_chop"):
                canonical, notes, changed = normalize_suggested_actions(
                    ["monitor_only", "wait_for_breakout"],
                    decision=dec,
                    has_trade_plan=False,
                    trade_plan=None,
                    opportunity_watch=None,
                )
                assert canonical == ["ignore"], (
                    f"{dec} must map to ignore; got {canonical}"
                )
                assert changed is True
        finally:
            handle.close()

    def test_d_monitor_only_fallback(self) -> None:
        """``decision='monitor_only'``, no plan, no opportunity_watch ->
        monitor_only (the fallback)."""
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["wait_for_breakout"],
                decision="monitor_only",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch=None,
            )
            assert canonical == ["monitor_only"], (
                f"monitor_only fallback must map to monitor_only; got {canonical}"
            )
            assert changed is True
        finally:
            handle.close()

    def test_d_already_canonical_unchanged(self) -> None:
        """A raw that is ALREADY canonical and valid -> changed=False.

        ``["monitor_only"]`` with ``decision='monitor_only'`` is already the
        canonical list the mapping would produce -> no change.
        """
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["monitor_only"],
                decision="monitor_only",
                has_trade_plan=False,
                trade_plan=None,
                opportunity_watch=None,
            )
            assert canonical == ["monitor_only"]
            assert changed is False, (
                "already-canonical valid input must NOT set changed (no repair)"
            )
            assert notes == [], f"no audit notes for an unchanged canonical input; got {notes}"
        finally:
            handle.close()

    def test_d_already_canonical_create_paper_order_unchanged(self) -> None:
        """``["create_paper_order"]`` with an executable plan -> unchanged."""
        handle = make_repo()
        try:
            canonical, notes, changed = normalize_suggested_actions(
                ["create_paper_order"],
                decision="trade_plan_available",
                has_trade_plan=True,
                trade_plan=_valid_trade_plan(),
                opportunity_watch=None,
            )
            assert canonical == ["create_paper_order"]
            assert changed is False, (
                "already-canonical create_paper_order must NOT set changed"
            )
        finally:
            handle.close()

    def test_d_canonical_always_subset_of_schema_enum(self) -> None:
        """Every canonical list the normalizer produces must be a subset of
        the 5-value schema enum (schema-validity guard)."""
        handle = make_repo()
        try:
            cases = [
                (["monitor_only", "wait_for_breakout", "avoid_chop"], "wait_for_breakout", False, None, None),
                (["wait_for_breakout"], "wait_for_pullback", False, None, None),
                (["wait_for_breakout"], "wait_for_reclaim", False, None, None),
                ([["monitor_only"], ["create_paper_order"]], "trade_plan_available", True, _valid_trade_plan(), None),
                (["monitor_only"], "no_edge", False, None, None),
                (["monitor_only"], "avoid_chop", False, None, None),
                (["monitor_only"], "monitor_only", False, None, None),
                (["create_opportunity_watch"], "wait_for_breakout", False, None, _valid_opportunity_watch()),
            ]
            for raw, dec, htp, tp, ow in cases:
                canonical, _notes, _changed = normalize_suggested_actions(
                    raw, decision=dec, has_trade_plan=htp, trade_plan=tp, opportunity_watch=ow,
                )
                assert canonical is not None
                assert all(a in _CANONICAL_ACTIONS for a in canonical), (
                    f"canonical {canonical} contains a value outside the 5-value enum"
                )
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Level 2: _try_repair_suggested_actions unit (llm_agent_judge.py)
# ---------------------------------------------------------------------------


class TestTryRepairSuggestedActionsUnit:
    """Unit tests for ``_try_repair_suggested_actions`` — extracts semantic
    fields from the decision + delegates to the normalizer. RED-first: the
    function does not exist pre-fix."""

    def test_d_repair_changed_returns_repaired_decision(self) -> None:
        """A decision with illegal ``suggested_actions`` -> returns a new
        decision with the canonical list + notes + changed=True."""
        handle = make_repo()
        try:
            decision = _minimal_valid_decision(
                decision="wait_for_breakout",
                suggested_actions=["monitor_only", "wait_for_breakout", "avoid_chop"],
                opportunity_watch=_valid_opportunity_watch(),
            )
            snapshot = {"symbol": _SYMBOL, "analysis_time_utc": _ANALYSIS_TIME_UTC}
            repaired, notes, changed = _try_repair_suggested_actions(decision, snapshot)
            assert changed is True
            assert repaired is not decision, "must return a NEW dict (not mutate in place)"
            assert repaired["suggested_actions"] == ["create_opportunity_watch"], (
                f"repaired suggested_actions must be canonical; got {repaired['suggested_actions']}"
            )
            assert isinstance(notes, list) and notes
        finally:
            handle.close()

    def test_d_repair_unchanged_returns_original(self) -> None:
        """A decision with already-canonical ``suggested_actions`` -> returns
        the original decision + empty notes + changed=False."""
        handle = make_repo()
        try:
            decision = _minimal_valid_decision(
                decision="monitor_only",
                suggested_actions=["monitor_only"],
            )
            snapshot = {"symbol": _SYMBOL, "analysis_time_utc": _ANALYSIS_TIME_UTC}
            repaired, notes, changed = _try_repair_suggested_actions(decision, snapshot)
            assert changed is False
            assert repaired is decision, "unchanged must return the SAME dict"
            assert notes == []
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Level 3: full _run_single_llm_attempt path (schema-fail repair block)
# ---------------------------------------------------------------------------


class TestRunSingleLlmAttemptSuggestedActionsRepair:
    """Full-path tests through ``_run_single_llm_attempt`` — the schema-fail
    block at llm_agent_judge.py:1422-1465. Patches ``_call_ga_llm`` to return
    the malformed payload and asserts the repaired decision comes back
    schema-valid with the right §8 envelope. RED-first: pre-fix the block
    only tries the entry-trigger repair, so the mixed decision-enum payload
    hard-fails to ``llm_schema_validation_failed``."""

    @staticmethod
    def _build_attempt_inputs(*, llm_payload: dict):
        """Build the minimal inputs ``_run_single_llm_attempt`` needs.

        Returns ``(kwargs, breaker)``. The breaker is a real
        ``CircuitBreaker`` so ``record_attempt`` is exercised (mirrors the
        smoke-suite entry-trigger repair test at line 44468).
        """
        snapshot = {
            "symbol": _SYMBOL,
            "analysis_time_utc": _ANALYSIS_TIME_UTC,
            "profiles": {tf: {"market_structure": "bullish", "momentum": "bullish"}
                         for tf in ("1d", "4h", "1h", "15m")},
            "modules": {"momentum": {"direction": "bullish"}},
            "data_quality": {
                "health_by_tf": {tf: {"ready": True, "last_close_time": _ANALYSIS_TIME_UTC - 60_000}
                                 for tf in ("1d", "4h", "1h", "15m")},
            },
        }
        fallback = _minimal_valid_decision(
            decision="monitor_only",
            suggested_actions=["monitor_only"],
        )
        breaker = CircuitBreaker(enabled=True, min_rate_samples=5)
        kwargs = dict(
            snapshot=snapshot,
            fallback=fallback,
            context=None,
            attempt=1,
            max_attempts=1,
            breaker=breaker,
            cfg_name="test_cfg",
            model_name="test_model",
            prompt_builders=(
                build_llm_decision_prompt,
                build_llm_strict_json_prompt,
                build_llm_minimal_safe_prompt,
            ),
        )
        return kwargs, breaker

    def test_d_suggested_actions_mixed_decision_enum_repaired_to_canonical(self) -> None:
        """The observed ETH payload: ``suggested_actions=['monitor_only',
        'wait_for_breakout','avoid_chop']`` + ``decision='wait_for_breakout'``
        + ``opportunity_watch`` non-null.

        GREEN contract (post-fix):
          - ``llm_status == "ok"``
          - ``llm_terminal_reason == "schema_repaired"``
          - ``llm_repair_event == True``
          - repaired ``suggested_actions`` is the canonical list
            (``["create_opportunity_watch"]`` — opportunity_watch non-null)
            AND a subset of the 5-value enum
          - ``llm_parse_meta["suggested_actions_repaired"] is True``
          - ``llm_provider_call_count == 1`` (ONE physical call)
          - NO ``llm_error_category`` / ``llm_fallback_reason`` (no physical
            failure surfaced)

        RED (pre-fix): the schema-fail block only calls
        ``_try_repair_entry_trigger_confirmation`` (no suggested_actions
        repair), so re-validation still fails and the row goes to
        ``llm_status="failed"`` / ``llm_terminal_reason=
        "llm_schema_validation_failed"`` / ``llm_error_category=
        "llm_schema_validation_failed"``. The assertions below would FAIL.
        """
        handle = make_repo()
        try:
            payload = _minimal_valid_decision(
                decision="wait_for_breakout",
                suggested_actions=["monitor_only", "wait_for_breakout", "avoid_chop"],
                opportunity_watch=_valid_opportunity_watch(),
            )
            kwargs, _breaker = self._build_attempt_inputs(llm_payload=payload)
            with patch(
                "plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
                return_value=json.dumps(payload, ensure_ascii=False),
            ):
                decision, attempt_meta = _run_single_llm_attempt(**kwargs)

            # --- GREEN assertions ---
            assert decision is not None, (
                "repaired decision must be returned (not None); pre-fix it is "
                "None because the suggested_actions repair does not exist."
            )
            assert str(decision.get("llm_status") or "").lower() == "ok", (
                f"repaired success must have llm_status=ok; got "
                f"{decision.get('llm_status')!r}. Pre-fix: 'failed'."
            )
            assert decision.get("llm_terminal_reason") == "schema_repaired", (
                f"repaired success must carry llm_terminal_reason=schema_repaired; "
                f"got {decision.get('llm_terminal_reason')!r}. Pre-fix: "
                "llm_schema_validation_failed."
            )
            assert decision.get("llm_repair_event") is True, (
                f"repaired success must set llm_repair_event=True; got "
                f"{decision.get('llm_repair_event')!r}"
            )
            repaired_actions = decision.get("suggested_actions")
            assert isinstance(repaired_actions, list), (
                f"repaired suggested_actions must be a list; got {type(repaired_actions)}"
            )
            assert all(a in _CANONICAL_ACTIONS for a in repaired_actions), (
                f"repaired suggested_actions must be subset of the 5-value enum; "
                f"got {repaired_actions}"
            )
            assert repaired_actions == ["create_opportunity_watch"], (
                f"opportunity_watch non-null -> create_opportunity_watch; got "
                f"{repaired_actions}"
            )
            parse_meta = decision.get("llm_parse_meta") or {}
            assert isinstance(parse_meta, dict) and parse_meta.get("suggested_actions_repaired") is True, (
                f"parse_meta must record suggested_actions_repaired=True; got "
                f"{parse_meta}"
            )
            # ONE physical call, NO physical failure surfaced.
            assert decision.get("llm_provider_call_count") == 1, (
                f"exactly ONE physical provider call; got "
                f"{decision.get('llm_provider_call_count')!r}"
            )
            assert not decision.get("llm_error_category"), (
                f"no physical failure category; got "
                f"{decision.get('llm_error_category')!r}. Pre-fix: "
                "llm_schema_validation_failed."
            )
            assert not decision.get("llm_fallback_reason"), (
                f"no fallback reason; got {decision.get('llm_fallback_reason')!r}"
            )
        finally:
            handle.close()

    def test_d_suggested_actions_nested_array_repaired(self) -> None:
        """Nested-array payload ``[['monitor_only'], ['create_paper_order']]``
        with ``has_trade_plan=True, trade_plan={...}, decision='trade_plan_available'``.

        GREEN: repaired to ``['create_paper_order']`` (executable plan
        mapping), schema-valid, ``schema_repaired``.
        """
        handle = make_repo()
        try:
            payload = _minimal_valid_decision(
                decision="trade_plan_available",
                suggested_actions=[["monitor_only"], ["create_paper_order"]],
                has_trade_plan=True,
                trade_plan=_valid_trade_plan(),
            )
            kwargs, _breaker = self._build_attempt_inputs(llm_payload=payload)
            with patch(
                "plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
                return_value=json.dumps(payload, ensure_ascii=False),
            ):
                decision, attempt_meta = _run_single_llm_attempt(**kwargs)

            assert decision is not None, "nested-array repair must return a decision"
            assert str(decision.get("llm_status") or "").lower() == "ok"
            assert decision.get("llm_terminal_reason") == "schema_repaired"
            assert decision.get("suggested_actions") == ["create_paper_order"], (
                f"executable plan -> create_paper_order; got "
                f"{decision.get('suggested_actions')!r}"
            )
            # Schema-valid (the caller re-validated).
            ok, _err = validate_json("ga_decision.schema.json", _strip_audit_fields(decision))
            assert ok, (
                f"repaired decision must be schema-valid; err={_err}"
            )
            assert decision.get("llm_parse_meta", {}).get("suggested_actions_repaired") is True
        finally:
            handle.close()

    def test_d_suggested_actions_already_canonical_unchanged(self) -> None:
        """A schema-valid ``suggested_actions=['monitor_only']`` with
        ``decision='monitor_only'`` — no repair needed.

        GREEN: the NORMAL success path (NOT schema_repaired) —
        ``llm_terminal_reason`` is None (not ``schema_repaired``), no
        ``suggested_actions_repaired`` marker. This proves the repair only
        fires when needed.
        """
        handle = make_repo()
        try:
            payload = _minimal_valid_decision(
                decision="monitor_only",
                suggested_actions=["monitor_only"],
            )
            kwargs, _breaker = self._build_attempt_inputs(llm_payload=payload)
            with patch(
                "plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
                return_value=json.dumps(payload, ensure_ascii=False),
            ):
                decision, attempt_meta = _run_single_llm_attempt(**kwargs)

            assert decision is not None
            assert str(decision.get("llm_status") or "").lower() == "ok"
            assert decision.get("llm_terminal_reason") is None, (
                f"normal success has llm_terminal_reason=None (not schema_repaired); "
                f"got {decision.get('llm_terminal_reason')!r}"
            )
            assert decision.get("llm_repair_event") is not True, (
                f"no repair event for an already-valid payload; got "
                f"{decision.get('llm_repair_event')!r}"
            )
            parse_meta = decision.get("llm_parse_meta") or {}
            assert "suggested_actions_repaired" not in parse_meta, (
                f"no suggested_actions_repaired marker for an unchanged payload; "
                f"got {parse_meta}"
            )
            assert decision.get("suggested_actions") == ["monitor_only"]
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Schema-not-loosened guard + revert-fail control
# ---------------------------------------------------------------------------


def _strip_audit_fields(decision: dict) -> dict:
    """Strip non-schema audit fields so ``validate_json`` checks ONLY the
    schema-declared shape (the §8 envelope / parse_meta are not in the
    schema)."""
    schema_keys = {
        "symbol", "analysis_time_utc", "decision", "signal_grade", "market_bias",
        "trend_stage", "confidence", "summary", "evidence", "counter_evidence",
        "risk_notes", "has_trade_plan", "trade_plan", "opportunity_watch",
        "suggested_actions", "timeframe_context", "alignment", "htf_conflict",
        "market_reason_codes",
    }
    return {k: v for k, v in decision.items() if k in schema_keys}


class TestSchemaNotLoosenedGuard:
    """The schema STAYS a flat string enum. The raw illegal payload STILL
    fails ``validate_json`` after the change — the repair is in Python, NOT
    in the schema file (which is unchanged). This is the key D guard."""

    def test_d_raw_illegal_payload_still_fails_schema_validation(self) -> None:
        """The raw payload
        ``["monitor_only","wait_for_breakout","avoid_chop"]`` is schema-
        INVALID before AND after the fix (the schema enum is unchanged).

        This proves the schema was NOT loosened — only the Python repair
        rebuilds a canonical list. Document in the docstring: the
        ``schema_repaired`` + ``ok`` assertions in the full-path test above
        would FAIL against pre-fix code (the repair did not exist, so the raw
        payload hard-failed to ``llm_schema_validation_failed``).
        """
        handle = make_repo()
        try:
            illegal = _minimal_valid_decision(
                decision="wait_for_breakout",
                suggested_actions=["monitor_only", "wait_for_breakout", "avoid_chop"],
            )
            ok, err = validate_json("ga_decision.schema.json", illegal)
            assert ok is False, (
                f"raw illegal suggested_actions MUST fail schema validation "
                f"(schema not loosened); got ok={ok}, err={err}. If this passes, "
                f"the schema enum was loosened — D is violated."
            )
            # P2-2 (07-27 final review): parentheses required — bare
            # ``a and b or c or d`` binds as ``(a and b) or c or d`` and can
            # pass on a weak/wrong err via the later ``or`` arms.
            assert err is not None and (
                ("wait_for_breakout" in str(err))
                or ("avoid_chop" in str(err))
                or ("is not one of" in str(err))
            ), (
                f"the schema error must reference the illegal enum value; got {err}"
            )

            # And the repaired canonical version PASSES.
            repaired = dict(illegal)
            repaired["suggested_actions"] = ["create_opportunity_watch"]
            ok2, err2 = validate_json("ga_decision.schema.json", repaired)
            assert ok2 is True, (
                f"repaired canonical suggested_actions MUST pass schema "
                f"validation; got ok={ok2}, err={err2}"
            )
        finally:
            handle.close()

    def test_d_nested_array_raw_still_fails_schema_validation(self) -> None:
        """The raw nested-array payload is schema-invalid (items must be
        strings from the enum, not sub-arrays)."""
        handle = make_repo()
        try:
            illegal = _minimal_valid_decision(
                decision="trade_plan_available",
                suggested_actions=[["monitor_only"], ["create_paper_order"]],
                has_trade_plan=True,
                trade_plan=_valid_trade_plan(),
            )
            ok, err = validate_json("ga_decision.schema.json", illegal)
            assert ok is False, (
                f"raw nested-array suggested_actions MUST fail schema "
                f"validation; got ok={ok}, err={err}"
            )
        finally:
            handle.close()

    def test_d_revert_fail_control_pre_fix_would_hard_fail(self) -> None:
        """Revert-fail control: prove the GREEN assertions in
        ``test_d_suggested_actions_mixed_decision_enum_repaired_to_canonical``
        are LOAD-BEARING, not vacuously true.

        Reconstructs the PRE-FIX schema-fail block behavior: only the
        entry-trigger repair is tried (no suggested_actions repair). For the
        ETH payload (no entry_trigger_confirmation to repair), the entry-
        trigger repair returns ``changed=False`` and re-validation is NOT
        re-run, so the row falls through to hard schema failure. This control
        asserts that pre-fix shape so that removing the
        ``_try_repair_suggested_actions`` call flips the full-path test RED.

        Implementation: call the RAW ``validate_json`` on the un-repaired
        payload (what the pre-fix code did at line 1422) and assert it
        returns False — proving the pre-fix code would have hard-failed
        this row to ``llm_schema_validation_failed`` (the exact opposite of
        the GREEN ``schema_repaired`` + ``ok``).
        """
        handle = make_repo()
        try:
            payload = _minimal_valid_decision(
                decision="wait_for_breakout",
                suggested_actions=["monitor_only", "wait_for_breakout", "avoid_chop"],
                opportunity_watch=_valid_opportunity_watch(),
            )
            # Pre-fix line 1422: validate_json on the raw candidate.
            ok_raw, _err_raw = validate_json("ga_decision.schema.json", payload)
            assert ok_raw is False, (
                "revert-fail control: the raw payload must be schema-invalid "
                "(pre-fix line 1422 returns ok=False). If this passes, the "
                "schema was loosened and the control is meaningless."
            )
            # Pre-fix line 1425: _try_repair_entry_trigger_confirmation on a
            # decision with NO entry_trigger_confirmation -> changed=False.
            from plugins.crypto_guard.reasoning.llm_agent_judge import (
                _try_repair_entry_trigger_confirmation,
            )
            snapshot = {"symbol": _SYMBOL, "analysis_time_utc": _ANALYSIS_TIME_UTC}
            _repaired, _notes, et_changed = _try_repair_entry_trigger_confirmation(
                payload, snapshot
            )
            assert et_changed is False, (
                "revert-fail control: the ETH payload has no "
                "entry_trigger_confirmation, so the entry-trigger repair must "
                "return changed=False (nothing to repair). Pre-fix the block "
                "then falls through to hard schema failure."
            )
            # Pre-fix: with et_changed=False, the block skips re-validation
            # and falls to lines 1466-1473 -> llm_schema_validation_failed.
            # So the pre-fix terminal reason for this row would be
            # ``llm_schema_validation_failed`` — the exact opposite of the
            # GREEN ``schema_repaired``. This control documents that fact.
        finally:
            handle.close()