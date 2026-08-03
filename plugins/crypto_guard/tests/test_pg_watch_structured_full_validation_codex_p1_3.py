# -*- coding: utf-8 -*-
"""Codex terminal-review P1-3: structured-watch validation is FULL.

Codex finding (verbatim essence):
    ``is_structured_watch`` 必须与 ga_decision.schema.json 和 watcher 语义
    完全一致。拒绝所有不在允许集合中的 envelope/condition/invalid_condition
    字段。校验 timeframe enum、数值范围、type 对应所需字段。每个
    condition/invalid_condition.side 必须与 watch.direction 一致。
    short-circuit 只有真正 schema-valid + watcher-valid 时才能返回 True。
    RED 测试至少覆盖 extra note key、非法 timeframe、相反 side、非法值；
    并证明 normalize/repair 会清洗或 fail-closed，而不是进入 schema failure。

Today ``is_structured_watch`` (the schema-repair short-circuit) only checks:
  - envelope ``direction`` is LONG/SHORT;
  - every condition passes ``is_structured_condition`` (kind + usable
    level/price);
  - condition/invalid side is in {LONG, SHORT};
  - condition/invalid does not carry ``direction``/``symbol``.
It does NOT reject:
  - an envelope key outside ``_WATCH_KEYS`` (e.g. ``note``) — ga_decision
    schema makes the envelope ``additionalProperties: false``;
  - a condition/invalid key outside the schema's condition property set
    (``note``, ``kind``, anything) — schema condition items are also
    ``additionalProperties: false``;
  - an illegal ``timeframe`` (e.g. ``"30m"``) — the schema enum is
    1m/5m/15m/1h/4h and the watcher's ``_watch_timeframe`` would evaluate a
    bogus timeframe;
  - a non-numeric / negative ``tolerance_pct`` — the schema types it number;
    ``value``/``flow_confirmation`` were REMOVED from the schema key set in
    08-02 R2 P2-2, so a condition carrying either is rejected outright;
  - a condition/invalid ``side`` that DIFFERS from ``watch.direction`` —
    ``opportunity_watcher._condition_hit`` reads ``condition.side`` FIRST and
    falls back to the watch direction, so a SHORT condition on a LONG watch
    makes the watcher evaluate the OPPOSITE direction.
Because the repair chain short-circuits on ``is_structured_watch``
(``_try_repair_opportunity_watch`` returns the watch unchanged when it returns
True), every one of those schema-invalid / watcher-inconsistent watches
persists untouched. RED-first + revert-fail: each test drives the real
predicate / normalizer / repair chain and asserts the fail-closed output;
reverting the tightened checks flips the assertions back to RED.

NOTE on which layer each check lives at (locked by existing 08-02 tests):
  - ``is_structured_condition`` stays PERMISSIVE about extra keys — the
    normalizer's keep-loop keeps such conditions and ``_clean_condition``
    strips the unknown keys (``test_structured_llm_condition_kept_and_side_
    injected``, ``test_normalize_drops_schema_forbidden_condition_keys``).
  - ``is_structured_watch`` is the STRICT predicate (the short-circuit): it
    must equal schema-valid + watcher-valid, so all the strict checks live
    here.
  - ``normalize_opportunity_watch`` REPAIRS: aligns every condition/invalid
    ``side`` to the resolved direction and ``_clean_condition`` drops illegal
    ``timeframe`` / ``tolerance_pct`` / non-string flow/value — so the
    normalized output is always schema-valid or None (fail-closed), never a
    schema failure.
"""

from __future__ import annotations

import pytest

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.watch_conditions import (
    is_structured_watch,
    normalize_opportunity_watch,
)
from plugins.crypto_guard.reasoning.llm_agent_judge import (
    _try_repair_opportunity_watch,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_ANALYSIS_TIME_UTC = 1785487499999


def _structured_watch() -> dict:
    """A fully schema-valid + watcher-valid watch (control)."""
    return {
        "needed": True,
        "direction": "LONG",
        "reason": "等待回踩确认",
        "conditions": [{"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m"}],
        "invalid_condition": {"type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m"},
        "expires_minutes": 120,
    }


def _decision(*, watch: object) -> dict:
    """A schema-complete decision whose ``opportunity_watch`` is the test knob."""
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
        "timeframe_context": {
            tf: {"bias": "bullish", "structure": "bullish", "closed": True,
                 "close_time": _ANALYSIS_TIME_UTC - 60_000}
            for tf in ("1d", "4h", "1h", "15m")
        },
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
    }


def _assert_schema_valid(watch: object) -> None:
    ok, err = validate_json("ga_decision.schema.json", _decision(watch=watch))
    assert ok, f"watch must be schema-valid; {err}"


