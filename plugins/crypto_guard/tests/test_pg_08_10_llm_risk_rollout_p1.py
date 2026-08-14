# -*- coding: utf-8 -*-
"""08-10 Step 1 RED contract: risk-assistance rollout modes + final gate (P1).

Contract under test (design.md §7/§8/§11, prd.md P1-6 + §7 scenarios 9-13):

  - The worker creates an order ONLY through the design §7 final conjunction:

        proposal verified
        AND final risk_check.ok is True
        AND plan_execution_state == confirmed
        AND account gate open
        AND market regime gate open
        AND once-ever watch/order gate open
        AND mode == paper_bounded

  - ``risk_advisory_order_allowed(...)`` (run_ga_workers.py, NEW) is the pure
    gate that encodes this conjunction. ``off`` = risk-advisory path absent
    (existing gate decides, behavior byte-for-byte unchanged); ``shadow`` =
    hypothetical only (recorded, never orders); ``paper_bounded`` = only a
    verified proposal may supply the adjusted plan; unknown mode fails closed.
  - The decision carries a system-only ``risk_advisory`` envelope (mode,
    proposal_status, verification_ok, final_risk_check_ok) stamped AFTER LLM
    schema validation -- the LLM can never author it. ``handle_opportunity_watch_recheck``
    must refuse an order when the envelope says shadow / failed / unverified.
  - Provider/tool/schema failure produces no order and a prior proposal is never
    reused (proposal_status=failed -> rejected).
  - Once-ever watch->order bridge is preserved: duplicate/concurrent recheck ->
    same order ID, no duplicate alert; terminal order still holds the link.
  - Observation triggers remain silent (internal-only, no alert_outbox, no push);
    ONLY a VERIFIED paper_bounded recheck that CREATES an order announces that
    ORDER with an order-creation notification (PRD P2-1 / reviewer P2-1).
  - LTC 4985->4997 historical fixture: a 5m bearish BOS at T0 carries into the
    next eligible bar (valid / carried_forward), but the same SHORT plan stays
    NON-executable while the independent blockers (news_like_event, stop distance
    0.791% < 0.8%, ATR buffer) remain unresolved. 确认 != 下单.

RED-first: ``run_ga_workers.risk_advisory_order_allowed``,
``reasoning.entry_confirmation_lifecycle``, ``risk.risk_policy`` and
``risk.risk_adjustment_verifier`` do not exist yet; imports fail and the
handler ignores the ``risk_advisory`` envelope (creates orders it must refuse).
That is the intended baseline.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from plugins.crypto_guard.tests import pg_fixtures as fx
from plugins.crypto_guard.tests.test_pg_08_04_watch_order_bridge_b import (
    _materialize_breakout_watch,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_T0 = 1_700_000_100_000  # exact 5m bar-close boundary (decision 4985)
_BAR_MS = {"5m": 300_000, "15m": 900_000}
_T1 = _T0 + _BAR_MS["5m"]  # next closed 5m bar (decision 4997 window)


# ── helpers ────────────────────────────────────────────────────────────────


def _ltc_event(*, close_time: int = _T0, price: float = 45.34) -> dict:
    """The trusted closed 5m bearish BOS from decision 4985 (production
    evidence: price 45.34, close time equals the decision analysis time)."""
    return {"event": "bearish_bos", "timeframe": "5m", "direction": "bearish",
            "candle_close_time": close_time, "price": price, "closed": True}


def _ltc_confirmation(*, close_time: int = _T0, price: float = 45.34) -> dict:
    """Canonical trusted confirmation derived from ``_ltc_event``."""
    return {"type": "closed_candle_confirmation", "timeframe": "5m",
            "event_type": "BOS", "direction": "bearish",
            "candle_close_time": close_time, "price": price,
            "source": "price_action", "symbol": "LTCUSDT"}


def _ltc_snapshot(*, at: int = _T1, events: list[dict] | None = None,
                  regime: str = "news_like_event", atr: float = 2.0) -> dict:
    """LTCUSDT SHORT snapshot. Default regime is ``news_like_event`` (an
    ADAPTIVE blocker per prd.md P0-2 -- never a hard veto) and ATR current 2.0
    (so 0.2*ATR=0.4 exceeds the 0.36 LTC stop distance -> ATR buffer fails).
    Bearish structure/momentum, complete data, normal closed-bar sequence."""
    events = [] if events is None else list(events)
    health = {tf: {"ready": True, "last_close_time": at}
              for tf in ("1d", "4h", "1h", "15m", "5m")}
    profiles = {tf: {"market_structure": "bearish", "momentum": "bearish"}
                for tf in ("1d", "4h", "1h", "15m")}
    pa = {"market_structure": "bearish", "structure_events": events,
          "last_close": 45.34}
    return {
        "symbol": "LTCUSDT",
        "analysis_time_utc": at,
        "mode": "shadow_test",
        "profiles": profiles,
        "modules": {
            "price_action": pa,
            "momentum": {"direction": "bearish", "atr": {"current": atr}},
            "market_regime": {"regime": regime, "extreme": False},
        },
        "timeframe_modules": {"5m": {"price_action": dict(pa),
                                     "smc": {"structure_events": []}}},
        "data_quality": {"status": "complete", "health_by_tf": health,
                         "health": health},
        "analysis_degraded": False,
        "partial_tf_mode": False,
    }


def _ltc_plan(*, entry: float = 45.51, stop: float = 45.87,
              invalid: float = 45.80, close_time: int = _T0) -> dict:
    """Decision 4985/4997 SHORT geometry. Stop distance (45.87-45.51)/45.51 =
    0.791% is below the 0.8% minimum; TPs give RR 3.08 so the stop/ATR/news
    blockers are the ONLY reasons this plan is not executable."""
    return {
        "side": "SHORT",
        "entry_type": "limit",
        "entry_price": entry,
        "trigger_price": entry,
        "stop_loss": stop,
        "take_profits": [{"price": 44.40, "ratio": 0.5},
                         {"price": 43.90, "ratio": 0.5}],
        "risk_percent": 0.5,
        "invalid_condition": f"5m 收盘站回 {invalid}",
        "reason": "结构偏空，等待反抽确认；仅用于模拟盘（LTC 4985→4997 fixture）",
        "entry_trigger_confirmation": _ltc_confirmation(close_time=close_time),
    }


def _persist_ltc_source_event(h, *, at: int = _T0) -> tuple[int, int, int]:
    """Persist the 4985 source snapshot + owning decision + canonical event in
    ONE unit of work (production decision-persistence contract)."""
    snap = _ltc_snapshot(at=at, events=[_ltc_event(close_time=at)], atr=2.0)
    snap_id = h.repo.save_market_snapshot(snap)
    plan = {**_ltc_plan(close_time=at),
            "entry_trigger_confirmation": _ltc_confirmation(close_time=at)}
    dec = {
        "symbol": "LTCUSDT", "analysis_time": at, "analysis_time_utc": at,
        "decision_type": "opportunity_watch_recheck", "signal_grade": "A",
        "confidence": 0.8, "market_bias": "bearish", "trend_stage": "early",
        "decision": "trade_plan_available", "skill_result_refs": {},
        "evidence": [], "counter_evidence": [], "risk_check": {"ok": True},
        "feishu_actions": [], "trade_plan": plan, "snapshot_id": snap_id,
        "final_summary": "ltc-4985", "raw_llm_summary": "ltc-4985",
        "rendered_summary": "ltc-4985", "batch_id": None,
        "previous_grade": "D", "llm_status": "ok",
    }
    dec_id = h.repo.create_ga_decision(dec)
    ev_id = h.repo.insert_entry_confirmation_event_after_decision(
        decision_id=dec_id, snapshot_id=snap_id,
        confirmation=_ltc_confirmation(close_time=at), analysis_time_ms=at)
    return snap_id, dec_id, ev_id


def _risk_advisory(*, mode: str, proposal_status: str = "ok",
                   verification_ok: bool = True,
                   final_risk_check_ok: bool = True,
                   candidate_plan: dict | None = None,
                   lifecycle: dict | None = None) -> dict:
    """System-only envelope stamped AFTER LLM schema validation; the LLM can
    never author it. Drives the design §7 final gate in the recheck handler.

    08-10 P2-1 (reviewer finding): a VERIFIED paper_bounded envelope that rides
    ``candidate_plan`` (ORIGINAL pre-adjustment geometry) + the flat
    ``entry_confirmation_lifecycle`` lets the production order notification
    render original-vs-adjusted geometry through ``build_order_notification``.
    """
    envelope = {"mode": mode, "proposal_status": proposal_status,
                "verification_ok": verification_ok,
                "final_risk_check_ok": final_risk_check_ok}
    if candidate_plan is not None:
        envelope["candidate_plan"] = candidate_plan
    if lifecycle is not None:
        envelope["entry_confirmation_lifecycle"] = lifecycle
    return envelope


def _gate_clearing_decision(symbol: str = "BTCUSDT", *,
                            risk_advisory: dict | None = None,
                            trade_plan: dict | None = None) -> dict:
    """A decision that clears the EXISTING ``_recheck_order_gate`` (confirmed /
    S / llm ok / llm_confirmed / risk_check.ok / valid LONG plan matching the
    LONG breakout watch / account open). ``risk_advisory`` is the NEW field
    whose gate Step 8 must enforce."""
    decision = {
        "symbol": symbol,
        "signal_id": None,
        "ga_decision_id": 9_999,
        "plan_execution_state": "confirmed",
        "plan_origin": "llm_confirmed",
        "llm_status": "ok",
        "effective_signal_grade": "A",
        "signal_grade": "A",
        "risk_check": {"ok": True},
        "trade_plan": trade_plan or {
            "side": "LONG", "entry_type": "limit", "entry_price": 99.5,
            "trigger_price": 99.5, "stop_loss": 94.0,
            "take_profits": [{"price": 107.5, "ratio": 0.5},
                             {"price": 112.5, "ratio": 0.5}],
            "risk_percent": 0.4,
            "invalid_condition": "15m 收盘跌破 95.0",
            "reason": "结构偏多，等待回踩确认；仅用于模拟盘（调整后计划）",
        },
    }
    if risk_advisory is not None:
        decision["risk_advisory"] = risk_advisory
    return decision


def _make_analyze(captured: dict, decision: dict) -> callable:
    """Narrow ``_analyze`` seam: records the canned decision, returns it."""
    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        captured["decision"] = decision
        return decision
    return _analyzer


def _make_counting_analyze(captured: dict, count: dict, decision: dict) -> callable:
    """Wrap the canned analyzer with an analyze counter (once-ever proof)."""
    inner = _make_analyze(captured, decision)

    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        count["n"] += 1
        return inner(repo, symbol=symbol, analysis_time_utc=analysis_time_utc,
                     snapshot_id=snapshot_id)
    return _analyzer


def _policy(mode: str = "paper_bounded"):
    from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy
    return RiskAssistancePolicy(mode=mode)


# ── design §7 final gate: the pure mode conjunction ─────────────────────────


class TestRiskAdvisoryModeGate:
    """``risk_advisory_order_allowed`` encodes the worker order conjunction."""

    def _gate(self, **over: object) -> bool:
        from plugins.crypto_guard.run_ga_workers import (
            risk_advisory_order_allowed,
        )
        kwargs = {
            "proposal_verified": True,
            "final_risk_check_ok": True,
            "plan_execution_state": "confirmed",
            "account_gate_open": True,
            "regime_gate_open": True,
            "once_ever_open": True,
            "mode": "paper_bounded",
        }
        kwargs.update(over)
        return risk_advisory_order_allowed(**kwargs)

    def test_paper_bounded_all_conditions_true(self):
        assert self._gate() is True

    def test_shadow_never_allows_even_when_all_conditions_pass(self):
        assert self._gate(mode="shadow") is False

    def test_off_keeps_existing_behavior(self):
        # off = risk-advisory path absent; the existing gate decides.
        assert self._gate(mode="off") is True

    def test_paper_bounded_proposal_not_verified(self):
        assert self._gate(proposal_verified=False) is False

    def test_paper_bounded_final_risk_not_ok(self):
        assert self._gate(final_risk_check_ok=False) is False

    def test_paper_bounded_plan_not_confirmed(self):
        assert self._gate(plan_execution_state="risk_rejected") is False

    def test_paper_bounded_account_closed(self):
        assert self._gate(account_gate_open=False) is False

    def test_paper_bounded_regime_closed(self):
        assert self._gate(regime_gate_open=False) is False

    def test_paper_bounded_once_ever_closed(self):
        assert self._gate(once_ever_open=False) is False

    def test_unknown_mode_fails_closed(self):
        assert self._gate(mode="live_money") is False


# ── handler: off / shadow / paper_bounded behavior ──────────────────────────


class TestRolloutModesHandler:
    """``handle_opportunity_watch_recheck`` must enforce the risk-advisory
    envelope. RED baseline: the current handler ignores it and creates the
    order in every case below."""

    def test_off_legacy_decision_orders_as_today(self) -> None:
        """No ``risk_advisory`` envelope (off / pre-rollout decision): existing
        byte-for-byte behavior -- a gate-clearing decision creates an order."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(captured, _gate_clearing_decision()),
            )
            assert result.get("created") is True, result
            assert result.get("paper_order_id"), result
            assert captured["decision"].get("risk_advisory") is None
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders) == 1
        finally:
            handle.close()

    def test_shadow_never_orders_even_when_gate_clears(self) -> None:
        """design §7 scenario 10: in shadow the verifier may pass yet the
        hypothetical result must NOT create or alter an order."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(mode="shadow"))),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"RED: a shadow recheck must never create an order; {result}"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], "shadow hypothetical approval must not create an order"
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == []
        finally:
            handle.close()

    def test_paper_bounded_verified_adjusted_plan_orders_once(self) -> None:
        """paper_bounded + verified proposal + final risk ok + confirmed plan
        -> exactly ONE order carrying the FINAL verified (adjusted) plan."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            adjusted = {
                "side": "LONG", "entry_type": "limit", "entry_price": 99.5,
                "trigger_price": 99.5, "stop_loss": 94.0,
                "take_profits": [{"price": 107.5, "ratio": 0.5},
                                 {"price": 112.5, "ratio": 0.5}],
                "risk_percent": 0.4,
                "invalid_condition": "15m 收盘跌破 95.0",
                "reason": "结构偏多，等待回踩确认；仅用于模拟盘（调整后计划）",
            }

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(mode="paper_bounded"),
                        trade_plan=adjusted)),
            )
            assert result.get("created") is True, result
            order_id = int(result["paper_order_id"])
            row = conn.execute("SELECT * FROM paper_orders WHERE id=%s",
                               (order_id,)).fetchone()
            assert row is not None
            # The ordered plan is the FINAL verified plan, not a stale plan.
            assert abs(float(row["entry_price"]) - 99.5) < 1e-9, row["entry_price"]
            assert abs(float(row["stop_loss"]) - 94.0) < 1e-9, row["stop_loss"]
            assert int(row["trigger_watch_id"]) == watch_id
        finally:
            handle.close()

    def test_hard_blocker_no_order_even_if_llm_approves(self) -> None:
        """design §7 scenario 9: the LLM says approve_as_is but the verifier /
        final risk rerun fails -> no order, regardless of the LLM verdict."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(
                            mode="paper_bounded",
                            verification_ok=False, final_risk_check_ok=False))),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"RED: a hard blocker must block even when the LLM approves; {result}"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == []
        finally:
            handle.close()

    def test_provider_tool_schema_failure_no_order(self) -> None:
        """LLM/provider/tool/schema failure (proposal_status=failed) produces
        no order; the deterministic plan is retained but never ordered."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(
                            mode="paper_bounded", proposal_status="failed",
                            verification_ok=False, final_risk_check_ok=False))),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"RED: a provider/tool/schema failure must produce no order; {result}"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == []
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == []
        finally:
            handle.close()

    def test_failed_proposal_never_reuses_prior_adjusted_plan(self) -> None:
        """prd.md P1-6: LLM unavailable cannot reuse a stale prior proposal.
        The failed recheck keeps the ORIGINAL deterministic plan -- the order
        (if it existed) must never reflect an old adjusted plan."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            original_plan = {
                "side": "LONG", "entry_type": "limit", "entry_price": 100.0,
                "trigger_price": 100.0, "stop_loss": 95.0,
                "take_profits": [{"price": 108.0, "ratio": 0.5},
                                 {"price": 113.0, "ratio": 0.5}],
                "risk_percent": 0.5,
                "invalid_condition": "15m 收盘跌破 95.0",
                "reason": "结构偏多；仅用于模拟盘（原始计划）",
            }

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(
                            mode="paper_bounded", proposal_status="failed",
                            verification_ok=False, final_risk_check_ok=False),
                        trade_plan=original_plan)),
            )
            assert result.get("rejected") is True, result
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], "a failed proposal must never bridge to an order"
        finally:
            handle.close()


# ── once-ever watch->order bridge preserved ─────────────────────────────────


class TestOnceEverBridgePreserved:
    def test_duplicate_recheck_same_order_id_no_dup_alert(self) -> None:
        """A repeated trigger -> single analysis, single order, no duplicate
        alert, no alert_outbox row, under the verified paper_bounded path."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            count = {"n": 0}
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            decision = _gate_clearing_decision(
                risk_advisory=_risk_advisory(mode="paper_bounded"))
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_counting_analyze(captured, count, decision))
            assert first.get("created") is True, first
            assert count["n"] == 1

            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_counting_analyze(captured, count, decision))
            assert second.get("duplicate") is True, (
                f"RED: a repeated trigger must be a duplicate; {second}"
            )
            assert second.get("paper_order_id") == first.get("paper_order_id")
            assert count["n"] == 1, (
                f"analyzer must run exactly once (ran {count['n']} times)"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders) == 1
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == []
        finally:
            handle.close()

    def test_concurrent_recheck_blocked_by_task_lock(self) -> None:
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            lock_name = f"opportunity_watch_recheck:{watch_id}"
            assert repo.acquire_lock(lock_name, "concurrent_worker", 600) is True

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(mode="paper_bounded"))))
            assert result.get("ok") is False, result
            assert result.get("error") == "recheck_already_in_progress", result
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == []
        finally:
            handle.close()

    def test_terminal_order_still_holds_once_ever_link(self) -> None:
        """A terminal (filled) order still holds the link: a delayed retry is a
        duplicate, never a second order, and does not re-analyze."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            decision = _gate_clearing_decision(
                risk_advisory=_risk_advisory(mode="paper_bounded"))
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(captured, decision))
            assert first.get("created") is True, first
            order_id = int(first["paper_order_id"])
            repo.update_paper_order_status(order_id, "filled")

            captured2: dict = {}
            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_analyze(captured2, decision))
            assert second.get("duplicate") is True, (
                f"RED: a terminal order must still hold the once-ever link; {second}"
            )
            assert second.get("paper_order_id") == first.get("paper_order_id")
            assert "decision" not in captured2, (
                "a terminal once-ever retry must not re-analyze"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders) == 1
        finally:
            handle.close()


# ── observation triggers remain silent ──────────────────────────────────────


class TestObservationTriggersSilent:
    """08-04 contract A + 08-10 P2-1 (reviewer finding).

    The observation-trigger lifecycle stays INTERNAL-ONLY: no observation-trigger
    push is ever restored, and a REJECTED recheck writes nothing. But a VERIFIED
    paper_bounded recheck that CREATES an order announces the ORDER with an
    order-creation notification (PRD P2-1, exactly like ``_post_decision_effects``)
    — the notification is an order-creation push, not an observation-trigger push.
    """

    def test_rejected_recheck_stays_silent(self) -> None:
        """A rejected recheck (hard blocker / failed envelope) never fires the
        send spy and writes no alert_outbox row — the observation trigger itself
        is never user-facing."""
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            sent: list = []

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id},
                send_message=lambda *a, **kw: sent.append(kw),
                _analyze=_make_analyze(
                    {}, _gate_clearing_decision(
                        risk_advisory=_risk_advisory(
                            mode="paper_bounded", verification_ok=False,
                            final_risk_check_ok=False))),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, result
            assert result.get("sent") is False, result
            assert result.get("order_notification_sent") is not True, result
            assert sent == [], f"a rejected recheck must never push; {sent}"
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == [], "no alert_outbox row may be written by a rejected recheck"
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], "a rejected recheck must not create an order"
        finally:
            handle.close()

    def test_verified_paper_bounded_order_created_pushes_order_notification(
        self, monkeypatch,
    ) -> None:
        """P2-1 RED (reviewer finding): a VERIFIED paper_bounded recheck that
        CREATES an order announces it with ``build_order_notification`` — the
        create/pending push carries original-vs-adjusted geometry, effective
        risk, computed quantity, the TP list, the confirmation source/timeframe,
        and the final risk checks. The observation trigger itself stays silent;
        ONLY the order-creation notification is user-facing. Pre-fix code has no
        ``order_notification_sent`` key and writes no ``paper_order_filled`` row.
        """
        handle = fx.make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            sent: list = []

            from plugins.crypto_guard import run_ga_workers
            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

            # ORIGINAL candidate (pre-adjustment): wider stop 93.0 is the LLM
            # proposal; risk 0.4 < candidate 0.5 keeps monetary risk non-increasing.
            candidate = {
                "side": "LONG", "entry_type": "limit", "entry_price": 99.5,
                "trigger_price": 99.5, "stop_loss": 94.0,
                "take_profits": [{"price": 107.5, "ratio": 0.5},
                                 {"price": 112.5, "ratio": 0.5}],
                "risk_percent": 0.5,
                "invalid_condition": "15m 收盘跌破 95.0",
            }
            adjusted = {
                "side": "LONG", "entry_type": "limit", "entry_price": 99.5,
                "trigger_price": 99.5, "stop_loss": 93.0,
                "take_profits": [{"price": 107.5, "ratio": 0.5},
                                 {"price": 112.5, "ratio": 0.5}],
                "risk_percent": 0.4,
                "invalid_condition": "15m 收盘跌破 95.0",
                "reason": "结构偏多，等待回踩确认；仅用于模拟盘（调整后计划）",
            }
            lifecycle = {
                "status": "valid", "origin": "current_snapshot",
                "timeframe": "5m", "source": "price_action",
                "event_type": "BOS", "age_bars": 0, "ttl_bars": 3,
                "source_decision_id": None, "source_snapshot_id": None,
                "invalidation_reason": None,
            }
            decision = _gate_clearing_decision(
                trade_plan=adjusted,
                risk_advisory=_risk_advisory(
                    mode="paper_bounded", candidate_plan=candidate,
                    lifecycle=lifecycle,
                ),
            )
            monkeypatch.setattr(
                run_ga_workers, "resolve_report_target",
                lambda repo, payload=None: {
                    "receive_id": "oc_test", "receive_id_type": "chat_id",
                },
            )
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id},
                send_message=lambda *a, **kw: sent.append([a, kw]),
                _analyze=_make_analyze({}, decision),
            )
            assert result.get("created") is True, result
            assert result.get("order_notification_sent") is True, (
                f"P2-1 RED: a verified paper_bounded created order must announce "
                f"itself; {result}"
            )
            assert result.get("sent") is False, (
                "the observation trigger itself stays silent (only the order "
                "creation notification fires)"
            )
            rows = conn.execute(
                "SELECT * FROM alert_outbox WHERE alert_type='paper_order_filled'"
            ).fetchall()
            assert len(rows) == 1, (
                f"P2-1 RED: expected exactly one paper_order_filled order-creation "
                f"row; {rows}"
            )
            payload = rows[0]["payload_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            text = payload["fallback_text"]
            # builder header + computed quantity (10000 * 0.4% / 6.5 = 6.15)
            assert "**订单** BTCUSDT LONG limit · 数量 6.15" in text, text
            # the geometry that actually fills
            assert "- 入场 99.50 · 止损 93.00" in text, text
            # original-vs-adjusted (the whole point of P2-1)
            assert "- 原始 99.50/94.00 · 调整 99.50/93.00" in text, text
            # effective risk + the TP list
            assert "- 有效风险 0.40% · 止盈 107.50 · 112.50" in text, text
            # confirmation source/timeframe/remaining TTL + final risk checks
            assert (
                "- 入场确认 price_action 5m · 已延续 0 · 剩余 3 · 最终风控：通过"
                in text
            ), text
            # the send spy fired exactly once against the resolved target.
            # ``_deliver_alert`` calls ``send_message(receive_id, content,
            # msg_type=..., receive_id_type=...)`` — receive_id arrives
            # POSITIONALLY, so inspect the captured positional args.
            assert len(sent) == 1, sent
            assert sent[0][0][0] == "oc_test", sent
        finally:
            handle.close()


# ── LTC 4985 -> 4997 historical fixture ─────────────────────────────────────


class TestLtcHistoricalFixture:
    """The trusted 4985 BOS carries into the 4997 window, but confirmation
    presence is NOT order eligibility: the independent blockers still reject."""

    def test_4985_event_carries_into_4997_window(self) -> None:
        """With no opposite structure / price invalidation / geometry mismatch,
        the 5m bearish BOS at T0 resolves as a VALID carried confirmation one
        closed 5m bar later (age 1/3), even for the 45.51 entry geometry."""
        from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
            resolve_trusted_entry_confirmation,
        )
        from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy

        handle = fx.make_repo()
        try:
            _persist_ltc_source_event(handle, at=_T0)
            # 4997 window: range snapshot (no qualifying current event), same
            # SHORT geometry, no opposite structure, price has not crossed 45.80.
            snap = _ltc_snapshot(at=_T1, events=[])
            result = resolve_trusted_entry_confirmation(
                handle.repo, snap, _ltc_plan(), RiskAssistancePolicy())
            assert result.status == "valid"
            assert result.origin == "carried_forward"
            assert result.age_bars == 1
            assert result.ttl_bars == 3
            assert result.source_decision_id is not None
            assert result.checks["same_symbol"] is True
            assert result.checks["same_side"] is True
            assert result.checks["source_event_found"] is True
            assert result.checks["geometry_ok"] is True, (
                "the production-real 45.51 entry over the 45.34 event must carry"
            )
            assert result.checks["price_invalidation_clear"] is True
            assert result.checks["opposite_structure_absent"] is True
            assert result.checks["closed_bar_sequence_complete"] is True
        finally:
            handle.close()

    def test_carried_confirmation_does_not_force_order(self) -> None:
        """The SAME carried confirmation + the 4997 SHORT geometry stays
        NON-executable: ``news_like_event``, stop distance 0.791% < 0.8% and the
        ATR buffer independently block. 确认 != 下单."""
        from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
            resolve_trusted_entry_confirmation,
        )
        from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy
        from plugins.crypto_guard.risk.risk_adjustment_verifier import (
            candidate_plan_fingerprint,
            verify_risk_adjustment,
        )

        handle = fx.make_repo()
        try:
            _persist_ltc_source_event(handle, at=_T0)
            snap = _ltc_snapshot(at=_T1, events=[])
            lifecycle = resolve_trusted_entry_confirmation(
                handle.repo, snap, _ltc_plan(), RiskAssistancePolicy())
            assert lifecycle.status == "valid", lifecycle
            assert lifecycle.origin == "carried_forward", lifecycle

            account = {
                "enabled": True, "paused": False, "equity": 10_000.0,
                "available_balance": 10_000.0, "leverage": 1.0,
                "open_orders": 0, "max_orders": 3,
                "open_position_risk_pct": 0.0,
                "max_single_trade_risk_pct": 2.0,
                "max_total_risk_pct": 10.0, "drawdown_pct": 0.0,
            }
            # 08-10 P2-1 (reviewer): the verifier fail-closes on a proposal that
            # omits candidate_fingerprint, so the compliant proposal quotes the
            # candidate identity verbatim.
            proposal = {"verdict": "approve_as_is", "reason_codes": [],
                        "evidence_refs": [], "counter_evidence_refs": [],
                        "summary": "确认仍在，结构偏空，维持原案。",
                        "candidate_fingerprint": candidate_plan_fingerprint(
                            _ltc_plan(), snapshot=snap,
                            policy=_policy("paper_bounded"))}
            result = verify_risk_adjustment(
                candidate_plan=_ltc_plan(),
                proposal=proposal,
                confirmation_lifecycle=lifecycle,
                snapshot=snap,
                account_state=account,
                policy=_policy("paper_bounded"),
                decision_confidence=0.8,
            )
            assert result.ok is False, (
                "RED: carried confirmation must NOT flip the LTC plan to "
                "executable while the independent blockers remain"
            )
            assert result.effective_order_allowed is False
            final = result.final_risk_check or {}
            assert final.get("ok") is not True, final

            # The rejection cites the INDEPENDENT blockers, not confirmation
            # absence (the confirmation is present and carried).
            text = "\n".join(result.errors or ())
            assert any("止损" in e or "0.79" in e for e in (result.errors or ())), text
            assert any("atr" in e.lower() or "缓冲" in e for e in (result.errors or ())), text
            assert any("news" in e.lower() or "新闻" in e for e in (result.errors or ())), text
        finally:
            handle.close()
