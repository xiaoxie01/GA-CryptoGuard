# -*- coding: utf-8 -*-
"""08-02 production fix P0-3 (2026-08-02): structured opportunity-watch
conditions — RED-first behavioral test + revert-fail.

Production evidence (audit-confirmed 2026-08-02): 190 decisions/24h; 158/190
carried an ``opportunity_watch``; the ``conditions`` array = 121, ALL text
(e.g. "15M 收盘突破上沿或跌破下沿"), structured = 0; ``invalid_condition``
was a text blob or the pseudo-kind ``risk_rejected``; ``opportunity_watches``
table = 0 rows; 92 decisions declared ``create_opportunity_watch`` but nothing
materialized. The watcher (``scheduler/opportunity_watcher._condition_hit``)
only understands structured condition objects, so every text condition waited
forever — the watch never triggered and the alert never enqueued.

Contracts (P0-3 PRD, verbatim):

- ga_decision schema MUST forbid bare-string ``opportunity_watch.conditions``.
- Conditions must be the objects the watcher actually supports (type/kind,
  side, timeframe, plus required level/price fields).
- ``invalid_condition`` must also be structured.
- No regex/LLM free-text translator to fabricate conditions.
- If LLM gives no valid structure, build deterministically from
  candidate_trade_plan: pullback/breakout/reclaim + stop invalidation; if
  unbuildable, fail-closed (no auto watch) and emit structured diagnostics.
- Watcher must report untriggerable for text/unknown conditions, never
  silently wait forever.
- Real e2e: B candidate lands one active watch; next batch idempotent
  refresh; once K-line satisfies, becomes triggered and enqueues
  opportunity_watch_alert.

Revert-fail: undoing any single production edit (the schema tightening alone,
the ``normalize_opportunity_watch`` side injection, the ga_judge/llm_agent_
judge/risk_engine wiring, or the watcher untriggerable branch) must turn the
matching test RED again.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.watch_conditions import (
    is_structured_watch,
    normalize_opportunity_watch,
)
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _try_repair_opportunity_watch,
    _run_single_llm_attempt,
    build_llm_decision_prompt,
    build_llm_minimal_safe_prompt,
    build_llm_strict_json_prompt,
    run_agent_sop_decision,
)
from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker
from plugins.crypto_guard.scheduler.opportunity_watcher import (
    evaluate_watch,
    update_opportunity_watches,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo

_ANALYSIS_TIME_UTC = 1785487499999


def _tf_ctx() -> dict:
    return {
        tf: {"bias": "bullish", "structure": "bullish", "closed": True, "close_time": _ANALYSIS_TIME_UTC - 60_000}
        for tf in ("1d", "4h", "1h", "15m")
    }


def _decision(*, watch: object) -> dict:
    """A schema-complete decision whose ``opportunity_watch`` is the test knob.

    trade_plan mirrors the P1-2 ``_decision`` shape (schema-valid, string
    invalid_condition allowed by the trade_plan schema).
    """
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
            "take_profits": [{"price": 196.0, "ratio": 1.0}],
            "invalid_condition": "1H 跌破 170",
        },
        "opportunity_watch": watch,
        "suggested_actions": ["create_opportunity_watch"],
        "timeframe_context": _tf_ctx(),
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
    }


def _structured_watch() -> dict:
    """A fully schema-valid structured watch (side on conditions AND invalid)."""
    return {
        "needed": True,
        "direction": "LONG",
        "reason": "等待回踩确认",
        "conditions": [{"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m"}],
        "invalid_condition": {"type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m"},
        "expires_minutes": 120,
    }


def _plan(*, side="LONG", entry_type="limit", entry_price=2500.0,
          trigger_price=None, stop_loss=2400.0, confirmation=None) -> dict:
    plan = {
        "side": side,
        "entry_type": entry_type,
        "entry_price": entry_price,
        "trigger_price": trigger_price,
        "stop_loss": stop_loss,
        "take_profits": [{"price": 2800.0, "ratio": 1.0}],
        "invalid_condition": "1H 跌破 2400",
    }
    if confirmation is not None:
        plan["entry_trigger_confirmation"] = confirmation
    return plan


def _assert_schema_valid(watch: object) -> None:
    ok, err = validate_json("ga_decision.schema.json", _decision(watch=watch))
    assert ok, f"watch must be schema-valid; {err}"


def _assert_schema_invalid(watch: object) -> None:
    ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
    assert ok is False, "watch must be schema-invalid"


# ── 1. Schema: bare-string / malformed conditions are FORBIDDEN ────────────


class TestSchemaForbidsBareStringConditions:
    def test_bare_string_condition_rejected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False, "bare-string conditions must be schema-invalid"

    def test_mixed_string_condition_rejected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0},
                "15M 收盘突破上沿或跌破下沿",
            ],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_condition_missing_trigger_field_rejected(self) -> None:
        # type + side but no level/price/flow/value -> anyOf fails.
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "side": "LONG"}],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_unknown_kind_rejected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "break_the_market", "side": "LONG", "level": 100.0}],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_condition_missing_side_rejected(self) -> None:
        # The tightened schema requires ``side`` on every condition item.
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "level": 180.0}],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_string_invalid_condition_rejected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "side": "LONG", "level": 180.0}],
            "invalid_condition": "跌破 172",
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_invalid_condition_missing_side_rejected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "side": "LONG", "level": 180.0}],
            "invalid_condition": {"type": "close_below", "level": 172.0},
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_missing_direction_rejected(self) -> None:
        watch = {
            "needed": True,
            "conditions": [{"type": "pullback", "side": "LONG", "level": 180.0}],
            "invalid_condition": None,
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False

    def test_structured_watch_accepted(self) -> None:
        _assert_schema_valid(_structured_watch())

    def test_null_watch_accepted(self) -> None:
        _assert_schema_valid(None)

    def test_every_supported_kind_accepted(self) -> None:
        kinds = ["price_below", "close_below", "price_above", "close_above",
                 "pullback", "breakout", "reclaim"]
        for kind in kinds:
            cond = {"type": kind, "side": "LONG", "level": 180.0}
            watch = {
                "needed": True,
                "direction": "LONG",
                "conditions": [cond],
                "invalid_condition": {"type": "close_below", "side": "LONG", "level": 172.0},
            }
            _assert_schema_valid(watch)

    def test_cvd_confirmation_rejected(self) -> None:
        # 08-02 Codex P0: cvd_confirmation is REMOVED from the schema (the
        # watcher only compared the persisted flow_confirmation string, never
        # the real order-flow). A decision carrying a cvd condition is invalid.
        cond = {"type": "cvd_confirmation", "side": "LONG", "flow_confirmation": "supports_long"}
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [cond],
            "invalid_condition": {"type": "close_below", "side": "LONG", "level": 172.0},
        }
        _assert_schema_invalid(watch)

    def test_condition_with_schema_forbidden_keys_rejected(self) -> None:
        """08-02 P2-2 (fresh reviewer): condition items and invalid_condition
        are ``additionalProperties: false`` — ``direction``/``symbol`` are
        forbidden inside a condition (they are watch-envelope / top-level
        decision keys only). The schema itself must reject them."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0,
                 "direction": "LONG", "symbol": "SOLUSDT"}
            ],
            "invalid_condition": {
                "type": "close_below", "side": "LONG", "level": 172.0,
                "direction": "LONG", "symbol": "SOLUSDT"
            },
        }
        ok, _err = validate_json("ga_decision.schema.json", _decision(watch=watch))
        assert ok is False


