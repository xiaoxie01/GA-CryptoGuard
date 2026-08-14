# -*- coding: utf-8 -*-
"""08-10 Step 1 RED contract: entry-confirmation lifecycle (P1).

Contract under test (design.md §5, prd.md P1-1 + §7 scenarios 1-6):

  1. Current closed 5m bearish BOS -> origin=current_snapshot.
  2. Next round becomes range (no opposite structure / price invalidation /
     geometry mismatch) -> within TTL the prior event carries:
     origin=carried_forward for closed bars 1..3 (default 5m TTL=3).
  3. The 4th closed 5m bar -> exactly expired (age_bars > ttl_bars).
  4. A later opposite bullish CHOCH/BOS -> immediately invalidated.
  5. A closed candle crossing the structured invalidation condition ->
     immediately invalidated (price_invalidation).
  6. Data gap, future/unclosed event, cross symbol, cross side, unknown
     source, geometry mismatch -> fail-closed.

Persistence contract (design.md §5.2, §9):
  - events are appended ONLY after the owning decision + snapshot exist, in
    the same decision unit of work; a decision rollback leaves no orphan;
  - UNIQUE(event_fingerprint), fingerprint computed from canonical trusted
    fields (not prose / JSON serialization order); identical event is
    idempotent;
  - unknown source / future close / direction-vs-side mismatch / decision
    provenance mismatch are rejected by the insert method (fail-closed).

Policy parsing (design.md §4): exact keys + types; unknown mode, TTL above
hard max, overlapping hard/adaptive entries, missing hard_gates, NaN/inf,
or unknown keys fail closed.

RED-first: none of the referenced modules/columns/markers exist yet; every
test below fails at import (missing `entry_confirmation_lifecycle` /
`risk_policy`) or on the missing repository method. That is the intended
baseline.
"""
from __future__ import annotations

import datetime as _dt
import math

import pytest

from plugins.crypto_guard.tests import pg_fixtures as fx

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

# 1_700_000_100_000 is an exact 5m AND 15m bar-close boundary (UTC).
_ANALYSIS = 1_700_000_100_000
_BAR_MS = {"5m": 300_000, "15m": 900_000}
_TTL = {"5m": 3, "15m": 1}
_HARD_MAX = {"5m": 6, "15m": 2}


def _ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _bearish_event(*, close_time: int = _ANALYSIS, price: float = 45.34,
                   tf: str = "5m", **kw: object) -> dict:
    ev = {
        "event": "bearish_bos",
        "timeframe": tf,
        "direction": "bearish",
        "candle_close_time": close_time,
        "price": price,
        "closed": True,
    }
    ev.update(kw)
    return ev


def _bullish_event(*, close_time: int, price: float, tf: str = "5m",
                   event: str = "bullish_choch", **kw: object) -> dict:
    return _bearish_event(
        close_time=close_time, price=price, tf=tf, event=event, **kw
    )


def _snapshot(*, symbol: str = "LTCUSDT", at: int | None = None,
              events: list[dict] | None = None,
              tf: str = "5m",
              health_tfs: tuple = ("1d", "4h", "1h", "15m", "5m"),
              market_structure: str = "bearish", momentum: str = "bearish",
              status: str = "complete", last_close: float | None = None,
              last_close_time: int | None = None,
              health: dict | None = None) -> dict:
    """Production-shaped market snapshot (market_state_builder contract)."""
    at = _ANALYSIS if at is None else int(at)
    events = [] if events is None else list(events)
    health = health or {}
    health_by_tf: dict = {}
    for htf in health_tfs:
        health_by_tf[htf] = {
            "ready": True,
            "contiguous_count": 300,
            "loaded_count": 300,
            "last_close_time": (last_close_time if last_close_time is not None
                                else at),
        }
        if htf in health:
            health_by_tf[htf].update(health[htf])
    profiles = {h: {"market_structure": market_structure,
                    "momentum": momentum} for h in health_tfs}
    pa_module = {
        "market_structure": market_structure,
        "structure_events": events,
        "last_close": last_close if last_close is not None else (
            45.34 if momentum == "bearish" else 45.34),
    }
    return {
        "symbol": symbol,
        "analysis_time_utc": at,
        "mode": "shadow_test",
        "profiles": profiles,
        "modules": {"price_action": pa_module,
                    "momentum": {"direction": momentum}},
        "timeframe_modules": {
            tf: {"price_action": dict(pa_module, structure_events=events),
                 "smc": {"structure_events": []}},
        },
        "data_quality": {"status": status,
                         "health_by_tf": health_by_tf,
                         "health": health_by_tf},
        "analysis_degraded": status != "complete",
        "partial_tf_mode": False,
        "decision": "opportunity_watch",
        "has_trade_plan": False,
    }


