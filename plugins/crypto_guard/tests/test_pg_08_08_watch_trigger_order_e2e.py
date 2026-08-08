# -*- coding: utf-8 -*-
"""08-08 P1-4 (PRD): end-to-end watch-trigger -> order test.

The production 55/55 funnel drop: 55 ``opportunity_watch_recheck`` jobs, 55
rejected, 0 ``order_created``, ``paper_orders=0``. This test proves the FULL
watch-trigger -> recheck -> order-gate -> paper-order bridge works end to end
when the decision is produced by the REAL controller/adapter production path.

The happy-path decision MUST come from the real ``GAMasterController`` +
``fair_llm_call_adapter`` (patched provider ``_call_ga_llm`` returns raw LLM
JSON — the same seam P0-1 uses), so ``risk_check={"ok": True}`` is produced by
the real controller's ``apply_risk_to_decision`` — NEVER a hand-written ideal
decision. The ``_analyze`` seam (a real production dependency-injection point
that defaults to ``_run_recheck_analysis``) is used to inject the real
controller path with a real snapshot + real adapter candidate, exactly the fair
coordinator's production flow.

Coverage: happy path (exactly one order, real risk_check, no feishu push),
rejected path (LLM no-plan -> recheck_rejected, no order, no outbox),
concurrency (task lock -> recheck_already_in_progress), duplicate tasks
(single analysis, single order), terminal once-ever (a terminal order still
holds the link -> delayed retry is a duplicate, never a second order).

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.ga_master.controller import GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
from plugins.crypto_guard.reasoning import llm_agent_judge
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_08_04_watch_order_bridge_b import (
    _materialize_breakout_watch,
)
from plugins.crypto_guard.tests.test_pg_fair_llm_raw_reference_p0_1 import (
    _AT_MS,
    _build_snapshot,
)

_SYMBOL = "BTCUSDT"


# ── fixtures ────────────────────────────────────────────────────────────────


def _bullish_snapshot(symbol: str = _SYMBOL) -> dict:
    """Deterministic S/A fixture: raw SOP yields a LONG trade plan at
    ``plan_status="executable"`` (S grade / 0.95 confidence / entry 100.0 /
    stop 95.0 / TPs 108,112). Injects a closed bullish BOS event so the
    structured ``entry_trigger_confirmation`` is consistent with the snapshot
    (price 99.5, within the P1-2 geometry tolerance of entry 100.0)."""
    snap = _build_snapshot(symbol=symbol, bias="bullish", stage="middle",
                           structure="bullish", momentum_dir="bullish",
                           candles_count=250)
    snap.setdefault("modules", {}).setdefault("price_action", {})["structure_events"] = [
        {"event": "bullish_bos", "timeframe": "15m", "direction": "bullish",
         "candle_close_time": _AT_MS - 900_000, "price": 99.5, "closed": True,
         "symbol": symbol},
    ]
    return snap


def _entry_conf(symbol: str = _SYMBOL) -> dict:
    """The exact ``_extract_structured_entry_confirmation`` output for
    ``_bullish_snapshot(symbol)`` — the trusted deterministic confirmation."""
    return {"type": "closed_candle_confirmation", "timeframe": "15m",
            "event_type": "BOS", "direction": "bullish",
            "candle_close_time": _AT_MS - 900_000, "price": 99.5,
            "source": "price_action", "symbol": symbol}


def _raw_sop_plan(snapshot: dict, *, entry_conf: dict | None = None) -> dict:
    """The deterministic S/A trade_plan, shaped the way the real LLM response
    would carry it (with an optional structured entry confirmation)."""
    raw = run_ga_sop_decision(snapshot)
    tp = dict(raw.get("trade_plan") or {})
    if entry_conf is not None:
        tp["entry_trigger_confirmation"] = entry_conf
    return tp


def _fake_llm_provided_plan_call(snapshot: dict) -> callable:
    """LLM PROVIDES a full executable trade_plan (with structured entry
    confirmation) — the genuine confirmation path that clears the order gate."""
    symbol = str(snapshot.get("symbol") or _SYMBOL)
    plan = _raw_sop_plan(snapshot, entry_conf=_entry_conf(symbol))

    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "trade_plan_available",
            "signal_grade": "S",
            "market_bias": "bullish",
            "trend_stage": "middle",
            "confidence": 0.95,
            "summary": "强势看涨，创建模拟盘多单",
            "evidence": ["bullish_bos", "momentum"],
            "counter_evidence": [],
            "risk_notes": [],
            "has_trade_plan": True,
            "trade_plan": plan,
            "opportunity_watch": None,
            "suggested_actions": ["create_paper_order"],
            "timeframe_context": {
                tf: {"bias": "unknown", "structure": "unknown", "closed": True,
                     "close_time": _AT_MS - offset}
                for tf, offset in (("1d", 86_400_000), ("4h", 14_400_000),
                                   ("1h", 3_600_000), ("15m", 900_000))
            },
            "alignment": "aligned",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _fake_no_plan_call(symbol: str) -> callable:
    """LLM succeeds but produces NO plan (monitor_only / C grade) — the
    rejected path: the real controller yields a C-grade decision that the
    order gate rejects (grade not S/A)."""

    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "monitor_only",
            "signal_grade": "C",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.45,
            "summary": f"{symbol} no edge",
            "evidence": ["no edge"],
            "counter_evidence": ["no edge"],
            "risk_notes": [],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["ignore"],
            "timeframe_context": {
                tf: {"bias": "unknown", "structure": "unknown", "closed": True,
                     "close_time": _AT_MS - offset}
                for tf, offset in (("1d", 86_400_000), ("4h", 14_400_000),
                                   ("1h", 3_600_000), ("15m", 900_000))
            },
            "alignment": "unknown",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _adapter_call(snapshot: dict, fake_call: callable) -> tuple[dict, dict]:
    """Run the real fair-adapter on a patched provider and return the
    normalized (candidate, attempt_meta) — the exact seam the fair
    coordinator uses in production."""
    from plugins.crypto_guard.reasoning.llm_breaker import (
        CircuitBreaker, PerSymbolDeadline,
    )
    from plugins.crypto_guard.reasoning.llm_fair_scheduler import (
        resolve_fair_batch_config,
    )
    clock = {"t": 0}

    def fake_now_ms():
        return clock["t"]

    with mock.patch("plugins.crypto_guard.reasoning.llm_breaker._now_ms",
                    side_effect=fake_now_ms):
        deadline = PerSymbolDeadline(
            per_symbol_timeout_seconds=300,
            per_attempt_timeout_seconds=180,
        )
    breaker = CircuitBreaker(enabled=True, consecutive_threshold=3,
                             rate_threshold=0.5, rate_window=10,
                             min_rate_samples=5)
    breaker.llm_config_name = "test_cfg"
    breaker.llm_model = "test_model"
    cfg = resolve_fair_batch_config({
        "scheduling": {"mode": "fair_pool", "max_concurrency": 4,
                       "per_symbol_timeout_seconds": 300,
                       "per_attempt_timeout_seconds": 180,
                       "batch_completion_guard_seconds": 60,
                       "rotate_start_symbol": True},
        "retry": {"max_attempts_per_symbol": 3},
    })
    with mock.patch("plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
                    side_effect=fake_call):
        candidate, attempt_meta = llm_agent_judge.fair_llm_call_adapter(
            snapshot=snapshot, deadline=deadline, breaker=breaker,
            retry_budget=cfg, wall_clock_budget=cfg,
            attempt=1, max_attempts=cfg.max_attempts_per_symbol,
            schedule_position=0, schedule_round=1, context=None,
        )
    assert candidate is not None, attempt_meta
    return candidate, attempt_meta


def _make_real_controller_analyze(captured: dict, *, fake_call: callable | None = None) -> callable:
    """Build the ``_analyze`` seam that drives the REAL controller/adapter
    production path: real ``fair_llm_call_adapter`` (patched provider) ->
    real ``GAMasterController(repo).analyze_symbol`` with the preset
    candidate — exactly the fair coordinator's production flow. The decision
    (including ``risk_check``) is produced by real code, never hand-written.
    ``captured["decision"]`` records the final decision for assertions."""
    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        snapshot = _bullish_snapshot(symbol)
        call = fake_call or _fake_llm_provided_plan_call(snapshot)
        candidate, attempt_meta = _adapter_call(snapshot, call)
        controller = GAMasterController(repo)
        request = GAAnalysisRequest(
            symbol=symbol,
            decision_type="opportunity_watch_recheck",
            analysis_time_utc=int(snapshot.get("analysis_time_utc") or analysis_time_utc),
            mode="opportunity_watch",
            snapshot=snapshot,
            snapshot_id=snapshot_id,
            requested_by="opportunity_watch_recheck",
            request_text="opportunity watch recheck",
        )
        decision = controller.analyze_symbol(
            request,
            preset_llm_candidate=candidate,
            preset_llm_attempt_meta=attempt_meta,
        )
        captured["decision"] = decision
        return decision
    return _analyzer


def _make_counting_real_analyze(captured: dict, count: dict) -> callable:
    """Wrap the real controller analyzer with an analyze counter so the
    duplicate/once-ever tests can assert the analyzer runs exactly once."""
    inner = _make_real_controller_analyze(captured)

    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        count["n"] += 1
        return inner(repo, symbol=symbol, analysis_time_utc=analysis_time_utc,
                     snapshot_id=snapshot_id)
    return _analyzer


# ── happy path: real controller produces exactly one order ──────────────────


class TestHappyPathRealControllerProducesOrder:
    def test_real_controller_risk_check_ok_creates_linked_order(self) -> None:
        """P1-4 happy path: the REAL controller/adapter path produces
        ``risk_check={"ok": True}`` (never hand-written), the order gate
        clears, and exactly ONE paper order linked to ``trigger_watch_id`` is
        created. No feishu push / no alert_outbox row."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_real_controller_analyze(captured),
            )
            assert result.get("ok") is True, result
            order_id = result.get("paper_order_id")
            assert order_id, f"P1-4 RED: a gate-clearing recheck must create a paper order; {result}"
            assert result.get("created") is True, result

            # The decision came from the REAL controller: risk_check={"ok": True}
            # is produced by apply_risk_to_decision, not hand-written.
            decision = captured.get("decision")
            assert decision is not None, "the real controller must produce a decision"
            assert decision.get("risk_check", {}).get("ok") is True, (
                f"P1-4 RED: real controller must produce risk_check.ok=True; "
                f"got {decision.get('risk_check')!r}"
            )
            assert decision.get("plan_execution_state") == "confirmed", decision.get("plan_execution_state")
            assert decision.get("plan_origin") == "llm_confirmed", decision.get("plan_origin")
            assert decision.get("llm_status") == "ok", decision.get("llm_status")
            assert decision.get("effective_signal_grade") in ("S", "A"), decision.get("effective_signal_grade")

            # Exactly one order, linked to the watch.
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders) == 1, f"P1-4 RED: exactly one order expected; {orders}"
            row = orders[0]
            assert int(row["trigger_watch_id"]) == watch_id, row
            assert str(row["side"]) == "LONG", row["side"]
            assert row["source"] == "watch_recheck", row["source"]
            assert row["risk_check_passed"] is True, row["risk_check_passed"]

            fresh = repo.get_opportunity_watch(watch_id)
            assert fresh["recheck_status"] == "order_created", fresh
            assert int(fresh["recheck_order_id"]) == int(order_id), fresh

            # No feishu push / no alert_outbox row (internal-only bridge).
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == [], f"P1-4: no alert_outbox row may be written; {outbox}"
        finally:
            handle.close()