# ── 2. Deterministic builder / normalizer ──────────────────────────────────


class TestDeterministicWatchBuilder:
    def test_text_conditions_built_to_pullback_from_limit_plan(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "reason": "等待回踩",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            "invalid_condition": "跌破 2400",
            "expires_minutes": 120,
        }
        normalized, notes = normalize_opportunity_watch(watch, _plan())
        assert normalized is not None
        assert normalized["direction"] == "LONG"
        assert normalized["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 2500.0, "timeframe": "15m"}
        ], f"got {normalized['conditions']!r}"
        assert normalized["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 2400.0, "timeframe": "15m"
        }
        assert normalized["expires_minutes"] == 120
        assert any("由交易计划确定性构建" in n for n in notes), notes
        _assert_schema_valid(normalized)

    def test_text_conditions_built_to_breakout_from_trigger_plan(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["突破 2600 后入场"],
            "invalid_condition": None,
        }
        # A trigger plan carries trigger_price (no entry_price); the builder
        # must fall back to trigger_price.
        normalized, _notes = normalize_opportunity_watch(
            watch, _plan(entry_type="trigger", entry_price=None, trigger_price=2600.0)
        )
        assert normalized is not None
        assert normalized["conditions"] == [
            {"type": "breakout", "side": "LONG", "level": 2600.0, "timeframe": "15m"}
        ]
        _assert_schema_valid(normalized)

    def test_text_conditions_built_to_reclaim_from_confirm_event(self) -> None:
        confirmation = {
            "event_type": "RECLAIM",
            "timeframe": "1h",
            "type": "closed_candle_confirmation",
            "direction": "bullish",
            "candle_close_time": 1000,
            "price": 2500.0,
            "source": "price_action",
            "symbol": "SOLUSDT",
        }
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["收复 2500 后入场"],
            "invalid_condition": None,
        }
        normalized, _notes = normalize_opportunity_watch(watch, _plan(confirmation=confirmation))
        assert normalized is not None
        assert normalized["conditions"] == [
            {"type": "reclaim", "side": "LONG", "level": 2500.0, "timeframe": "1h"}
        ]
        _assert_schema_valid(normalized)

    def test_structured_llm_condition_kept_and_side_injected(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"kind": "pullback", "level": 2500.0, "junk_field": "x"}],
            "invalid_condition": {"kind": "close_below", "level": 2400.0},
            "expires_minutes": 90,
        }
        normalized, notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        # kind->type canonicalized, junk dropped, side injected (schema-required).
        assert normalized["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 2500.0}
        ], f"got {normalized['conditions']!r}"
        assert normalized["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 2400.0
        }
        assert normalized["expires_minutes"] == 90
        assert not any("由交易计划确定性构建" in n for n in notes), notes
        _assert_schema_valid(normalized)

    def test_text_invalid_rebuilt_from_stop_loss(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "side": "LONG", "level": 2500.0}],
            "invalid_condition": "跌破 2400",
        }
        normalized, notes = normalize_opportunity_watch(watch, _plan())
        assert normalized is not None
        assert normalized["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 2400.0, "timeframe": "15m"
        }
        assert any("已从交易计划重建" in n for n in notes), notes
        _assert_schema_valid(normalized)

    def test_fail_closed_unbuildable_returns_none_with_diagnostic(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            "invalid_condition": None,
        }
        normalized, notes = normalize_opportunity_watch(watch, None)
        assert normalized is None
        assert any("fail-closed" in n for n in notes), notes

    def test_no_input_noop(self) -> None:
        normalized, notes = normalize_opportunity_watch(None, None)
        assert normalized is None
        assert notes == []

    def test_direction_falls_back_to_condition_side(self) -> None:
        watch = {
            "needed": True,
            "conditions": [{"type": "pullback", "side": "SHORT", "level": 100.0}],
            "invalid_condition": None,
        }
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert normalized["direction"] == "SHORT"
        _assert_schema_valid(normalized)

    def test_is_structured_watch_requires_side_on_conditions(self) -> None:
        bad = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "level": 2500.0}],
            "invalid_condition": None,
        }
        assert is_structured_watch(bad) is False, (
            "schema-repair short-circuit must NOT treat a side-less condition "
            "as structured (it would fail the tightened schema)"
        )

    def test_is_structured_watch_requires_side_on_invalid(self) -> None:
        bad = {
            "needed": True,
            "direction": "LONG",
            "conditions": [{"type": "pullback", "side": "LONG", "level": 2500.0}],
            "invalid_condition": {"type": "close_below", "level": 2400.0},
        }
        assert is_structured_watch(bad) is False

    def test_is_structured_watch_accepts_schema_valid_watch(self) -> None:
        assert is_structured_watch(_structured_watch()) is True

    def test_normalize_drops_schema_forbidden_condition_keys(self) -> None:
        """08-02 P2-2 (fresh reviewer): ``_clean_condition`` must NOT preserve
        ``direction``/``symbol`` inside a condition — ga_decision.schema.json
        makes condition items ``additionalProperties: false`` (only type/side/
        timeframe/level/price/tolerance_pct are legal; flow_confirmation/value
        were removed in 08-02 R2 P2-2). The normalizer drops them; the result
        must be schema-valid."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0,
                 "direction": "LONG", "symbol": "SOLUSDT"}
            ],
            "invalid_condition": {
                "type": "close_below", "side": "LONG", "level": 172.0,
                "direction": "LONG", "symbol": "SOLUSDT"
            },
        }
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert normalized["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 180.0}
        ], f"got {normalized['conditions']!r}"
        assert normalized["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 172.0
        }, f"got {normalized['invalid_condition']!r}"
        _assert_schema_valid(normalized)

    def test_is_structured_watch_rejects_schema_forbidden_condition_keys(self) -> None:
        """08-02 P2-2: the schema-repair short-circuit must not treat a watch
        as structured when a condition (or invalid_condition) carries
        ``direction``/``symbol`` — such a watch fails ``additionalProperties:
        false``, so it must fall through to ``normalize_opportunity_watch``
        which drops the keys."""
        bad = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0,
                 "direction": "LONG", "symbol": "SOLUSDT"}
            ],
            "invalid_condition": None,
        }
        assert is_structured_watch(bad) is False
        bad_invalid = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0}
            ],
            "invalid_condition": {
                "type": "close_below", "side": "LONG", "level": 172.0,
                "direction": "LONG", "symbol": "SOLUSDT"
            },
        }
        assert is_structured_watch(bad_invalid) is False

    def test_schema_rejects_level_less_flow_confirmation_condition(self) -> None:
        """08-02 R2 P2-2 (fresh reviewer): ``flow_confirmation`` is NO LONGER a
        declared condition property (``additionalProperties: false``) and the
        tightened anyOf requires level OR price. A condition carrying only a
        persisted ``flow_confirmation`` string — the old fake-CVD trigger —
        must be schema-invalid, so it can never be materialized as a
        triggerable watch. RED (revert-fail): restoring the flow_confirmation
        property + anyOf member makes this schema-valid again."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG",
                 "flow_confirmation": "supports_long", "timeframe": "15m"}
            ],
            "invalid_condition": None,
        }
        _assert_schema_invalid(watch)

    def test_normalize_fail_closed_when_only_flow_confirmation_condition(self) -> None:
        """08-02 R2 P2-2: a watch whose ONLY condition is a level-less
        ``flow_confirmation`` (no level/price, no plan to rebuild from) must
        normalize to None (fail-closed) — never an empty-shell or triggerable
        watch. RED (revert-fail): this is the P0 end-to-end guard — re-enabling
        the pre-P0 string-trigger acceptance in ``is_structured_condition``
        (``flow_confirmation`` as a usable trigger field, no level needed)
        keeps the condition and materializes the watch, flipping this RED
        (the P0 revert is proven in final-seal.md). The P2-2 key-set removal
        alone does not flip it — ``is_structured_condition`` already requires a
        numeric level/price, so this test is defense-in-depth for the
        ``_clean_condition``/key-set change AND the end-to-end fail-closed
        contract."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG",
                 "flow_confirmation": "supports_long", "timeframe": "15m"}
            ],
            "invalid_condition": None,
        }
        normalized, notes = normalize_opportunity_watch(watch, None)
        assert normalized is None, f"got {normalized!r}"
        assert any("fail-closed" in str(n) or "无法结构化" in str(n) for n in notes), notes


# ── 3. Schema-repair chain unit (``_try_repair_opportunity_watch``) ─────────


class TestTryRepairOpportunityWatch:
    def test_structured_watch_untouched(self) -> None:
        d = _decision(watch=_structured_watch())
        repaired, notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is False
        assert repaired is d
        assert notes == []

    def test_text_watch_rebuilt_from_plan(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            # A text blob invalid_condition (production evidence shape) must be
            # rebuilt from the plan's stop loss.
            "invalid_condition": "跌破 172",
        }
        d = _decision(watch=watch)
        repaired, notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True
        assert repaired["opportunity_watch"]["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m"}
        ], f"got {repaired['opportunity_watch']['conditions']!r}"
        assert repaired["opportunity_watch"]["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m"
        }
        assert notes, "rebuild must emit an audit note"

    def test_text_watch_fail_closed_drops_action(self) -> None:
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            "invalid_condition": None,
        }
        d = _decision(watch=watch)
        d["has_trade_plan"] = False
        del d["trade_plan"]
        repaired, notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True
        assert repaired["opportunity_watch"] is None
        assert "create_opportunity_watch" not in repaired["suggested_actions"]
        assert notes, "fail-closed must emit a diagnostic"

    def test_no_watch_no_plan_untouched(self) -> None:
        d = _decision(watch=None)
        d["has_trade_plan"] = False
        del d["trade_plan"]
        repaired, notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is False
        assert repaired is d
        assert notes == []

    def test_try_repair_cleans_schema_forbidden_condition_keys(self) -> None:
        """08-02 P2-2: an LLM watch that already carries ``side`` on every
        condition but ALSO smuggles schema-forbidden ``direction``/``symbol``
        keys must still be repaired (the tightened ``is_structured_watch``
        refuses the short-circuit) — never persisted schema-invalid."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": [
                {"type": "pullback", "side": "LONG", "level": 180.0,
                 "direction": "LONG", "symbol": "SOLUSDT"}
            ],
            "invalid_condition": {
                "type": "close_below", "side": "LONG", "level": 172.0,
                "direction": "LONG", "symbol": "SOLUSDT"
            },
        }
        d = _decision(watch=watch)
        repaired, _notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True
        assert repaired["opportunity_watch"]["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 180.0}
        ], f"got {repaired['opportunity_watch']['conditions']!r}"
        assert repaired["opportunity_watch"]["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 172.0
        }, f"got {repaired['opportunity_watch']['invalid_condition']!r}"
        ok, err = validate_json("ga_decision.schema.json", repaired)
        assert ok, f"repaired decision must pass the tightened schema; {err}"