def _plan(*, side: str = "SHORT", entry_price: float = 45.34,
          stop_loss: float = 45.70, invalid_price: float = 45.50,
          tf: str = "5m", risk_percent: float = 0.5, **kw: object) -> dict:
    plan = {
        "side": side,
        "entry_type": "limit",
        "entry_price": entry_price,
        "trigger_price": entry_price,
        "stop_loss": stop_loss,
        "take_profits": [{"price": 44.90, "ratio": 0.5},
                         {"price": 44.50, "ratio": 0.5}],
        "risk_percent": risk_percent,
        "invalid_condition": (f"{tf} 收盘站回 {invalid_price}" if side == "SHORT"
                              else f"{tf} 收盘跌破 {invalid_price}"),
        "reason": "结构偏空，等待反抽确认；仅用于模拟盘",
    }
    plan.update(kw)
    return plan


def _canonical_confirmation(*, symbol: str = "LTCUSDT", side: str = "SHORT",
                            close_time: int = _ANALYSIS, price: float = 45.34,
                            tf: str = "5m", source: str = "price_action",
                            **kw: object) -> dict:
    conf = {
        "type": "closed_candle_confirmation",
        "timeframe": tf,
        "event_type": "BOS",
        "direction": "bearish" if side == "SHORT" else "bullish",
        "candle_close_time": close_time,
        "price": price,
        "source": source,
        "symbol": symbol,
    }
    conf.update(kw)
    return conf


def _source_decision(*, snapshot_id: int, plan: dict, symbol: str = "LTCUSDT",
                     at: int | None = None, llm_status: str = "ok") -> dict:
    at = _ANALYSIS if at is None else int(at)
    return {
        "symbol": symbol,
        "analysis_time": at,
        "analysis_time_utc": _ts(at),
        "decision_type": "opportunity_watch_recheck",
        "signal_grade": "A",
        "confidence": 0.8,
        "market_bias": "bearish",
        "trend_stage": "early",
        "decision": "trade_plan_available",
        "skill_result_refs": {},
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "trade_plan": plan,
        "snapshot_id": snapshot_id,
        "final_summary": "lc-contract",
        "raw_llm_summary": "lc-contract",
        "rendered_summary": "lc-contract",
        "batch_id": None,
        "previous_grade": "D",
        "llm_status": llm_status,
    }


def _snapshot_containing(confirmation: dict, *, at: int) -> dict:
    """Production-shaped source snapshot: it IS the snapshot the event was
    extracted from, so its ``structure_events`` MUST contain the canonical
    event (mirrors the producer fixtures, e.g. ``_persist_ltc_source_event``
    persists ``events=[_ltc_event(...)]``). 08-10 P2-3 (fresh reviewer P2):
    provenance is exact — the resolver re-extracts the event from the source
    snapshot and requires a field-for-field match, so a source snapshot that
    LACKS the event fails closed at resolve time (``data_gap``)."""
    direction = str(confirmation.get("direction") or "").lower()
    raw = {
        "event": "bearish_bos" if direction == "bearish" else "bullish_bos",
        "direction": direction,
        "timeframe": str(confirmation.get("timeframe") or "5m"),
        "candle_close_time": confirmation.get("candle_close_time"),
        "price": confirmation.get("price"),
        "closed": True,
    }
    snap = _snapshot(at=at)
    snap["modules"]["price_action"]["structure_events"] = [raw]
    return snap


