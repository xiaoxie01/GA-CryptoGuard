"""P1-3 (07-22 production review): producer-side direction-flip gate.

CryptoGuard 2026-07-24 production review (Codex) found that a direction
flip in ``candidate_trade_plan`` could be proposed WITHOUT closed-candle
breakout/failure evidence. The post-hoc diagnostic
``report_diagnostics._check_direction_flip_without_closed_candle`` only
warns AFTER the decision is persisted; it never stops the producer from
emitting the flipped candidate in the first place. The fix adds a
producer-side gate inside ``ga_judge.run_ga_sop_decision``: when the prior
round had a concrete side and the current candidate trade_plan flips to
the opposite side, the flip MUST be backed by a closed-candle structural
break (BOS / BREAK_OF_STRUCTURE / CHOCH / CHANGE_OF_CHARACTER / BREAKOUT /
BREAKDOWN) whose candle-close time is strictly after the previous decision
and not after the current analysis time and whose direction matches the
new side. Without it, the candidate plan is withheld (preserved as
``candidate_trade_plan`` for audit), the decision is downgraded to
``wait_for_pullback`` (keep observing), and a structured ``plan_blockers``
entry with code ``direction_flip_without_closed_candle_confirmation`` is
recorded. Existing execution blocking (risk_gate, the legacy
``side_invalidated`` continuity gate) stays as defense-in-depth.

These tests drive the REAL producer->consumer chain:
``build_market_state_snapshot`` (real market_state_builder on isolated PG)
-> ``attach_analysis_continuity_to_snapshot`` (real decision_context, with
a real previous_row read back from ``analysis_states`` via
``repo.latest_analysis_state_for_continuity``) -> ``run_ga_sop_decision``
(real ga_judge). No mocks of the function under test.

Isolated PostgreSQL fixture only. No production DB mutation, no service
restart, no commit/push/finish-work.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.reasoning.market_state_builder import (
    build_market_state_snapshot,
)
from plugins.crypto_guard.reasoning.decision_context import (
    attach_analysis_continuity_to_snapshot,
)
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision

# A bullish shadow_test snapshot on one timeframe produces a real LONG
# trade_plan (score 0.72, grade B, bias bullish) but its only structure
# event is ``range_bound`` (type "none") — there is NO bullish breakout
# event. That is the ideal "flip without confirmation" baseline. The
# tests inject a production-shape bullish BOS event to prove the gate
# accepts a real closed-candle breakout, and vary the previous decision
# time (prev_ts) relative to that BOS close_time to prove the gate
# rejects an event that closed BEFORE the flip point.
_LAST_CLOSE = 1_783_155_599_999
_SPAN_15M = 15 * 60 * 1000
_ANALYSIS_TIME = _LAST_CLOSE + 1
_SYMBOL = "TESTUSDT"


def _seed_bullish_candles(repo) -> None:
    """Seed 260 strongly trending-up 15m candles with periodic pullbacks.

    Mirrors the Pass 7 P0 fixture: trending 100 -> ~250 with small
    oscillations so swings form a bullish structure and the deterministic
    SOP produces a real LONG trade_plan (score 0.72, grade B).
    """
    base_close = _LAST_CLOSE - (260 - 1) * _SPAN_15M
    candles = []
    for i in range(260):
        ct_close = base_close + i * _SPAN_15M
        trend = 100.0 + i * 0.6
        pullback = 2.0 * math.sin(i / 5.0)
        open_p = trend + pullback - 0.3
        close_p = trend + pullback + 0.3
        high_p = max(open_p, close_p) + 0.8
        low_p = min(open_p, close_p) - 0.8
        candles.append({
            "symbol": _SYMBOL,
            "interval": "15m",
            "open_time": ct_close - _SPAN_15M + 1,
            "close_time": ct_close,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 1000.0,
            "quote_volume": 100000.0,
            "trades": 100,
            "closed": True,
        })
    repo.upsert_candles(candles)


def _inject_bullish_bos(snapshot, *, close_time_ms: int) -> None:
    """Inject a production-shape bullish BOS closed-candle event into the
    snapshot's in-memory structure_events (both the primary modules and the
    15m timeframe_modules), mirroring what price_action_engine emits on a
    real breakout. The gate reads these in-memory events via
    ``_collect_in_memory_flip_events``.
    """
    bos_event = {
        "event": "bullish_bos",
        "type": "BOS",
        "event_type": "BOS",
        "direction": "bullish",
        "timeframe": "15m",
        "reference_high": 246.0,
        "reference_low": 240.0,
        "close": 258.0,
        "close_time": int(close_time_ms),
        "closed": True,
    }
    pa_primary = (snapshot.get("modules") or {}).get("price_action") or {}
    pa_primary.setdefault("structure_events", []).append(bos_event)
    tf_modules = snapshot.get("timeframe_modules") or {}
    tf_15m = (tf_modules.get("15m") or {}).get("price_action") or {}
    tf_15m.setdefault("structure_events", []).append(bos_event)


def _seed_previous_state(repo, *, prev_side: str, prev_analysis_time: int) -> None:
    """Persist a prior ``analysis_states`` row whose state carries a
    ``trade_plan.side`` so the real ``latest_analysis_state_for_continuity``
    -> ``_compact_previous_state`` -> ``previous.side`` path surfaces the
    previous side. This is exactly the producer caller's source of
    ``previous_row`` (run_ga_workers.py:519).
    """
    state = {
        "symbol": _SYMBOL,
        "analysis_time": int(prev_analysis_time),
        "analysis_time_utc": int(prev_analysis_time),
        "analysis_mode": "shadow_test",
        "timeframes": ["15m"],
        "market_structure": {"direction_4h": "bearish" if prev_side == "SHORT" else "bullish"},
        "trend_clarity": {"score": 0.8},
        "no_trade_reason": {},
        "key_levels": {},
        "next_triggers": [],
        "next_analysis": {},
        "breakout_watch": {},
        "trade_permission": {"paper_trade_allowed": True},
        "trade_plan": {
            "side": prev_side,
            "entry_price": 250.0,
            "trigger_price": 250.0,
            "stop_loss": 240.0,
        },
        "opportunity_watch_recommended": False,
    }
    repo.save_analysis_state(state)


def _build_snapshot_with_continuity(repo, *, prev_side: str, prev_analysis_time: int):
    """Full producer->consumer setup: seed candles + previous state, build
    the real snapshot, attach a real continuity block read back from the DB.
    Returns the snapshot (caller injects/omits the BOS event before running
    the decision).
    """
    _seed_bullish_candles(repo)
    _seed_previous_state(repo, prev_side=prev_side, prev_analysis_time=prev_analysis_time)
    snapshot = build_market_state_snapshot(
        repo, symbol=_SYMBOL, analysis_time_utc=_ANALYSIS_TIME,
        mode="shadow_test", timeframes=["15m"],
    )
    previous_row = repo.latest_analysis_state_for_continuity(
        _SYMBOL, analysis_time_utc=_ANALYSIS_TIME,
    )
    assert previous_row is not None, "P1-3: seeded previous state must be readable"
    attach_analysis_continuity_to_snapshot(
        snapshot, previous_row=previous_row,
        current_batch_id=None, current_decision=None,
    )
    prev_block = (snapshot.get("analysis_continuity") or {}).get("previous") or {}
    assert str(prev_block.get("side") or "").upper() == prev_side, (
        f"P1-3: continuity previous.side must surface {prev_side} from DB; "
        f"got {prev_block.get('side')!r}"
    )
    return snapshot


class TestPgDirectionFlipClosedCandleP1_3:
    """P1-3 producer-side flip gate: the gate MUST withhold a flipped
    candidate_trade_plan unless a closed-candle breakout confirms it."""

    def test_flip_without_closed_candle_confirmation_is_withheld(self) -> None:
        """A SHORT->LONG flip with NO closed-candle breakout event is
        withheld: ``has_trade_plan`` False, ``decision`` wait_for_pullback,
        ``plan_status`` withheld, ``plan_blockers`` carries
        ``direction_flip_without_closed_candle_confirmation``, and the
        withheld LONG plan is preserved as ``candidate_trade_plan`` for
        audit (the system keeps observing, not generating a new-direction
        candidate). Defense-in-depth (risk_gate) still applies downstream.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            # Previous SHORT decision at an early time. The bullish snapshot
            # has no BOS event injected -> no closed-candle confirmation.
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="SHORT", prev_analysis_time=_LAST_CLOSE - 20 * _SPAN_15M,
            )
            decision = run_ga_sop_decision(snapshot)
            # The flip is NOT confirmed -> plan withheld.
            assert decision["has_trade_plan"] is False, (
                "P1-3: unconfirmed SHORT->LONG flip must withhold trade_plan"
            )
            assert decision["decision"] == "wait_for_pullback", (
                f"P1-3: unconfirmed flip must downgrade to wait_for_pullback "
                f"(keep observing); got {decision['decision']!r}"
            )
            assert decision["plan_status"] == "withheld", (
                f"P1-3: plan_status must be 'withheld'; got {decision['plan_status']!r}"
            )
            blockers = decision.get("plan_blockers") or []
            codes = [str(b.get("code") or "") for b in blockers]
            assert "direction_flip_without_closed_candle_confirmation" in codes, (
                f"P1-3: plan_blockers must record the flip-gate code; got {codes}"
            )
            # The withheld candidate MUST be preserved for audit / continuity.
            cand = decision.get("candidate_trade_plan")
            assert isinstance(cand, dict) and cand.get("side") == "LONG", (
                "P1-3: withheld candidate_trade_plan must be preserved with side LONG"
            )
            assert decision.get("invalidated_candidate_plan") is not None, (
                "P1-3: invalidated_candidate_plan must surface the withheld flip candidate"
            )
        finally:
            handle.close()

    def test_flip_with_closed_candle_confirmation_passes(self) -> None:
        """A SHORT->LONG flip backed by a bullish closed-candle BOS whose
        close_time is strictly after the previous decision time and not
        after the analysis time is ACCEPTED: the trade_plan stays executable,
        no flip-gate blocker is recorded.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            bos_close = _LAST_CLOSE - 3 * _SPAN_15M  # after prev_ts, before analysis
            prev_ts = bos_close - _SPAN_15M          # strictly before the BOS close
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="SHORT", prev_analysis_time=prev_ts,
            )
            _inject_bullish_bos(snapshot, close_time_ms=bos_close)
            decision = run_ga_sop_decision(snapshot)
            assert decision["has_trade_plan"] is True, (
                "P1-3: confirmed flip must keep the trade_plan"
            )
            assert decision["plan_status"] == "executable", (
                f"P1-3: confirmed flip plan_status must be executable; "
                f"got {decision['plan_status']!r}"
            )
            codes = [str(b.get("code") or "") for b in (decision.get("plan_blockers") or [])]
            assert "direction_flip_without_closed_candle_confirmation" not in codes, (
                "P1-3: confirmed flip must NOT record the flip-gate blocker"
            )
            assert (decision.get("trade_plan") or {}).get("side") == "LONG"
        finally:
            handle.close()

    def test_flip_confirmed_event_before_prev_ts_is_rejected(self) -> None:
        """Even when a bullish BOS event exists, if its close_time is NOT
        strictly after the previous decision time (it closed BEFORE the flip
        point), it does NOT confirm the flip and the plan is withheld. This
        pins the ``prev_ts < event_time`` rule (the breakout must belong to
        the period AFTER the previous decision, not a stale prior breakout).
        """
        handle = make_repo()
        try:
            repo = handle.repo
            bos_close = _LAST_CLOSE - 10 * _SPAN_15M
            prev_ts = bos_close + _SPAN_15M   # previous decision AFTER the BOS close
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="SHORT", prev_analysis_time=prev_ts,
            )
            _inject_bullish_bos(snapshot, close_time_ms=bos_close)
            decision = run_ga_sop_decision(snapshot)
            assert decision["has_trade_plan"] is False, (
                "P1-3: a BOS that closed BEFORE the previous decision does NOT confirm the flip"
            )
            assert decision["plan_status"] == "withheld"
            codes = [str(b.get("code") or "") for b in (decision.get("plan_blockers") or [])]
            assert "direction_flip_without_closed_candle_confirmation" in codes
        finally:
            handle.close()

    def test_no_flip_same_side_gate_not_active(self) -> None:
        """When the previous side equals the current side (no flip), the
        gate MUST NOT activate even though the snapshot has no breakout
        event. The plan stays executable.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="LONG", prev_analysis_time=_LAST_CLOSE - 20 * _SPAN_15M,
            )
            decision = run_ga_sop_decision(snapshot)
            assert decision["has_trade_plan"] is True, (
                "P1-3: no flip (LONG->LONG) must keep the plan"
            )
            assert decision["plan_status"] == "executable"
            codes = [str(b.get("code") or "") for b in (decision.get("plan_blockers") or [])]
            assert "direction_flip_without_closed_candle_confirmation" not in codes
        finally:
            handle.close()

    def test_no_previous_side_gate_not_active(self) -> None:
        """When there is no prior decision (first analysis), ``previous`` is
        None / has no side, so the gate MUST NOT activate. This is the
        greenfield / new-symbol case: a real LONG plan is produced.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            _seed_bullish_candles(repo)
            snapshot = build_market_state_snapshot(
                repo, symbol=_SYMBOL, analysis_time_utc=_ANALYSIS_TIME,
                mode="shadow_test", timeframes=["15m"],
            )
            attach_analysis_continuity_to_snapshot(
                snapshot, previous_row=None, current_batch_id=None, current_decision=None,
            )
            decision = run_ga_sop_decision(snapshot)
            assert decision["has_trade_plan"] is True, (
                "P1-3: first analysis (no previous side) must produce a plan"
            )
            assert decision["plan_status"] == "executable"
            codes = [str(b.get("code") or "") for b in (decision.get("plan_blockers") or [])]
            assert "direction_flip_without_closed_candle_confirmation" not in codes
        finally:
            handle.close()

    def test_flip_watch_fail_closed_note_is_preserved(self) -> None:
        """08-02 review P2-B: when the flip-gate branch WANTS a watch but the
        watch normalizer cannot build structured conditions, the fail-closed
        note MUST survive into ``risk_notes``.

        RED mechanics: the note was set inside the flip branch and then
        RE-INITIALIZED to None by a later statement in the same branch, so the
        decision row shipped without any explanation of why no auto watch was
        materialized. With the fix the note is initialized once BEFORE the
        branch and only ever set, so it is appended to ``risk_notes``.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="SHORT", prev_analysis_time=_LAST_CLOSE - 20 * _SPAN_15M,
            )
            # No BOS injected -> the SHORT->LONG flip is unconfirmed and enters
            # the flip branch; force _build_sop_watch to fail-closed (None) so
            # the note path is exercised.
            with patch(
                "plugins.crypto_guard.reasoning.ga_judge._build_sop_watch",
                return_value=None,
            ):
                decision = run_ga_sop_decision(snapshot)
            assert decision["decision"] == "wait_for_pullback"
            assert decision["has_trade_plan"] is False
            notes = [str(n) for n in (decision.get("risk_notes") or [])]
            assert any("fail-closed" in n for n in notes), (
                "P2-B: the flip-gate fail-closed watch note must be preserved "
                f"in risk_notes; got {notes}"
            )
            assert "create_opportunity_watch" not in (
                decision.get("suggested_actions") or []
            ), "P2-B: a watch-less flip must not suggest create_opportunity_watch"
        finally:
            handle.close()