# ── rejected path: LLM no-plan -> recheck_rejected, no order ───────────────


class TestRejectedPathNoOrder:
    def test_llm_no_plan_rejected_no_order_no_outbox(self) -> None:
        """P1-4 rejected path: the real controller yields a C-grade decision
        (LLM produced no plan) -> the order gate rejects -> recheck_rejected,
        no paper order, no alert_outbox row."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_real_controller_analyze(
                    captured, fake_call=_fake_no_plan_call(_SYMBOL),
                ),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"P1-4 RED: a no-plan recheck must be rejected, not create an order; {result}"
            )
            decision = captured.get("decision")
            assert decision is not None
            assert decision.get("effective_signal_grade") not in ("S", "A"), decision.get("effective_signal_grade")

            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], f"P1-4: no paper order may be created; {orders}"
            fresh = repo.get_opportunity_watch(watch_id)
            assert fresh["recheck_status"] == "recheck_rejected", fresh
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == [], f"P1-4: no alert_outbox row may be written; {outbox}"
        finally:
            handle.close()


# ── concurrency: task lock blocks a concurrent recheck ──────────────────────


class TestConcurrencyTaskLock:
    def test_concurrent_recheck_blocked_by_task_lock(self) -> None:
        """P1-4 concurrency: the per-watch task lock allows only ONE recheck at
        a time. A concurrent recheck (lock already held) returns
        ``recheck_already_in_progress`` and creates no order."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            lock_name = f"opportunity_watch_recheck:{watch_id}"
            # Simulate a concurrent recheck already running (different owner).
            assert repo.acquire_lock(lock_name, "concurrent_worker", 600) is True

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_real_controller_analyze({}),
            )
            assert result.get("ok") is False, result
            assert result.get("error") == "recheck_already_in_progress", result
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], "P1-4: a lock-blocked recheck must not create an order"
        finally:
            handle.close()