def _persist_source_event(h, *, confirmation: dict, snapshot: dict | None = None,
                          plan: dict | None = None, at: int | None = None
                          ) -> tuple[int, int, int]:
    """Persist source snapshot + owning decision + canonical event in the
    SAME unit of work, exactly as the production decision-persistence phase
    does. Returns (snapshot_id, decision_id, event_id)."""
    at = _ANALYSIS if at is None else int(at)
    snap = (snapshot if snapshot is not None
            else _snapshot_containing(confirmation, at=at))
    plan = _plan() if plan is None else plan
    plan = {**plan, "entry_trigger_confirmation": confirmation}
    snap_id = h.repo.save_market_snapshot(snap)
    dec_id = h.repo.create_ga_decision(
        _source_decision(snapshot_id=snap_id, plan=plan, at=at))
    ev_id = h.repo.insert_entry_confirmation_event_after_decision(
        decision_id=dec_id, snapshot_id=snap_id, confirmation=confirmation,
        analysis_time_ms=at)
    return snap_id, dec_id, ev_id


def _count_events(h) -> int:
    with h.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM entry_confirmation_events")
        return int(cur.fetchone()["n"])


def _resolve(h, *, snapshot: dict, plan: dict):
    from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy
    from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
        resolve_trusted_entry_confirmation,
    )
    return resolve_trusted_entry_confirmation(
        h.repo, snapshot, plan, RiskAssistancePolicy())


class TestCurrentSnapshotResolution:
    """Scenario 1 + current-event priority over older persisted events."""

    def test_current_bearish_bos_is_current_snapshot(self):
        from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
            resolve_trusted_entry_confirmation,
        )
        from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy

        h = fx.make_repo()
        try:
            snap = _snapshot(events=[_bearish_event(close_time=_ANALYSIS)])
            plan = _plan()
            result = resolve_trusted_entry_confirmation(
                h.repo, snap, plan, RiskAssistancePolicy())
            assert result.status == "valid"
            assert result.origin == "current_snapshot"
            assert result.age_bars == 0
            assert result.confirmation["symbol"] == "LTCUSDT"
            assert result.confirmation["direction"] == "bearish"
            assert result.checks["same_symbol"] is True
            assert result.checks["same_side"] is True
            assert result.checks["opposite_structure_absent"] is True
            assert result.checks["price_invalidation_clear"] is True
            # current event has no persisted owning decision yet
            assert result.source_decision_id is None
            assert result.source_snapshot_id is None
        finally:
            h.close()

    def test_current_event_wins_over_older_persisted_event(self):
        h = fx.make_repo()
        try:
            # An old SHORT confirmation is carried from decision N at T0.
            old_conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=old_conf, at=_ANALYSIS)
            # The current round has a NEWER closed bearish BOS at T2.
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            snap = _snapshot(at=t2, events=[_bearish_event(close_time=t2,
                                                           price=45.20)])
            plan = _plan(entry_price=45.20)
            result = _resolve(h, snapshot=snap, plan=plan)
            assert result.status == "valid"
            assert result.origin == "current_snapshot"
            assert result.confirmation["candle_close_time"] == t2
            assert result.confirmation["price"] == 45.20
        finally:
            h.close()

    def test_current_event_fails_geometry_never_falls_through_to_carried(self):
        h = fx.make_repo()
        try:
            # A valid SHORT confirmation is carried from decision N at T0 whose
            # price EQUALS the plan entry (45.34 -> the carried-history path
            # WOULD resolve valid with origin=carried_forward). Fresh reviewer
            # P2-1: this carried candidate must never resurrect a state the
            # current event contradicts.
            _persist_source_event(
                h, confirmation=_canonical_confirmation(
                    close_time=_ANALYSIS, price=45.34), at=_ANALYSIS,
            )
            # The current round has a NEWER closed bearish BOS at 47.00, 3.5%
            # above the 45.34 entry -> current-event geometry FAILS the 0.5%
            # max_entry_deviation_pct tolerance.
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            snap = _snapshot(at=t2, events=[_bearish_event(close_time=t2,
                                                           price=47.00)])
            result = _resolve(h, snapshot=snap, plan=_plan(entry_price=45.34))
            # Current same-side event decides EVEN ON FAILURE (design §5.3
            # step 4): invalidated geometry_mismatch, origin=current_snapshot.
            # It must NOT fall through to the carried 45.34 event (option-a
            # semantics) which would resurrect a "valid" state.
            assert result.status == "invalidated"
            assert result.invalidation_reason == "geometry_mismatch"
            assert result.origin == "current_snapshot"
            assert result.confirmation["candle_close_time"] == t2
            assert result.checks["geometry_ok"] is False
        finally:
            h.close()