# ── 4. Real single-attempt path: normalization happens pre-validation ──────


class TestWatchChainIntegration:
    """Through the real ``_run_single_llm_attempt`` the watch block in
    ``_normalize_llm_decision`` runs BEFORE schema validation, so a
    text-condition watch is normalized to a plain success (llm_terminal_reason
    None) — never a hard schema failure, never a repair event."""

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

    def test_text_watch_with_plan_normalized_to_structured_success(self) -> None:
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
                "take_profits": [{"price": 196.0, "ratio": 1.0}],
                "invalid_condition": "1H 跌破 170",
            },
            "opportunity_watch": {
                "needed": True,
                "direction": "LONG",
                "conditions": ["15M 收盘突破上沿或跌破下沿"],
                "invalid_condition": "跌破 172",
            },
            "suggested_actions": ["create_opportunity_watch"],
        }
        candidate, meta = self._run_attempt(json.dumps(payload, ensure_ascii=False))
        assert candidate is not None, f"text watch must normalize, not fail; meta={meta}"
        assert meta.get("llm_status") == "ok"
        assert meta.get("llm_terminal_reason") is None, (
            "normalize-in-_normalize_llm_decision is a plain success, not a repair"
        )
        watch = candidate.get("opportunity_watch")
        assert watch is not None
        assert watch["conditions"] == [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m"}
        ], f"got {watch['conditions']!r}"
        assert watch["invalid_condition"] == {
            "type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m"
        }
        ok, err = validate_json("ga_decision.schema.json", candidate)
        assert ok, f"normalized decision must pass the tightened schema; {err}"

    def test_text_watch_no_plan_fail_closed_drops_action(self) -> None:
        payload = {
            "symbol": "SOLUSDT",
            "analysis_time_utc": _ANALYSIS_TIME_UTC,
            "decision": "wait_for_pullback",
            "signal_grade": "B",
            "market_bias": "bullish",
            "trend_stage": "early",
            "confidence": 0.67,
            "summary": "等待回踩",
            "evidence": ["1H 反弹"],
            "counter_evidence": ["1D 仍下行"],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": {
                "needed": True,
                "direction": "LONG",
                "conditions": ["15M 收盘突破上沿或跌破下沿"],
                "invalid_condition": None,
            },
            "suggested_actions": ["create_opportunity_watch"],
        }
        candidate, meta = self._run_attempt(json.dumps(payload, ensure_ascii=False))
        assert candidate is not None, f"fail-closed must still be a success; meta={meta}"
        assert meta.get("llm_status") == "ok"
        assert candidate.get("opportunity_watch") is None
        assert "create_opportunity_watch" not in candidate.get("suggested_actions", []), (
            "fail-closed must drop the create action so the button never fires"
        )
        notes = candidate.get("risk_notes") or []
        assert any("fail-closed" in str(n) for n in notes), f"missing diagnostic; {notes}"
        ok, err = validate_json("ga_decision.schema.json", candidate)
        assert ok, err


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