class TestPgDirectionFlipNormalizeEventReworkP1_3:
    """终审返工 P1-3 (2026-07-25): ``_normalize_in_memory_event`` must be
    strictly fail-closed on the event time source and the ``closed`` flag.

    Contract (Codex final review):
      - The event time MUST come ONLY from the source event's ``close_time``
        (candle close). Fallback to ``time`` / ``event_time`` / a module
        ``analysis_time`` is FORBIDDEN - an event that only carries ``time`` or
        ``event_time`` (missing ``close_time``) MUST be rejected.
      - ``closed`` MUST be strictly ``is True`` (identity), mirroring the
        production ``price_action_engine`` shape (ga_judge.py line ~230:
        ``if closed is not True: ...``). Truthy strings ("true", "1", "yes")
        and missing ``closed`` are rejected - no invented ``True``.
      - A malformed event that slips through means the flip gate sees NO
        confirmation, so a flipped plan is WITHHELD (fail-closed).

    RED-first: these FAIL on the current code (which falls back to
    ``time``/``event_time`` and accepts truthy-string ``closed``).
    """

    def _bullish_event(self, **overrides) -> dict:
        """A production-shape bullish BOS event; callers override fields."""
        ev = {
            "event": "bullish_bos",
            "type": "BOS",
            "event_type": "BOS",
            "direction": "bullish",
            "timeframe": "15m",
            "reference_high": 246.0,
            "reference_low": 240.0,
            "close": 258.0,
            "close_time": _LAST_CLOSE - 3 * _SPAN_15M,
            "closed": True,
        }
        ev.update(overrides)
        return ev

    def test_event_time_must_come_from_close_time_only(self) -> None:
        """An event carrying ONLY ``time`` (no ``close_time``) MUST be
        rejected - the time MUST come from ``close_time`` (candle close),
        never a ``time`` fallback."""
        from plugins.crypto_guard.reasoning.ga_judge import _normalize_in_memory_event
        # Only `time` present, close_time absent -> reject.
        ev = self._bullish_event()
        ev.pop("close_time")
        ev["time"] = _LAST_CLOSE - 3 * _SPAN_15M
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: event time must come from close_time only; a `time`-only "
            "event must be rejected"
        )

    def test_event_time_must_not_fall_back_to_event_time(self) -> None:
        """An event carrying ONLY ``event_time`` (no ``close_time``) MUST be
        rejected - no fallback to ``event_time``."""
        from plugins.crypto_guard.reasoning.ga_judge import _normalize_in_memory_event
        ev = self._bullish_event()
        ev.pop("close_time")
        ev["event_time"] = _LAST_CLOSE - 3 * _SPAN_15M
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: event time must come from close_time only; an "
            "`event_time`-only event must be rejected"
        )

    def test_real_close_time_event_passes(self) -> None:
        """A real price_action event carrying ``close_time`` + ``closed=True``
        passes normalization (the positive path must keep working)."""
        from plugins.crypto_guard.reasoning.ga_judge import _normalize_in_memory_event
        ev = self._bullish_event()  # close_time + closed=True
        canon = _normalize_in_memory_event(ev, timeframe="15m")
        assert canon is not None, "P1-3: a real close_time+closed=True event must pass"
        assert canon["time"] == _LAST_CLOSE - 3 * _SPAN_15M
        assert canon["closed"] is True
        assert canon["direction"] == "bullish"

    def test_closed_must_be_strictly_true(self) -> None:
        """``closed`` MUST be strictly ``is True``. Truthy strings ("true",
        "1", "yes"), missing ``closed``, and ``closed=False`` are ALL rejected
        - no invented ``True``. Mirrors the production price_action shape."""
        from plugins.crypto_guard.reasoning.ga_judge import _normalize_in_memory_event
        # closed missing -> reject.
        ev = self._bullish_event()
        ev.pop("closed")
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: missing `closed` must be rejected"
        )
        # closed=False -> reject.
        ev = self._bullish_event(closed=False)
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: closed=False must be rejected"
        )
        # closed="true" (string) -> reject (strict is True).
        ev = self._bullish_event(closed="true")
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: closed='true' string must be rejected (strict is True)"
        )
        # closed="1" (string) -> reject (strict is True).
        ev = self._bullish_event(closed="1")
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: closed='1' string must be rejected (strict is True)"
        )
        # closed=1 (int) -> reject (strict is True, only bool True passes).
        ev = self._bullish_event(closed=1)
        assert _normalize_in_memory_event(ev, timeframe="15m") is None, (
            "P1-3: closed=1 int must be rejected (strict is True)"
        )
        # closed=True (bool) -> passes.
        ev = self._bullish_event(closed=True)
        canon = _normalize_in_memory_event(ev, timeframe="15m")
        assert canon is not None and canon["closed"] is True

    def test_malformed_event_withholds_flip_plan(self) -> None:
        """End-to-end: a SHORT->LONG flip whose only "confirmation" is a
        MALFORMED event (only ``time``, no ``close_time``; or ``closed``
        string) MUST be withheld - the gate sees no valid closed-candle
        confirmation and stays fail-closed. This proves the strict
        normalization reaches the producer gate, not just the helper."""
        handle = make_repo()
        try:
            repo = handle.repo
            prev_ts = _LAST_CLOSE - 20 * _SPAN_15M
            snapshot = _build_snapshot_with_continuity(
                repo, prev_side="SHORT", prev_analysis_time=prev_ts,
            )
            # Inject a MALFORMED bullish event: only `time` (no close_time),
            # closed as a truthy string. Under the old (loose) normalization
            # this would have been ACCEPTED and confirmed the flip; under the
            # strict fix it is rejected -> flip withheld.
            malformed = self._bullish_event()
            malformed.pop("close_time")
            malformed["time"] = _LAST_CLOSE - 3 * _SPAN_15M
            malformed["closed"] = "true"
            pa_primary = (snapshot.get("modules") or {}).get("price_action") or {}
            pa_primary.setdefault("structure_events", []).append(malformed)
            tf_modules = snapshot.get("timeframe_modules") or {}
            tf_15m = (tf_modules.get("15m") or {}).get("price_action") or {}
            tf_15m.setdefault("structure_events", []).append(malformed)
            decision = run_ga_sop_decision(snapshot)
            assert decision["has_trade_plan"] is False, (
                "P1-3: a malformed (time-only / string-closed) event must NOT "
                "confirm a flip; the plan must be withheld"
            )
            assert decision["plan_status"] == "withheld"
            codes = [str(b.get("code") or "") for b in (decision.get("plan_blockers") or [])]
            assert "direction_flip_without_closed_candle_confirmation" in codes
        finally:
            handle.close()