def _clone(base: dict, **overrides) -> dict:
    import copy
    out = copy.deepcopy(base)
    out.update(overrides)
    return out


# ── 1. is_structured_watch: STRICT schema+watcher predicate ─────────────────


class TestIsStructuredWatchFullValidationCodexP1_3:
    def test_accepts_schema_valid_watch(self) -> None:
        """GREEN both (control): the schema-valid + watcher-valid watch is
        still structured — the tightening must not over-reject."""
        assert is_structured_watch(_structured_watch()) is True

    def test_rejects_extra_envelope_key(self) -> None:
        """RED->GREEN: an envelope ``note`` key is ``additionalProperties:
        false`` in ga_decision.schema.json; today is_structured_watch ignores
        envelope keys -> True -> short-circuits the repair and the schema-
        invalid watch persists."""
        watch = _clone(_structured_watch(), note="内嵌提示")
        assert is_structured_watch(watch) is False, (
            "envelope must not carry keys outside the schema property set")

    def test_rejects_extra_condition_key(self) -> None:
        """RED->GREEN: a ``note`` key inside a condition is also
        ``additionalProperties: false``; today only direction/symbol are
        checked -> True."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m",
             "note": "x"}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_extra_invalid_condition_key(self) -> None:
        """RED->GREEN: same for the ``invalid_condition`` object."""
        watch = _clone(_structured_watch())
        watch["invalid_condition"] = {
            "type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m",
            "note": "x",
        }
        assert is_structured_watch(watch) is False

    def test_rejects_illegal_timeframe_on_condition(self) -> None:
        """RED->GREEN: ``30m`` is not in the schema enum (1m/5m/15m/1h/4h);
        the watcher's ``_watch_timeframe`` would query a bogus timeframe."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "30m"}
        ]
        assert is_structured_watch(watch) is False, (
            "timeframe must be one of 1m/5m/15m/1h/4h")

    def test_rejects_illegal_timeframe_on_invalid_condition(self) -> None:
        """RED->GREEN: same for the ``invalid_condition`` timeframe."""
        watch = _clone(_structured_watch())
        watch["invalid_condition"] = {
            "type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "30m"
        }
        assert is_structured_watch(watch) is False

    def test_rejects_opposite_side_on_condition(self) -> None:
        """RED->GREEN: a SHORT condition on a LONG watch evaluates the OPPOSITE
        direction in ``opportunity_watcher._condition_hit`` (``condition.side``
        is read first); such a watch must not be considered structured."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "SHORT", "level": 180.0, "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False, (
            "every condition.side must equal watch.direction")

    def test_rejects_opposite_side_on_invalid_condition(self) -> None:
        """RED->GREEN: same for ``invalid_condition.side``."""
        watch = _clone(_structured_watch())
        watch["invalid_condition"] = {
            "type": "close_above", "side": "SHORT", "level": 172.0, "timeframe": "15m"
        }
        assert is_structured_watch(watch) is False

    def test_rejects_non_numeric_tolerance_pct(self) -> None:
        """RED->GREEN: ``tolerance_pct`` is schema ``number, minimum 0``; a
        string leaks an un-parseable tolerance into the watcher's pullback
        tolerance arithmetic."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m",
             "tolerance_pct": "tight"}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_negative_tolerance_pct(self) -> None:
        """RED->GREEN: a negative ``tolerance_pct`` violates ``minimum 0``."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m",
             "tolerance_pct": -0.5}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_non_string_flow_confirmation_value(self) -> None:
        """RED->GREEN: 08-02 R2 P2-2 removed ``flow_confirmation``/``value``
        from the schema condition key set (``additionalProperties: false``), so
        a condition carrying either is rejected outright regardless of type."""
        for key in ("flow_confirmation", "value"):
            watch = _clone(_structured_watch())
            watch["conditions"] = [
                {"type": "pullback", "side": "LONG", "level": 180.0,
                 key: 123, "timeframe": "15m"}
            ]
            assert is_structured_watch(watch) is False, f"{key} must be rejected"


# ── 2. normalize_opportunity_watch: REPAIR, never schema failure ────────────


class TestNormalizeRepairCleansCodexP1_3:
    def test_repair_drops_extra_envelope_key(self) -> None:
        """GREEN both (proof): normalize rebuilds the envelope from known keys,
        so an extra envelope ``note`` is dropped and the output is schema-valid
        (never a schema failure)."""
        watch = _clone(_structured_watch(), note="内嵌提示")
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert "note" not in normalized
        _assert_schema_valid(normalized)

    def test_repair_drops_extra_condition_key(self) -> None:
        """GREEN both (proof): ``_clean_condition`` strips unknown condition
        keys, so a ``note`` key is cleaned, not persisted."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m",
             "note": "x"}
        ]
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert "note" not in normalized["conditions"][0]
        _assert_schema_valid(normalized)

    def test_repair_never_emits_illegal_timeframe(self) -> None:
        """RED->GREEN: today the keep-loop preserves an illegal ``30m``
        timeframe, so the normalized output FAILS ga_decision.schema.json (the
        schema failure the finding forbids). The fix drops the illegal
        timeframe; the watcher's ``_watch_timeframe`` then falls back to 15m."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "30m"}
        ]
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert normalized["conditions"][0].get("timeframe") not in {"30m"}, (
            normalized["conditions"])
        _assert_schema_valid(normalized)

    def test_repair_never_emits_illegal_tolerance_pct(self) -> None:
        """RED->GREEN: a non-numeric ``tolerance_pct`` is dropped, never
        persisted into a schema-invalid watch."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "15m",
             "tolerance_pct": "tight"}
        ]
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert "tolerance_pct" not in normalized["conditions"][0]
        _assert_schema_valid(normalized)

    def test_repair_aligns_condition_sides_to_direction(self) -> None:
        """RED->GREEN: today a SHORT condition on a LONG watch survives
        normalize with side SHORT — schema passes (side enum is checked, the
        watch-level direction is not cross-checked) but the watcher evaluates
        the OPPOSITE direction. The fix aligns every condition/invalid side to
        the resolved direction."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "SHORT", "level": 180.0, "timeframe": "15m"}
        ]
        watch["invalid_condition"] = {
            "type": "close_above", "side": "SHORT", "level": 172.0, "timeframe": "15m"
        }
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert normalized["direction"] == "LONG"
        assert normalized["conditions"][0]["side"] == "LONG", normalized["conditions"]
        assert normalized["invalid_condition"]["side"] == "LONG", (
            normalized["invalid_condition"])
        _assert_schema_valid(normalized)

    def test_repair_fail_closed_unrepairable_never_emits_schema_invalid(self) -> None:
        """GREEN both (proof): a watch with only an unbuildable text condition
        and no plan fail-closes to None (with a diagnostic) — never a
        schema-invalid watch."""
        watch = {
            "needed": True,
            "direction": "LONG",
            "conditions": ["15M 收盘突破上沿或跌破下沿"],
            "invalid_condition": None,
        }
        normalized, notes = normalize_opportunity_watch(watch, None)
        assert normalized is None
        assert any("fail-closed" in n for n in notes), notes


# ── 3. repair chain (``_try_repair_opportunity_watch``) ─────────────────────


class TestTryRepairOpportunityWatchCodexP1_3:
    def test_short_circuit_refuses_schema_invalid_watch(self) -> None:
        """RED->GREEN: the repair chain must NOT short-circuit a watch that
        carries a schema-invalid shape (extra envelope key here). Today
        ``is_structured_watch`` returns True, so ``changed`` stays False and
        the schema-invalid watch persists untouched."""
        watch = _clone(_structured_watch(), note="内嵌提示")
        d = _decision(watch=watch)
        repaired, _notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True, (
            "a schema-invalid watch must be repaired, not short-circuited")
        assert repaired["opportunity_watch"] is not None
        _assert_schema_valid(repaired["opportunity_watch"])

    def test_illegal_timeframe_watch_repaired_to_schema_valid(self) -> None:
        """RED->GREEN: a watch whose only defect is an illegal condition
        timeframe is repaired (timeframe dropped), never persisted as a schema
        failure."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0, "timeframe": "30m"}
        ]
        d = _decision(watch=watch)
        repaired, _notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True
        assert repaired["opportunity_watch"] is not None
        assert repaired["opportunity_watch"]["conditions"][0].get("timeframe") not in {"30m"}
        _assert_schema_valid(repaired["opportunity_watch"])

    def test_opposite_side_watch_repaired_aligned(self) -> None:
        """RED->GREEN: a watch with a condition side opposite to the direction
        is repaired to a single coherent direction, never persisted with a
        condition the watcher would evaluate in the OPPOSITE direction."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "SHORT", "level": 180.0, "timeframe": "15m"}
        ]
        d = _decision(watch=watch)
        repaired, _notes, changed = _try_repair_opportunity_watch(d, None)
        assert changed is True
        assert repaired["opportunity_watch"] is not None
        for cond in repaired["opportunity_watch"]["conditions"]:
            assert cond["side"] == "LONG", cond
        _assert_schema_valid(repaired["opportunity_watch"])