# ── 5. Watcher: text / unknown conditions are UNTRIGGERABLE, never silent ───


def _seed_closed_candle(repo, symbol: str, *, close: float,
                        at_ms: int = _ANALYSIS_TIME_UTC) -> None:
    span = 900_000
    base = at_ms - span
    repo.upsert_candles([
        {
            "symbol": symbol,
            "interval": "15m",
            "open_time": base,
            "close_time": base + span - 1,
            "open": close - 1.0,
            "high": close + 1.0,
            "low": close - 1.5,
            "close": close,
            "volume": 1000,
            "is_closed": True,
        }
    ])


class TestWatcherUntriggerableReporting:
    def test_text_condition_reported_untriggerable(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id = repo.create_opportunity_watch(
                "BTCUSDT",
                {
                    "direction": "LONG",
                    "reason": "等待结构确认",
                    "conditions": ["15M 收盘突破上沿或跌破下沿"],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                },
            )
            watch = repo.get_opportunity_watch(watch_id)
            _seed_closed_candle(repo, "BTCUSDT", close=100.0)
            result = evaluate_watch(repo, watch, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert result["status"] == "waiting"
            assert result.get("untriggerable") is True, (
                "text conditions must surface the untriggerable marker, "
                "never silently wait forever"
            )
        finally:
            handle.close()

    def test_unknown_kind_condition_reported_untriggerable(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id = repo.create_opportunity_watch(
                "BTCUSDT",
                {
                    "direction": "LONG",
                    "reason": "等待结构确认",
                    "conditions": [{"type": "make_money", "side": "LONG", "level": 100.0}],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                },
            )
            watch = repo.get_opportunity_watch(watch_id)
            _seed_closed_candle(repo, "BTCUSDT", close=100.0)
            result = evaluate_watch(repo, watch, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert result["status"] == "waiting"
            assert result.get("untriggerable") is True
        finally:
            handle.close()

    def test_mixed_conditions_not_whole_watch_untriggerable(self) -> None:
        # 08-02 Finding 5 (P2) per-condition contract: the runtime
        # ``untriggerable`` marker means the WHOLE watch is dead (every
        # condition untriggerable — ``all(...)``). A MIXED watch (one
        # structured condition that can still trigger + one pre-fix bare-string
        # condition) must stay a plain waiting watch, NOT declared dead. The
        # dead sub-condition stays visible per-condition in ``condition_results``
        # so the P1-3 diagnostic (which flags ANY untriggerable condition)
        # never silently contradicts this all-watch runtime marker.
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id = repo.create_opportunity_watch(
                "BTCUSDT",
                {
                    "direction": "LONG",
                    "reason": "结构化条件可触发，文本条件为历史残留",
                    "conditions": [
                        {"type": "reclaim", "side": "LONG", "level": 100.0, "timeframe": "15m"},
                        "15M 收盘突破上沿或跌破下沿",
                    ],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                },
            )
            watch = repo.get_opportunity_watch(watch_id)
            _seed_closed_candle(repo, "BTCUSDT", close=99.0)
            result = evaluate_watch(repo, watch, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert result["status"] == "waiting"
            assert result.get("untriggerable") is not True, (
                "a mixed watch with a structured triggerable condition is not "
                "whole-watch-dead; only an ALL-untriggerable watch is dead"
            )
            per_condition = result.get("condition_results") or []
            untriggerable_flags = [c.get("untriggerable") for c in per_condition]
            assert any(untriggerable_flags), (
                "the dead sub-condition must stay visible per-condition so "
                "diagnostics/report can surface it"
            )
        finally:
            handle.close()

    def test_known_kind_not_yet_matched_is_plain_waiting(self) -> None:
        # reclaim with a SINGLE candle (no previous) is a known kind that has
        # not matched yet — plain waiting, NOT untriggerable.
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id = repo.create_opportunity_watch(
                "BTCUSDT",
                {
                    "direction": "LONG",
                    "reason": "等待 reclaim",
                    "conditions": [{"type": "reclaim", "side": "LONG", "level": 100.0, "timeframe": "15m"}],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                },
            )
            watch = repo.get_opportunity_watch(watch_id)
            _seed_closed_candle(repo, "BTCUSDT", close=99.0)
            result = evaluate_watch(repo, watch, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert result["status"] == "waiting"
            assert result.get("untriggerable") is not True, (
                "a supported kind merely not-yet-matched must stay plain waiting"
            )
        finally:
            handle.close()

    def test_update_opportunity_watches_surfaces_untriggerable(self) -> None:
        # Full scheduler path (mirrors production): the untriggerable watch
        # stays waiting AND the batch result carries the marker.
        handle = make_repo()
        try:
            repo = handle.repo
            repo.create_opportunity_watch(
                "BTCUSDT",
                {
                    "direction": "LONG",
                    "reason": "等待结构确认",
                    "conditions": ["15M 收盘突破上沿或跌破下沿"],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                },
            )
            _seed_closed_candle(repo, "BTCUSDT", close=100.0)
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["checked"] == 1
            assert update["triggered"] == 0
            assert len(update["results"]) == 1
            result = update["results"][0]
            assert result["status"] == "waiting"
            assert result.get("untriggerable") is True
        finally:
            handle.close()


# ── 6. Real e2e: B candidate -> one active watch -> triggered -> alert ─────


def _save_risk_approved_snapshot(repo, symbol: str = "BTCUSDT") -> int:
    """Mirror _smoke_suite._risk_approved_snapshot_id (phase04's snapshot)."""
    snapshot = {
        "symbol": symbol,
        "analysis_time_utc": 1_700_000_000_000,
        "mode": "ad_hoc",
        "profiles": {
            "1d": {"market_structure": "bullish", "trend_stage": "middle", "momentum": "bullish", "candles_count": 80},
            "4h": {"market_structure": "bullish", "trend_stage": "middle", "momentum": "bullish", "candles_count": 80},
            "1h": {"market_structure": "bullish", "trend_stage": "middle", "momentum": "bullish", "candles_count": 80},
            "15m": {"market_structure": "bullish", "trend_stage": "early", "momentum": "bullish", "candles_count": 80},
            "5m": {"market_structure": "bullish", "trend_stage": "early", "momentum": "bullish", "candles_count": 80},
        },
        "modules": {
            "market_regime": {"regime": "normal", "extreme": False, "evolution_trigger_allowed": True},
            "price_action": {
                "market_structure": "bullish",
                "structure_events": [
                    {
                        "event": "bullish_bos",
                        "timeframe": "15m",
                        "direction": "bullish",
                        "candle_close_time": 1700000000000,
                        "price": 98.5,
                        "closed": True,
                    },
                ],
            },
            "smc": {},
            "momentum": {"direction": "bullish"},
        },
        "counter_evidence": {
            "bullish_evidence": ["高周期方向支持"],
            "bearish_evidence": [],
            "neutral_or_risk_evidence": [],
            "contradiction_level": "low",
        },
        "data_quality": {
            "closed_candles_only": True, "status": "complete",
            "analysis_time_utc": 1_700_000_000_000,
            "missing_timeframes": [], "low_sample_timeframes": [],
            "health_by_tf": {
                "1d": {"ready": True, "last_close_time": 1_699_991_360_000},
                "4h": {"ready": True, "last_close_time": 1_699_997_200_000},
                "1h": {"ready": True, "last_close_time": 1_699_999_600_000},
                "15m": {"ready": True, "last_close_time": 1_700_000_000_000},
            },
        },
        "timeframe_context": {
            "1d": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": 1_699_991_360_000},
            "4h": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": 1_699_997_200_000},
            "1h": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": 1_699_999_600_000},
            "15m": {"bias": "bullish", "structure": "uptrend", "closed": True, "close_time": 1_700_000_000_000},
        },
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
        "paper_context": {},
        "global_context": {"time_policy": "closed candles only"},
    }
    return repo.save_market_snapshot(snapshot)


class TestE2EStructuredWatchFunnel:
    """Real end-to-end funnel (mirrors _smoke_suite phase04): a B-grade
    wait_for_pullback signal carries a STRUCTURED breakout watch; the button
    materializes ONE active watch; once the 15m K-line closes above the level,
    the watcher triggers it and enqueues opportunity_watch_alert; the next
    batch is idempotent (triggered=0)."""

    def test_structured_watch_materializes_triggers_and_alerts(self) -> None:
        from plugins.crypto_guard.run_ga_workers import handle_button_callback

        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            snapshot_id = _save_risk_approved_snapshot(repo, "BTCUSDT")
            signal_id = repo.create_signal(
                {
                    "symbol": "BTCUSDT",
                    "decision": "wait_for_pullback",
                    "signal_grade": "B",
                    "confidence": 0.67,
                    "summary": "测试机会监控",
                    "market_bias": "bullish",
                    "risk_notes": ["仅用于测试"],
                    "has_trade_plan": False,
                    "opportunity_watch": {
                        "needed": True,
                        "direction": "LONG",
                        "reason": "等待突破确认",
                        "conditions": [{"type": "breakout", "side": "LONG", "level": 101.0, "timeframe": "15m"}],
                        "invalid_condition": {"type": "close_below", "side": "LONG", "level": 95.0},
                        "expires_minutes": 60,
                    },
                },
                snapshot_id,
            )
            button = handle_button_callback(
                repo,
                {"action": "create_opportunity_watch", "symbol": "BTCUSDT", "signal_id": signal_id},
            )
            assert button["ok"] is True, f"button must succeed; {button}"
            watch = repo.get_opportunity_watch(button["watch_id"])
            assert watch["status"] == "active"
            assert watch["expires_at"] is not None

            span = 900_000
            base = 1_700_000_000_000
            repo.upsert_candles(
                [
                    {
                        "symbol": "BTCUSDT",
                        "interval": "15m",
                        "open_time": base,
                        "close_time": base + span - 1,
                        "open": 99.0,
                        "high": 100.5,
                        "low": 98.0,
                        "close": 100.0,
                        "volume": 1000,
                        "is_closed": True,
                    },
                    {
                        "symbol": "BTCUSDT",
                        "interval": "15m",
                        "open_time": base + span,
                        "close_time": base + span * 2 - 1,
                        "open": 100.0,
                        "high": 103.0,
                        "low": 99.5,
                        "close": 102.0,
                        "volume": 1200,
                        "is_closed": True,
                    },
                ]
            )
            update = update_opportunity_watches(repo, analysis_time_utc=base + span * 2 - 1)
            assert update["triggered"] == 1, f"watch must trigger; {update}"
            triggered_watch = repo.get_opportunity_watch(button["watch_id"])
            assert triggered_watch["status"] == "triggered"
            alerts = conn.execute(
                "SELECT * FROM agent_jobs WHERE job_type='opportunity_watch_alert'"
            ).fetchall()
            assert len(alerts) == 1, "triggered watch must enqueue one alert job"
            second = update_opportunity_watches(repo, analysis_time_utc=base + span * 2 - 1)
            assert second["triggered"] == 0, "next batch must be idempotent"
        finally:
            handle.close()
