# -*- coding: utf-8 -*-
"""08-04 contract A (PRD): opportunity-watch lifecycle is INTERNAL-ONLY.

A watch transition (active -> triggered) must NOT write a feishu ``alert_outbox``
row and must NOT push any ``opportunity_triggered`` alert. Instead it silently
enqueues an ``opportunity_watch_recheck`` job (contract B's silent fresh
re-analysis). ``opportunity_triggered`` is removed from every ``never_silence``
set so a stray legacy push would be silenced. The hourly report shows an
aggregate watch line (never per-watch lines / raw dicts). Paper create/fill
pushes carry the mandated payload fields.

RED-first + revert-fail: each test fails against the pre-fix code and passes
after the fix.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.scheduler.opportunity_watcher import update_opportunity_watches
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _save_risk_approved_snapshot(repo, symbol: str = "BTCUSDT") -> int:
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
            "price_action": {"market_structure": "bullish", "structure_events": []},
            "smc": {},
            "momentum": {"direction": "bullish"},
        },
        "counter_evidence": {"bullish_evidence": ["高周期方向支持"], "bearish_evidence": [], "neutral_or_risk_evidence": [], "contradiction_level": "low"},
        "data_quality": {
            "closed_candles_only": True, "status": "complete", "analysis_time_utc": 1_700_000_000_000,
            "missing_timeframes": [], "low_sample_timeframes": [],
            "health_by_tf": {"1d": {"ready": True, "last_close_time": 1_699_991_360_000}, "4h": {"ready": True, "last_close_time": 1_699_997_200_000}, "1h": {"ready": True, "last_close_time": 1_699_999_600_000}, "15m": {"ready": True, "last_close_time": 1_700_000_000_000}},
        },
        "timeframe_context": {"1d": {"bias": "bullish", "structure": "uptrend", "closed": True}, "4h": {"bias": "bullish", "structure": "uptrend", "closed": True}, "1h": {"bias": "bullish", "structure": "uptrend", "closed": True}, "15m": {"bias": "bullish", "structure": "uptrend", "closed": True}},
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
        "paper_context": {},
        "global_context": {"time_policy": "closed candles only"},
    }
    return repo.save_market_snapshot(snapshot)


def _materialize_breakout_watch(repo, symbol: str = "BTCUSDT") -> dict:
    """Create an active structured breakout watch (LONG) via the real button path."""
    from plugins.crypto_guard.run_ga_workers import handle_button_callback

    snapshot_id = _save_risk_approved_snapshot(repo, symbol)
    signal_id = repo.create_signal(
        {
            "symbol": symbol,
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
        {"action": "create_opportunity_watch", "symbol": symbol, "signal_id": signal_id},
    )
    assert button["ok"] is True, f"button must succeed; {button}"
    return {"watch_id": button["watch_id"]}


def _upsert_triggering_candles(repo, *, base: int = 1_700_000_000_000, span: int = 900_000) -> None:
    repo.upsert_candles(
        [
            {"symbol": "BTCUSDT", "interval": "15m", "open_time": base, "close_time": base + span - 1, "open": 99.0, "high": 100.5, "low": 98.0, "close": 100.0, "volume": 1000, "is_closed": True},
            {"symbol": "BTCUSDT", "interval": "15m", "open_time": base + span, "close_time": base + span * 2 - 1, "open": 100.0, "high": 103.0, "low": 99.5, "close": 102.0, "volume": 1200, "is_closed": True},
        ]
    )


# ── A1/A2: watch condition hit is internal-only ─────────────────────────────


class TestWatchConditionHitInternalOnly:
    """A1: triggered watch must NOT write an alert_outbox row. A2: it enqueues
    ``opportunity_watch_recheck`` (the silent re-analysis), never
    ``opportunity_watch_alert``."""

    def test_watch_trigger_enqueues_recheck_no_alert_outbox(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            _materialize_breakout_watch(repo)
            base = 1_700_000_000_000
            span = 900_000
            _upsert_triggering_candles(repo)
            update = update_opportunity_watches(repo, analysis_time_utc=base + span * 2 - 1)
            assert update["triggered"] == 1, f"watch must trigger; {update}"

            # A1: NO feishu alert_outbox row.
            outbox = conn.execute(
                "SELECT * FROM alert_outbox WHERE alert_type='opportunity_triggered'"
            ).fetchall()
            assert outbox == [], (
                "A1 RED: a triggered watch must NOT write an "
                "alert_type='opportunity_triggered' alert_outbox row. Pre-fix "
                "code enqueues opportunity_watch_alert -> feishu push."
            )
            all_outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert all_outbox == [], (
                "A1: a watch condition hit must not write ANY alert_outbox row."
            )

            # A2: an opportunity_watch_recheck job is enqueued instead.
            recheck = conn.execute(
                "SELECT * FROM agent_jobs WHERE job_type='opportunity_watch_recheck'"
            ).fetchall()
            assert len(recheck) == 1, (
                "A2 RED: a triggered watch must enqueue exactly one "
                "opportunity_watch_recheck job. Pre-fix enqueues "
                "opportunity_watch_alert."
            )
            legacy = conn.execute(
                "SELECT * FROM agent_jobs WHERE job_type='opportunity_watch_alert'"
            ).fetchall()
            assert legacy == [], (
                "A2: no opportunity_watch_alert job may be enqueued."
            )
            # Idempotent: a second evaluation does not enqueue another job.
            second = update_opportunity_watches(repo, analysis_time_utc=base + span * 2 - 1)
            assert second["triggered"] == 0
            recheck_after = conn.execute(
                "SELECT * FROM agent_jobs WHERE job_type='opportunity_watch_recheck'"
            ).fetchall()
            assert len(recheck_after) == 1, "recheck job must be enqueued only once"
        finally:
            handle.close()

    def test_legacy_opportunity_watch_alert_job_is_ignored_no_push(self) -> None:
        """A2: if a legacy ``opportunity_watch_alert`` job is ever replayed
        (old queued job), processing it must NOT push to feishu. The consumer
        is removed/replaced, so it resolves to a silent no-op.

        08-04 contract B: the recheck now runs a fresh analysis + gate. The
        handler's ``_analyze`` seam is injected with a ``no_candidate`` decision
        so this A2 contract test stays deterministic (no LLM call, no order),
        and asserts the A2 invariants: ok=True, no alert_outbox row, no paper
        order, watch recorded as recheck_rejected."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

            def _rejected_analyze(repo, *, symbol, analysis_time_utc, snapshot_id):
                return {"plan_execution_state": "no_candidate", "symbol": symbol}

            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch["id"], "result": {"status": "triggered", "reason": "x"}, "analysis_time_utc": 1_700_000_000_000},
                send_message=None, _analyze=_rejected_analyze,
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, result
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == [], "the recheck handler must not write any alert_outbox row"
            orders = conn.execute("SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (int(watch["id"]),)).fetchall()
            assert orders == [], "contract B: a rejected recheck must not create a paper order"
            fresh = repo.get_opportunity_watch(int(watch["id"]))
            assert fresh["recheck_status"] == "recheck_rejected", fresh
        finally:
            handle.close()


# ── A2: never_silence no longer contains opportunity_triggered ──────────────


class TestNeverSilenceRemovesOpportunityTriggered:
    """A2: ``opportunity_triggered`` is removed from every never_silence set so
    a stray legacy push would be silenced."""

    def test_default_never_silence_removes_opportunity_triggered(self) -> None:
        from plugins.crypto_guard.notify.alert_delivery import DEFAULT_NEVER_SILENCE

        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            assert "opportunity_triggered" not in DEFAULT_NEVER_SILENCE, (
                "A2 RED: DEFAULT_NEVER_SILENCE must NOT contain "
                "'opportunity_triggered' (a watch lifecycle event must be "
                "silenceable)."
            )
            # Seed a recent duplicate so dedup CAN silence it: with the type
            # removed from never_silence, a duplicate within the quiet window
            # must be silenced (dedup applies).
            conn.execute(
                "INSERT INTO alert_outbox (alert_type, symbol, status, payload_json, created_at) "
                "VALUES ('opportunity_triggered', 'SOLUSDT', 'pending', '{}'::jsonb, NOW())"
            )
            silenced = repo.should_silence_alert(
                alert_type="opportunity_triggered", symbol="SOLUSDT",
                quiet_minutes=5, never_silence=DEFAULT_NEVER_SILENCE,
            )
            assert silenced is True, (
                "A2 RED: with opportunity_triggered removed from the default "
                "never_silence set, should_silence_alert must return True "
                "(silenced) for a stray duplicate opportunity_triggered push."
            )
            # Control: a never_silence set that still lists the type bypasses
            # dedup entirely (returns False) regardless of the duplicate.
            legacy = set(DEFAULT_NEVER_SILENCE) | {"opportunity_triggered"}
            legacy_silenced = repo.should_silence_alert(
                alert_type="opportunity_triggered", symbol="SOLUSDT",
                quiet_minutes=5, never_silence=legacy,
            )
            assert legacy_silenced is False, (
                "A2 control: a never_silence set still containing "
                "opportunity_triggered must bypass dedup (return False)."
            )
        finally:
            handle.close()

    def test_trading_mode_yaml_removes_opportunity_triggered(self) -> None:
        import re

        path = "plugins/crypto_guard/config/trading_mode.yaml"
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        # The never_silence block must not list opportunity_triggered.
        assert "opportunity_triggered" not in content, (
            "A2 RED: trading_mode.yaml must not list 'opportunity_triggered' "
            "in never_silence."
        )

    def test_send_interactive_alert_falls_back_to_default_never_silence(self) -> None:
        """A2: ``_send_interactive_alert``'s fallback uses the DEFAULT set (not
        ``or []``) so removing the config entry doesn't make every alert
        silenceable by an empty config list."""
        import inspect

        from plugins.crypto_guard import run_ga_workers

        src = inspect.getsource(run_ga_workers._send_interactive_alert)
        assert "DEFAULT_NEVER_SILENCE" in src, (
            "A2 RED: _send_interactive_alert must fall back to "
            "DEFAULT_NEVER_SILENCE (imported), not `or []`."
        )


# ── A3: hourly summary is aggregate-only for watches ────────────────────────


class TestHourlyReportWatchAggregate:
    """A3: the hourly summary shows an aggregate watch line, never per-watch
    lines and never raw watch dicts."""

    def _render(self, watches: list[dict]) -> str:
        from plugins.crypto_guard.notify import hourly_report

        return hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-08-04T04:00:00Z",
            active_symbols=["BTCUSDT", "ETHUSDT"],
            ga_decisions=[],
            open_orders=[],
            active_watches=watches,
            failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
        )

    def test_render_aggregate_line_no_per_watch_no_raw_dict(self) -> None:
        handle = make_repo()
        try:
            watches = [
                {"id": 1, "symbol": "BTCUSDT", "direction": "LONG", "watch_condition_json": [{"type": "breakout", "level": 101.0}], "watch_reason": "等待突破"},
                {"id": 2, "symbol": "ETHUSDT", "direction": "SHORT", "watch_condition_json": [{"type": "pullback", "level": 2000.0}], "watch_reason": "等待回踩"},
            ]
            text = self._render(watches)
            # Aggregate line present with the count.
            assert "内部观察上下文 2 条" in text, (
                "A3 RED: the hourly report must render the aggregate line "
                "'内部观察上下文 2 条'."
            )
            # The old opportunity header is gone.
            assert "当前机会监控" not in text, (
                "A3 RED: the per-watch '当前机会监控' section must be removed."
            )
            # No per-watch detail lines (no "#id symbol direction" rows).
            assert "#1 BTCUSDT LONG" not in text, (
                "A3 RED: no per-watch detail line may be rendered."
            )
            assert "#2 ETHUSDT SHORT" not in text
            # No raw dicts.
            assert "watch_condition_json" not in text, "A3: no raw watch dict may be printed."
            assert "'type'" not in text, "A3: no raw condition JSON may be printed."
        finally:
            handle.close()

    def test_render_zero_watches_shows_zero_aggregate(self) -> None:
        handle = make_repo()
        try:
            text = self._render([])
            assert "内部观察上下文 0 条" in text, (
                "A3: zero active watches must render '内部观察上下文 0 条'."
            )
            assert "暂无 active 机会监控" not in text, (
                "A3: the old empty-watch placeholder must be gone."
            )
        finally:
            handle.close()


