# -*- coding: utf-8 -*-
"""Codex terminal-review P2 rework (2026-08-03): fresh-reviewer P2 findings
P2-1 / P2-2 / P2-3 — RED-first + revert-fail regression tests.

Fresh reviewer verdict on the P1-1..P1-4 rework freeze: no P0/P1, five P2
findings. P2-5 (duplicate test coverage) is accepted-by-design — the standing
constraint forbids deleting/relaxing tests, so no code change there. These
tests drive the three REAL fixes:

  P2-1 corrupt execution-funnel marker (diagnostics/report_diagnostics.py):
    Of the three readers of the ``execution_funnel_report_contract_v1`` marker,
    two already FAIL CLOSED on a corrupt/unparseable value
    (``_execution_funnel_starvation_lower_bound_ts`` -> ``now``;
    ``_apply_execution_funnel_marker_cutoff`` -> no demotion) but the two
    remaining readers did NOT: ``_execution_funnel_check_created_at_lower_bound``
    returned the RAW marker string (a garbage value would be interpolated into
    ``created_at >= %s::timestamptz`` — a psycopg datetime crash) and
    ``_check_execution_funnel_report_contract_marker_missing`` only fired on an
    ABSENT row, so a present-but-corrupt marker was SILENT GREEN. Fix: the lower
    bound parses the marker (try/except ``datetime.fromisoformat``) and FAILS
    CLOSED to ``now`` on a corrupt value (nothing is provably post-marker,
    mirroring the starvation helper), and the marker-missing check fires on a
    corrupt value too (issue=marker_corrupt) so corruption is never silent.
    NOTE: ``_migration_state.applied_at`` is TIMESTAMPTZ, so a garbage literal
    cannot be stored end-to-end — the corrupt state is driven at the white-box
    level (repo-shaped stub data source), the same adversarial-input pattern as
    the P1-4 ``_decision_row`` tests. Nothing is mocked: the REAL production
    functions are driven. RED: pre-fix ``_execution_funnel_check_created_at_
    lower_bound`` returns the garbage marker verbatim and the marker-missing
    check returns [] (corrupt = silent).

  P2-2 condition ``level``/``price`` type hole (reasoning/watch_conditions.py):
    ``_is_schema_condition`` never type-checked ``level``/``price`` (schema:
    ``number, minimum 0``). ``is_structured_condition`` only checks the FIRST
    usable field, so a condition with a valid ``level`` PLUS a garbage
    ``price`` (``"abc"`` / negative) passed ``is_structured_watch`` -> the
    repair short-circuit left a schema-invalid watch persisted. Fix:
    ``_is_schema_condition`` rejects non-numeric/negative ``level``/``price``;
    ``_clean_condition`` DROPS the garbage field so the repaired watch is
    schema-valid (never a schema failure).
    RED: pre-fix ``is_structured_watch`` returns True and
    ``normalize_opportunity_watch`` emits the garbage ``price`` (schema
    failure).

  P2-3 watch envelope ``expires_minutes`` type hole (watch_conditions.py):
    ``is_structured_watch`` never type-checked ``expires_minutes`` (schema:
    ``["integer", "null"]``), so a string/float/bool value short-circuited the
    repair. Fix: reject any ``expires_minutes`` that is not None and not a
    non-bool int. The repair path already coerces garbage to
    ``_DEFAULT_EXPIRES_MINUTES``.
    RED: pre-fix ``is_structured_watch`` returns True for ``"abc"`` / 120.0 /
    True.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.diagnostics.report_diagnostics import (
    EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING,
    _check_execution_funnel_report_contract_marker_missing,
    _execution_funnel_check_created_at_lower_bound,
)
from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.watch_conditions import (
    is_structured_condition,
    is_structured_watch,
    normalize_opportunity_watch,
)


# ── P2-1 corrupt-marker white-box helpers ────────────────────────────────────


def _stub_marker_repo(applied_at: object):
    """A repo-shaped stub whose ``conn.execute(...).fetchone()`` returns one
    ``_migration_state`` row with the given ``applied_at``.

    The corrupt state cannot be stored in the real TIMESTAMPTZ column, so the
    REAL marker readers are driven with this controlled data source (the same
    white-box adversarial-input pattern as the P1-4 ``_decision_row`` tests).
    No production function is mocked — only the row the real functions read.
    """
    class _Cursor:
        def __init__(self, value: object) -> None:
            self._value = value

        def fetchone(self):
            return {"applied_at": self._value}

    class _Conn:
        def __init__(self, value: object) -> None:
            self._value = value

        def execute(self, query: str, params=None) -> _Cursor:
            return _Cursor(self._value)

    class _Repo:
        pass

    repo = _Repo()
    repo.conn = _Conn(applied_at)
    return repo


class TestExecutionFunnelCorruptMarkerFailClosedCodexP2_1(unittest.TestCase):
    """A corrupt (unparseable) execution-funnel marker value must FAIL CLOSED
    in BOTH marker readers that the P1-3/P1-4 rework left unguarded:

    - ``_execution_funnel_check_created_at_lower_bound`` must NOT return the
      raw garbage marker verbatim (which the four per-decision checks would
      interpolate into ``created_at >= %s::timestamptz`` — a psycopg datetime
      crash). RED: pre-fix it returns ``not-a-date`` verbatim.
    - ``_check_execution_funnel_report_contract_marker_missing`` must surface a
      present-but-corrupt marker as ``execution_funnel_report_contract_marker_
      missing`` with ``details.issue == "marker_corrupt"``. RED: pre-fix it
      returns [] (corrupt = SILENT GREEN — the fail-open the other two readers
      already guard against).

    ``_execution_funnel_starvation_lower_bound_ts`` and
    ``_apply_execution_funnel_marker_cutoff`` already fail closed on corrupt
    values (verified by their existing tests); this class proves the two
    remaining readers now match that contract."""

    def test_lower_bound_never_returns_corrupt_marker_verbatim(self) -> None:
        repo = _stub_marker_repo("not-a-date")
        bound = _execution_funnel_check_created_at_lower_bound(repo)
        self.assertNotIn("not-a-date", bound)
        # The returned bound must be a parseable, timezone-aware timestamp.
        parsed = datetime.fromisoformat(bound.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)

    def test_lower_bound_fails_closed_to_now_on_corrupt(self) -> None:
        repo = _stub_marker_repo("not-a-date")
        now_dt = datetime.now(timezone.utc)
        bound = _execution_funnel_check_created_at_lower_bound(repo)
        bound_dt = datetime.fromisoformat(bound.replace("Z", "+00:00"))
        # Fail-closed: nothing provably post-marker -> bound is now (within a
        # generous delta), NOT a pre-marker window that would re-evaluate
        # historical rows as current.
        self.assertLessEqual((now_dt - bound_dt).total_seconds(), 5.0)

    def test_lower_bound_absent_marker_still_now_24h(self) -> None:
        """Control: the absent-marker branch (now-24h) is unchanged."""
        repo = _stub_marker_repo(None)
        bound = _execution_funnel_check_created_at_lower_bound(repo)
        bound_dt = datetime.fromisoformat(bound.replace("Z", "+00:00"))
        self.assertAlmostEqual(
            (datetime.now(timezone.utc) - bound_dt).total_seconds(),
            86400.0, delta=60.0,
        )

    def test_lower_bound_valid_marker_passthrough(self) -> None:
        """Control: a VALID marker passes through unchanged (SQL handles its
        native format)."""
        valid = "2026-08-02T12:00:00Z"
        repo = _stub_marker_repo(valid)
        self.assertEqual(_execution_funnel_check_created_at_lower_bound(repo), valid)

    def test_marker_corrupt_surfaced_not_silent(self) -> None:
        """RED->GREEN: present-but-corrupt fires marker_corrupt (pre-fix [])."""
        repo = _stub_marker_repo("not-a-date")
        issues = _check_execution_funnel_report_contract_marker_missing(repo)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["type"], EXECUTION_FUNNEL_REPORT_CONTRACT_MARKER_MISSING)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["details"].get("issue"), "marker_corrupt")

    def test_marker_garbage_number_corrupt_surfaced(self) -> None:
        """A non-string garbage value (e.g. a legacy TEXT row re-typed) is also
        surfaced as corrupt."""
        repo = _stub_marker_repo(12345)
        issues = _check_execution_funnel_report_contract_marker_missing(repo)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["details"].get("issue"), "marker_corrupt")

    def test_marker_absent_still_marker_absent(self) -> None:
        """Control: an ABSENT marker still fires marker_absent (unchanged)."""
        repo = _stub_marker_repo(None)
        issues = _check_execution_funnel_report_contract_marker_missing(repo)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["details"].get("issue"), "marker_absent")

    def test_marker_valid_no_missing_issue(self) -> None:
        """Control: a valid marker fires no marker-missing issue."""
        repo = _stub_marker_repo("2026-08-02T12:00:00Z")
        issues = _check_execution_funnel_report_contract_marker_missing(repo)
        self.assertEqual(issues, [])


# ── P2-2 / P2-3 watch schema helpers ────────────────────────────────────────

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


# ── P2-2 condition level/price type hole ────────────────────────────────────


class TestSchemaLevelPriceTypeCodexP2_2:
    def test_accepts_valid_level_and_price(self) -> None:
        """GREEN both (control): a condition carrying BOTH a valid ``level`` and
        a valid ``price`` is schema-valid and stays structured."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0,
             "price": 175.0, "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is True

    def test_rejects_string_price_alongside_valid_level(self) -> None:
        """RED->GREEN: ``is_structured_condition`` checks only the FIRST usable
        field (``level`` here), so a garbage string ``price`` next to a valid
        ``level`` passed the pre-fix ``is_structured_watch`` -> the repair
        short-circuit left a schema-invalid watch persisted."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0,
             "price": "abc", "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_negative_price_alongside_valid_level(self) -> None:
        """RED->GREEN: schema ``price`` is ``number, minimum 0``."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0,
             "price": -5, "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_string_level(self) -> None:
        """RED->GREEN: a string ``level`` is schema-invalid (the pre-fix
        ``is_structured_condition`` already rejects it, but the strict predicate
        must too — the check lives at the schema layer, not only the usable-
        field layer)."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": "abc", "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False

    def test_rejects_negative_level(self) -> None:
        """RED->GREEN: schema ``level`` is ``number, minimum 0``."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": -5, "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False

    def test_normalize_drops_garbage_price(self) -> None:
        """RED->GREEN: the repair chain must ALSO clean the garbage sibling so
        the normalized output is schema-valid (the P1-3 contract: never a
        schema failure). Pre-fix ``_clean_condition`` copied ``price`` verbatim
        -> normalized output FAILED ga_decision.schema.json."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "pullback", "side": "LONG", "level": 180.0,
             "price": "abc", "timeframe": "15m"}
        ]
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert "price" not in normalized["conditions"][0], normalized["conditions"]
        _assert_schema_valid(normalized)


# ── P2-3 watch envelope expires_minutes type hole ───────────────────────────


class TestSchemaExpiresMinutesTypeCodexP2_3:
    def test_rejects_string_expires_minutes(self) -> None:
        """RED->GREEN: schema ``expires_minutes`` is ``["integer", "null"]``; a
        string short-circuited the repair pre-fix."""
        watch = _clone(_structured_watch(), expires_minutes="abc")
        assert is_structured_watch(watch) is False

    def test_rejects_float_expires_minutes(self) -> None:
        """RED->GREEN: a float is not a JSON integer."""
        watch = _clone(_structured_watch(), expires_minutes=120.0)
        assert is_structured_watch(watch) is False

    def test_rejects_bool_expires_minutes(self) -> None:
        """RED->GREEN: JSON bool is NOT the ``integer`` type (Python's bool is
        an int subclass — the check must exclude it explicitly)."""
        watch = _clone(_structured_watch(), expires_minutes=True)
        assert is_structured_watch(watch) is False

    def test_accepts_none_expires_minutes(self) -> None:
        """GREEN both (control): ``null`` is schema-allowed and the watcher
        falls back to its default expiry."""
        watch = _clone(_structured_watch(), expires_minutes=None)
        assert is_structured_watch(watch) is True

    def test_accepts_absent_expires_minutes(self) -> None:
        """GREEN both (control): the schema does NOT require ``expires_minutes``."""
        watch = _clone(_structured_watch())
        del watch["expires_minutes"]
        assert is_structured_watch(watch) is True

    def test_normalize_coerces_garbage_expires_minutes(self) -> None:
        """GREEN both (proof): the repair path defaults garbage
        ``expires_minutes`` to ``_DEFAULT_EXPIRES_MINUTES`` so the output is
        always schema-valid."""
        watch = _clone(_structured_watch(), expires_minutes="abc")
        normalized, _notes = normalize_opportunity_watch(watch, None)
        assert normalized is not None
        assert isinstance(normalized["expires_minutes"], int)
        assert not isinstance(normalized["expires_minutes"], bool)
        _assert_schema_valid(normalized)


# ── fresh-reviewer round 2 P2: envelope invalid_condition key required ──────


class TestSchemaInvalidConditionRequiredKeyCodexP2_6:
    """Fresh independent reviewer (round 2) P2: ``is_structured_watch`` never
    required the envelope key ``invalid_condition``.

    ga_decision.schema.json:64 lists it in the opportunity_watch ``required``
    set (["needed","direction","conditions","invalid_condition"]). Pre-fix, a
    watch missing the key returned True via ``watch.get(...)`` -> None ->
    ``is_structured_invalid_condition(None)`` -> True, so the repair
    short-circuit let a schema-invalid watch persist raw (or hard-fail
    ``validate_json`` downstream — normalize is what always emits the key).

    Fix: the KEY must be PRESENT. Its VALUE may be null (schema type
    ``["object","null"]``) — that is the legitimate "no invalidation" path.
    RED: pre-fix ``is_structured_watch`` returns True for a watch whose
    ``invalid_condition`` key is deleted."""

    def test_rejects_missing_invalid_condition_key(self) -> None:
        """RED->GREEN: the key is schema-required; an absent key must NOT
        short-circuit the repair chain."""
        watch = _clone(_structured_watch())
        del watch["invalid_condition"]
        assert is_structured_watch(watch) is False

    def test_accepts_none_invalid_condition_value(self) -> None:
        """GREEN both (control): the key PRESENT with a null value is schema
        ``["object","null"]``-valid and means "no invalidation"."""
        watch = _clone(_structured_watch(), invalid_condition=None)
        assert is_structured_watch(watch) is True


# ── fresh-reviewer round 3 P2: cvd trigger-field type hole ──────────────────


class TestSchemaCvdRemovalCodexR2P0:
    """08-02 Codex terminal-review round 2 P0: ``cvd_confirmation`` is REMOVED
    as a watch condition kind.

    Pre-fix ``opportunity_watcher._condition_hit`` compared only the condition's
    OWN persisted ``flow_confirmation`` string — never the real order-flow CVD —
    so a ``supports_long`` watch fired IMMEDIATELY on a static string match and
    ``supports_short`` / divergence values never fired at all. With no
    analysis-time-aligned order-flow read in the repository, the conservative
    path is taken: the kind is dropped from the schema AND the
    ``SUPPORTED_WATCH_CONDITION_KINDS`` set, the normalizer rebuilds price
    conditions from the trade plan, and an unbuildable watch fail-closes to
    None (never auto-triggers). A persisted ``flow_confirmation`` can never
    stand in for live CVD.

    This class SUPERSEDES the old ``TestSchemaCvdNonStringTriggerCodexP2_7``
    (which treated a non-empty string cvd trigger as a usable, keepable
    condition). Under the P0 contract a cvd condition is ALWAYS dropped and
    never kept — REGARDLESS of its trigger type.

    RED (revert-fail): restoring the old cvd kind + string-trigger acceptance
    makes the ``is_structured_condition`` assertions below go True / the
    rebuild assertions drop to the string condition, so the suite goes RED.
    """

    def test_is_structured_condition_never_cvd(self) -> None:
        """RED->GREEN: cvd_confirmation is no longer a supported kind, so
        ``is_structured_condition`` is False for EVERY trigger variant —
        including the old non-empty-string "usable" triggers that fired
        immediately."""
        for trigger in (
            {"flow_confirmation": True},
            {"value": 123},
            {"value": False},
            {"flow_confirmation": ""},
            {"flow_confirmation": "supports_long"},
            {"flow_confirmation": "divergence"},   # old accepted control -> now rejected
            {"value": "positive"},                 # old accepted control -> now rejected
        ):
            cond = {"type": "cvd_confirmation", "side": "LONG",
                    "timeframe": "15m", **trigger}
            assert is_structured_condition(cond) is False, cond

    def test_persisted_flow_confirmation_is_not_live_cvd(self) -> None:
        """Contract guard (P0): a cvd_confirmation watch can never be treated
        as a triggerable/materializable watch. ``is_structured_watch`` (the
        single gate shared by the worker auto-materialize, manual button, and
        controller paths) must be False because the kind is unsupported."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "cvd_confirmation", "side": "LONG",
             "flow_confirmation": "supports_long", "timeframe": "15m"}
        ]
        assert is_structured_watch(watch) is False

    def test_normalize_failcloses_unbuildable_cvd(self) -> None:
        """RED->GREEN: a watch whose ONLY condition is cvd_confirmation, with
        no plan supplied, fail-closes to None — never persisted as a
        permanent-wait "triggerable" CVD watch."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "cvd_confirmation", "side": "LONG",
             "flow_confirmation": "supports_long", "timeframe": "15m"}
        ]
        normalized, notes = normalize_opportunity_watch(watch, None)
        assert normalized is None, normalized
        assert any("fail-closed" in n or "无法结构化" in n for n in notes), notes

    def test_normalize_rebuilds_cvd_from_plan(self) -> None:
        """RED->GREEN: with a trade plan the cvd condition is dropped and
        REBUILT deterministically as price conditions (pullback toward the
        entry level), and the watch is schema-valid."""
        watch = _clone(_structured_watch())
        watch["conditions"] = [
            {"type": "cvd_confirmation", "side": "LONG",
             "flow_confirmation": "supports_long", "timeframe": "15m"}
        ]
        plan = {
            "side": "LONG", "entry_type": "limit", "entry_price": 180.0,
            "stop_loss": 172.0,
            "take_profits": [{"price": 196.0, "ratio": 1.0}],
        }
        normalized, _notes = normalize_opportunity_watch(watch, plan)
        assert normalized is not None
        assert len(normalized["conditions"]) == 1, normalized["conditions"]
        rebuilt = normalized["conditions"][0]
        assert rebuilt["type"] == "pullback", rebuilt
        assert rebuilt["level"] == 180.0, rebuilt
        assert is_structured_condition(rebuilt) is True
        _assert_schema_valid(normalized)

    def test_normalize_rebuilds_cvd_invalid_from_plan(self) -> None:
        """RED->GREEN: a cvd ``invalid_condition`` is rebuilt from the plan's
        stop loss (LONG -> close_below stop), never kept as a fake CVD shell."""
        watch = _clone(_structured_watch())
        watch["invalid_condition"] = {
            "type": "cvd_confirmation", "side": "LONG",
            "flow_confirmation": "supports_long", "timeframe": "15m"}
        plan = {
            "side": "LONG", "entry_type": "limit", "entry_price": 180.0,
            "stop_loss": 172.0,
            "take_profits": [{"price": 196.0, "ratio": 1.0}],
        }
        normalized, _notes = normalize_opportunity_watch(watch, plan)
        assert normalized is not None
        inv = normalized["invalid_condition"]
        assert inv is not None, normalized["invalid_condition"]
        assert inv["type"] == "close_below", inv
        assert inv["level"] == 172.0, inv
        assert is_structured_condition(inv) is True
        _assert_schema_valid(normalized)