# ── duplicate tasks: single analysis, single order ──────────────────────────


class TestDuplicateTasksSingleOrder:
    def test_repeated_trigger_single_analysis_single_order(self) -> None:
        """P1-4 duplicate: a repeated trigger (duplicate job) must NOT
        re-analyze or re-order — the once-ever link + task lock make the
        bridge idempotent (single analysis, single order)."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            count = {"n": 0}
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_counting_real_analyze(captured, count),
            )
            assert first.get("created") is True, first
            assert count["n"] == 1
            orders_after_first = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders_after_first) == 1

            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_counting_real_analyze(captured, count),
            )
            assert second.get("duplicate") is True, (
                f"P1-4 RED: a repeated trigger must be detected as a duplicate; {second}"
            )
            assert second.get("paper_order_id") == first.get("paper_order_id"), second
            assert count["n"] == 1, (
                f"P1-4 RED: the analyzer must run exactly once across a duplicate "
                f"trigger (ran {count['n']} times)"
            )
            orders_after_second = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders_after_second) == 1, (
                "P1-4: only one paper order may exist for the watch"
            )
        finally:
            handle.close()


# ── terminal once-ever: a terminal order still holds the link ───────────────


class TestTerminalOnceEver:
    def test_terminal_order_still_holds_once_ever_link(self) -> None:
        """P1-4 terminal once-ever: a TERMINAL (filled) order still holds the
        once-ever link, so a delayed-retry recheck is judged duplicate and
        never re-analyzes nor mints a second order."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            captured: dict = {}

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_real_controller_analyze(captured),
            )
            assert first.get("created") is True, first
            order_id = int(first.get("paper_order_id"))

            # Mark the order terminal (filled).
            repo.update_paper_order_status(order_id, "filled")

            # A delayed retry recheck fires afterwards: duplicate, no second
            # order, no re-analysis (the once-ever lookup short-circuits).
            captured2: dict = {}
            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_make_real_controller_analyze(captured2),
            )
            assert second.get("duplicate") is True, (
                f"P1-4 RED: a terminal order must still hold the once-ever link; {second}"
            )
            assert second.get("paper_order_id") == first.get("paper_order_id"), second
            assert "decision" not in captured2, (
                "P1-4: a terminal once-ever retry must not re-analyze"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders) == 1, (
                "P1-4: a terminal order must never be followed by a second order"
            )
        finally:
            handle.close()