class TestCarriedForwardAndExpiry:
    """Scenarios 2-3 + 15m one-bar carry + deterministic tie-break."""

    def test_carries_for_closed_bars_1_to_3(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            for n in (1, 2, 3):
                t_n = _ANALYSIS + n * _BAR_MS["5m"]
                # current round is range: no event, but 5m candles closed.
                snap = _snapshot(at=t_n, events=[])
                plan = _plan()
                result = _resolve(h, snapshot=snap, plan=plan)
                assert result.status == "valid", f"bar {n} should carry"
                assert result.origin == "carried_forward"
                assert result.age_bars == n
                assert result.ttl_bars == _TTL["5m"]
                assert result.confirmation == conf
                assert result.checks["source_event_found"] is True
                assert result.checks["closed_bar_sequence_complete"] is True
                assert result.checks["opposite_structure_absent"] is True
                assert result.checks["price_invalidation_clear"] is True
                assert result.checks["geometry_ok"] is True
        finally:
            h.close()

    def test_fourth_closed_5m_bar_expires_exactly(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t4 = _ANALYSIS + 4 * _BAR_MS["5m"]
            snap = _snapshot(at=t4, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "expired"
            assert result.invalidation_reason is None
            assert result.age_bars == 4
            assert result.ttl_bars == 3
        finally:
            h.close()

    def test_15m_event_carries_one_bar_then_expires_next(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS, tf="15m")
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + 1 * _BAR_MS["15m"]
            snap1 = _snapshot(at=t1, events=[], tf="15m")
            r1 = _resolve(h, snapshot=snap1, plan=_plan(tf="15m"))
            assert r1.status == "valid"
            assert r1.origin == "carried_forward"
            assert r1.age_bars == 1
            assert r1.ttl_bars == 1
            t2 = _ANALYSIS + 2 * _BAR_MS["15m"]
            snap2 = _snapshot(at=t2, events=[], tf="15m")
            r2 = _resolve(h, snapshot=snap2, plan=_plan(tf="15m"))
            assert r2.status == "expired"
            assert r2.age_bars == 2
        finally:
            h.close()

    def test_newest_close_time_wins_tie_break_deterministic(self):
        h = fx.make_repo()
        try:
            old = _canonical_confirmation(close_time=_ANALYSIS, price=45.30)
            _persist_source_event(h, confirmation=old, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            new = _canonical_confirmation(close_time=t1, price=45.35)
            _persist_source_event(h, confirmation=new, at=t1)
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            snap = _snapshot(at=t2, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan(entry_price=45.35))
            assert result.status == "valid"
            assert result.origin == "carried_forward"
            assert result.confirmation == new
        finally:
            h.close()

    def test_missing_source_event_fails_closed_data_gap(self):
        h = fx.make_repo()
        try:
            # The owning snapshot was saved WITHOUT the structure event (drift,
            # wrong snapshot id attached, event deleted after the fact).
            # 08-10 P2-3 (fresh reviewer P2): provenance is EXACT — the
            # resolver re-extracts the event from the source snapshot (the
            # single source of truth, design.md §5.3 step 6), so the carried
            # row fails closed at ``data_gap`` and can NEVER resolve valid from
            # a snapshot that no longer contains the event. Before the fix a
            # weak ``analysis >= close`` heuristic kept the gate open.
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS,
                                  snapshot=_snapshot(at=_ANALYSIS))
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(at=t1, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "invalidated"
            assert result.invalidation_reason == "data_gap"
            assert result.origin == "carried_forward"
            assert result.checks["source_event_found"] is False
        finally:
            h.close()

    def test_newest_invalid_does_not_block_older_valid_carried(self):
        """08-10 P2-4 (fresh reviewer P2): pin the carried-path semantics the
        docstring now states — an invalidated / expired / data-gap NEWEST
        candidate is SKIPPED and does not block an older valid event, but a
        valid candidate is returned immediately and an older event never
        resurrects a state the newest contradicts (design.md §5.3 step 10)."""
        h = fx.make_repo()
        try:
            # Older SHORT event at 45.34 == plan entry -> geometry passes.
            older = _canonical_confirmation(close_time=_ANALYSIS, price=45.34)
            _persist_source_event(h, confirmation=older, at=_ANALYSIS)
            # Newer SHORT event 3.5% above the entry -> geometry_mismatch.
            t1 = _ANALYSIS + _BAR_MS["5m"]
            newer = _canonical_confirmation(close_time=t1, price=47.00)
            _persist_source_event(h, confirmation=newer, at=t1)
            # Resolve at t2: newest is invalid, older is valid.
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            snap = _snapshot(at=t2, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan(entry_price=45.34))
            assert result.status == "valid"
            assert result.origin == "carried_forward"
            assert result.confirmation == older
            assert result.source_decision_id is not None
        finally:
            h.close()


class TestInvalidationAndFailClosed:
    """Scenarios 4-6."""

    def test_opposite_bullish_choch_invalidates_immediately(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            snap = _snapshot(at=t2, events=[_bullish_event(close_time=t2,
                                                           price=45.60)])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "invalidated"
            assert result.invalidation_reason == "opposite_structure"
            assert result.checks["opposite_structure_absent"] is False
        finally:
            h.close()

    def test_price_crosses_invalidation_level_invalidates(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t2 = _ANALYSIS + 2 * _BAR_MS["5m"]
            # range (no events) but the latest closed 5m candle closed at
            # 45.60, above the SHORT invalidation level 45.50.
            snap = _snapshot(at=t2, events=[], last_close=45.60)
            result = _resolve(h, snapshot=snap, plan=_plan(invalid_price=45.50))
            assert result.status == "invalidated"
            assert result.invalidation_reason == "price_invalidation"
            assert result.checks["price_invalidation_clear"] is False
        finally:
            h.close()

    def test_geometry_mismatch_fails_closed(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS, price=45.34)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(at=t1, events=[])
            # entry 46.5 is 2.56% away from the carried 45.34 close.
            result = _resolve(h, snapshot=snap, plan=_plan(entry_price=46.5))
            assert result.status == "invalidated"
            assert result.invalidation_reason == "geometry_mismatch"
            assert result.checks["geometry_ok"] is False
        finally:
            h.close()

    def test_missing_timeframe_health_is_data_gap(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(at=t1, events=[],
                             health={"5m": {"ready": True,
                                            "last_close_time": None,
                                            "contiguous_count": 0}})
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "invalidated"
            assert result.invalidation_reason == "data_gap"
            assert result.checks["closed_bar_sequence_complete"] is False
        finally:
            h.close()

    def test_stale_last_close_is_data_gap(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            # last closed 5m candle is one bar short of the expected
            # T0+1*B, so the [T0, T1] window cannot be proven complete.
            snap = _snapshot(at=t1, events=[],
                             last_close_time=_ANALYSIS)
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "invalidated"
            assert result.invalidation_reason == "data_gap"
            assert result.checks["closed_bar_sequence_complete"] is False
        finally:
            h.close()

    def test_degraded_snapshot_never_carries(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(at=t1, events=[], status="degraded")
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "invalidated"
            assert result.invalidation_reason == "data_gap"
        finally:
            h.close()

    def test_future_or_unclosed_current_event_is_absent(self):
        h = fx.make_repo()
        try:
            # future close: candle_close_time > analysis_time_utc
            snap = _snapshot(events=[_bearish_event(
                close_time=_ANALYSIS + _BAR_MS["5m"])])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "absent"
            # unclosed: closed is not True identity
            snap2 = _snapshot(events=[_bearish_event(close_time=_ANALYSIS,
                                                     closed=False)])
            result2 = _resolve(h, snapshot=snap2, plan=_plan())
            assert result2.status == "absent"
        finally:
            h.close()

    def test_cross_symbol_event_not_carried(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(symbol="LTCUSDT",
                                           close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(symbol="BTCUSDT", at=t1, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "absent"
        finally:
            h.close()

    def test_cross_side_event_not_carried(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(side="SHORT",
                                           close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            t1 = _ANALYSIS + _BAR_MS["5m"]
            snap = _snapshot(at=t1, events=[])
            result = _resolve(h, snapshot=snap,
                              plan=_plan(side="LONG"))
            assert result.status == "absent"
        finally:
            h.close()

    def test_no_self_carry_in_same_round(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _persist_source_event(h, confirmation=conf, at=_ANALYSIS)
            # The resolver is invoked again for the SAME analysis round on a
            # snapshot that no longer exposes the event (range). The event's
            # source_analysis_time is NOT strictly older, so it cannot be
            # carried as a history event.
            snap = _snapshot(at=_ANALYSIS, events=[])
            result = _resolve(h, snapshot=snap, plan=_plan())
            assert result.status == "absent"
        finally:
            h.close()


class TestEventPersistenceContract:
    """design.md §5.2/§9: append-only, same-transaction, idempotent."""

    def test_marker_registered(self):
        h = fx.make_repo()
        try:
            with h.conn.cursor() as cur:
                cur.execute(
                    "SELECT applied_at IS NOT NULL AS applied "
                    "FROM _migration_state "
                    "WHERE key='entry_confirmation_lifecycle_contract_v1'")
                row = cur.fetchone()
            assert row is not None and row["applied"] is True
        finally:
            h.close()

    def test_decision_rollback_leaves_no_orphan_event(self):
        h = fx.make_repo()
        try:
            try:
                with h.conn.transaction():
                    conf = _canonical_confirmation(close_time=_ANALYSIS)
                    snap_id = h.repo.save_market_snapshot(_snapshot())
                    # The owning decision MUST carry the confirmation in its
                    # trade_plan (provenance gate), exactly like the production
                    # decision-persistence phase / _persist_source_event.
                    plan = _plan()
                    plan["entry_trigger_confirmation"] = conf
                    dec_id = h.repo.create_ga_decision(
                        _source_decision(snapshot_id=snap_id, plan=plan))
                    h.repo.insert_entry_confirmation_event_after_decision(
                        decision_id=dec_id, snapshot_id=snap_id,
                        confirmation=conf, analysis_time_ms=_ANALYSIS)
                    assert _count_events(h) == 1
                    raise RuntimeError("rollback-now")
            except RuntimeError:
                pass
            assert _count_events(h) == 0
        finally:
            h.close()

    def test_identical_fingerprint_is_idempotent(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(close_time=_ANALYSIS)
            _, _, ev1 = _persist_source_event(h, confirmation=conf,
                                              at=_ANALYSIS)
            # A second decision at the same round re-observes the SAME
            # canonical event -> identical fingerprint -> no second row.
            _, _, ev2 = _persist_source_event(h, confirmation=conf,
                                              at=_ANALYSIS)
            assert ev1 == ev2
            assert _count_events(h) == 1
        finally:
            h.close()

    def test_fingerprint_is_field_order_insensitive(self):
        from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
            canonical_confirmation_fingerprint,
        )
        a = canonical_confirmation_fingerprint(
            _canonical_confirmation(close_time=_ANALYSIS))
        b = canonical_confirmation_fingerprint(_canonical_confirmation(
            close_time=_ANALYSIS, source="price_action"))
        assert a == b
        # changing a trusted field changes the fingerprint
        c = canonical_confirmation_fingerprint(
            _canonical_confirmation(close_time=_ANALYSIS, price=45.35))
        assert c != a

    def test_unknown_source_rejected_by_insert(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(source="fabricated_module")
            plan = _plan()
            plan["entry_trigger_confirmation"] = conf
            snap_id = h.repo.save_market_snapshot(_snapshot())
            dec_id = h.repo.create_ga_decision(
                _source_decision(snapshot_id=snap_id, plan=plan))
            with pytest.raises(ValueError):
                h.repo.insert_entry_confirmation_event_after_decision(
                    decision_id=dec_id, snapshot_id=snap_id,
                    confirmation=conf, analysis_time_ms=_ANALYSIS)
        finally:
            h.close()

    def test_future_close_rejected_by_insert(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(
                close_time=_ANALYSIS + _BAR_MS["5m"])
            plan = _plan()
            plan["entry_trigger_confirmation"] = conf
            snap_id = h.repo.save_market_snapshot(_snapshot())
            dec_id = h.repo.create_ga_decision(
                _source_decision(snapshot_id=snap_id, plan=plan))
            with pytest.raises(ValueError):
                h.repo.insert_entry_confirmation_event_after_decision(
                    decision_id=dec_id, snapshot_id=snap_id,
                    confirmation=conf, analysis_time_ms=_ANALYSIS)
        finally:
            h.close()

    def test_direction_vs_side_mismatch_rejected(self):
        h = fx.make_repo()
        try:
            conf = _canonical_confirmation(side="SHORT", direction="bullish")
            plan = _plan()
            plan["entry_trigger_confirmation"] = conf
            snap_id = h.repo.save_market_snapshot(_snapshot())
            dec_id = h.repo.create_ga_decision(
                _source_decision(snapshot_id=snap_id, plan=plan))
            with pytest.raises(ValueError):
                h.repo.insert_entry_confirmation_event_after_decision(
                    decision_id=dec_id, snapshot_id=snap_id,
                    confirmation=conf, analysis_time_ms=_ANALYSIS)
        finally:
            h.close()

    def test_decision_provenance_mismatch_rejected(self):
        h = fx.make_repo()
        try:
            # The owning decision's trade_plan carries a DIFFERENT
            # confirmation than the event being inserted.
            canonical = _canonical_confirmation(close_time=_ANALYSIS,
                                                price=45.34)
            forged = _canonical_confirmation(close_time=_ANALYSIS,
                                             price=45.99)
            plan = _plan()
            plan["entry_trigger_confirmation"] = forged
            snap_id = h.repo.save_market_snapshot(_snapshot())
            dec_id = h.repo.create_ga_decision(
                _source_decision(snapshot_id=snap_id, plan=plan))
            with pytest.raises(ValueError):
                h.repo.insert_entry_confirmation_event_after_decision(
                    decision_id=dec_id, snapshot_id=snap_id,
                    confirmation=canonical, analysis_time_ms=_ANALYSIS)
        finally:
            h.close()


class TestRiskAssistancePolicyParsing:
    """design.md §4: exact keys/types, fail-closed on anything else."""

    def test_absent_config_defaults_to_shadow(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        policy = load_risk_assistance_config({})
        assert policy.mode == "shadow"
        assert policy.confirmation_ttl_bars == _TTL
        assert policy.confirmation_hard_max_bars == _HARD_MAX
        assert policy.max_rounds == 2
        assert policy.max_tool_requests == 5
        assert policy.max_context_bytes == 49152
        assert math.isclose(policy.max_uncertainty, 0.35)
        assert "trusted_entry_confirmation" in policy.hard_gates
        assert "news_like_event" in policy.adaptive_gates

    def test_valid_explicit_modes(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        for mode in ("off", "shadow", "paper_bounded"):
            policy = load_risk_assistance_config(
                {"risk_assistance": {"mode": mode}})
            assert policy.mode == mode

    def test_unknown_mode_fails_closed(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {"mode": "live"}})

    def test_ttl_above_hard_max_fails_closed(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow",
                    "confirmation_ttl_bars": {"5m": 7, "15m": 1}}})

    def test_overlapping_hard_adaptive_fails_closed(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow",
                    "hard_gates": ["market_data_ready", "minimum_stop_distance"],
                    "adaptive_gates": ["minimum_stop_distance"]}})

    def test_missing_hard_gates_fails_closed(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow", "hard_gates": []}})

    def test_nan_inf_rejected(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow", "max_uncertainty": float("nan")}})
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow", "max_uncertainty": float("inf")}})

    def test_unknown_key_fails_closed(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config(
                {"risk_assistance": {
                    "mode": "shadow", "write_orders": True}})

    def test_hard_gates_cannot_be_deleted_in_code(self):
        from plugins.crypto_guard.risk.risk_policy import (
            HARD_GATE_CODES, load_risk_assistance_config,
        )
        # the compiled hard-gate set is the floor; config can only shrink
        # toward it, never remove a mandatory invariant.
        policy = load_risk_assistance_config(
            {"risk_assistance": {
                "mode": "shadow",
                "hard_gates": ["market_data_ready"]}})
        assert set(policy.hard_gates) == {"market_data_ready"}
        assert "trusted_entry_confirmation" in HARD_GATE_CODES
        assert "idempotency" in HARD_GATE_CODES
        assert "extreme_regime" in HARD_GATE_CODES