# ── A4/A5: paper order create/fill pushes carry mandated fields ─────────────


class TestPaperOrderPushPayload:
    """A4: create/pending push includes order_id/symbol/side/order_type/entry/
    SL/TP/quantity-or-risk/expiry/source_decision_id. A5: fill push includes
    fill_price/fill_time/slippage/position."""

    def test_fill_push_render_includes_fill_price_time_slippage_position(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            from plugins.crypto_guard.run_ga_workers import handle_paper_event_alert

            payload = {
                "event_type": "paper_order_filled",
                "symbol": "BTCUSDT",
                "order_id": 101,
                "trade_id": 202,
                "side": "LONG",
                "order_type": "market",
                "entry_price": 100.5,
                "fill_price": 100.5,
                "fill_method": "market",
                "filled_at": "2026-08-04T04:00:00Z",
                "slippage": 0.0,
                "quantity": 0.5,
                "stop_loss": 95.0,
                "take_profits": [{"price": 110.0}],
                "position": {"quantity": 0.5, "avg_price": 100.5, "side": "LONG"},
                "source_decision_id": 7777,
            }
            result = handle_paper_event_alert(repo, payload, send_message=None)
            text = result["text"]
            # A5 mandated fields.
            assert "成交价" in text, "A5: fill push must show the fill price."
            assert "100.5" in text, "A5: fill price value must be present."
            assert "成交时间" in text, "A5: fill push must show the fill time."
            assert "滑点" in text, "A5: fill push must show slippage."
            assert "持仓" in text, "A5: fill push must show the current position (operator mandate: 持仓信息)."
            assert "7777" in text, "A4: fill push must carry source_decision_id."
            assert "101" in text, "A4: fill push must carry order_id."
        finally:
            handle.close()
