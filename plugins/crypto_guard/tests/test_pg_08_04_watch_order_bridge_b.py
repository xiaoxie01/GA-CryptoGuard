# -*- coding: utf-8 -*-
"""08-04 contract B (PRD): the watch -> order bridge.

A triggered ``opportunity_watch_recheck`` job runs a FRESH re-analysis from the
latest closed candle (never a stored/stale snapshot), then — and only when the
order gate clears (S/A + llm_status ok + LLM confirmed + risk_ok + valid final
trade_plan + account not paused + direction valid) — bridges the decision into
exactly ONE paper order linked to ``trigger_watch_id``. B/C/D grades,
llm-unconfirmed, risk-rejected, continuity-invalidated and candidate-only plans
never create an order. The bridge is idempotent: task lock + partial unique
index ``idx_paper_orders_trigger_watch_once`` = single analysis, single order,
no duplicate alerts. The watch->order link is persisted and queryable.

RED-first + revert-fail: each behavior fails against the pre-Phase-2 handler
(the Phase 1 minimal recheck that never created an order) and passes after the
Phase 2 bridge.

No production DB mutation, no marker write, no service restart, no commit. The
handler's ``_analyze`` seam (a real production dependency-injection point that
defaults to ``_run_recheck_analysis``) is used to inject deterministic decisions
so the gate logic is tested without an LLM call or candle volume.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.tests.pg_fixtures import make_repo

_SYMBOL = "BTCUSDT"
_BASE = 1_700_000_000_000
_SPAN = 900_000


def _save_risk_approved_snapshot(repo, symbol: str = _SYMBOL) -> int:
    snapshot = {
        "symbol": symbol,
        "analysis_time_utc": _BASE,
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
            "closed_candles_only": True, "status": "complete", "analysis_time_utc": _BASE,
            "missing_timeframes": [], "low_sample_timeframes": [],
            "health_by_tf": {"1d": {"ready": True, "last_close_time": _BASE - 8_640_000}, "4h": {"ready": True, "last_close_time": _BASE - 3_600_000}, "1h": {"ready": True, "last_close_time": _BASE - 3_600_000}, "15m": {"ready": True, "last_close_time": _BASE}},
        },
        "timeframe_context": {"1d": {"bias": "bullish", "structure": "uptrend", "closed": True}, "4h": {"bias": "bullish", "structure": "uptrend", "closed": True}, "1h": {"bias": "bullish", "structure": "uptrend", "closed": True}, "15m": {"bias": "bullish", "structure": "uptrend", "closed": True}},
        "alignment": "aligned",
        "htf_conflict": False,
        "market_reason_codes": [],
        "paper_context": {},
        "global_context": {"time_policy": "closed candles only"},
    }
    return repo.save_market_snapshot(snapshot)


def _materialize_breakout_watch(repo, symbol: str = _SYMBOL) -> dict:
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


def _upsert_triggering_candles(repo, *, base: int = _BASE, span: int = _SPAN) -> None:
    repo.upsert_candles(
        [
            {"symbol": _SYMBOL, "interval": "15m", "open_time": base, "close_time": base + span - 1, "open": 99.0, "high": 100.5, "low": 98.0, "close": 100.0, "volume": 1000, "is_closed": True},
            {"symbol": _SYMBOL, "interval": "15m", "open_time": base + span, "close_time": base + span * 2 - 1, "open": 100.0, "high": 103.0, "low": 99.5, "close": 102.0, "volume": 1200, "is_closed": True},
        ]
    )


def _confirmed_decision(symbol: str = _SYMBOL, **overrides: dict) -> dict:
    """A decision dict that clears ``_recheck_order_gate`` (unless overridden).

    ``overrides`` are merged shallowly over the base dict; nested keys (e.g.
    ``trade_plan``, ``risk_check``) are replaced wholesale.
    """
    decision = {
        "symbol": symbol,
        "signal_id": None,
        "ga_decision_id": 12_345,
        "plan_execution_state": "confirmed",
        "plan_origin": "llm_confirmed",
        "llm_status": "ok",
        "effective_signal_grade": "A",
        "signal_grade": "A",
        "risk_check": {"risk_ok": True},
        "trade_plan": {
            "side": "LONG",
            "entry_type": "market",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profits": [{"price": 108.0}],
            "quantity": 0.5,
            "reason": "watch recheck",
        },
    }
    decision.update(overrides)
    return decision


def _rejected_analyze(**overrides: dict) -> dict:
    """Build an ``_analyze`` seam returning the given decision dict."""
    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        return _confirmed_decision(symbol, **overrides)
    return _analyzer


# ── B2: fresh recheck regenerates decision from latest closed candle ─────────


class TestFreshRecheckUsesLatestClosedCandle:
    def test_recheck_builds_fresh_snapshot_not_stale_stored(self) -> None:
        """B2: the recheck builds the market-state snapshot from the LATEST
        closed candle in the DB, never a stored/stale snapshot. The seam
        exercises the real ``build_market_state_snapshot`` and captures the
        snapshot it produces."""
        handle = make_repo()
        try:
            repo = handle.repo
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            _upsert_triggering_candles(repo)
            captured: dict = {}

            def _fresh_analyze(repo, *, symbol, analysis_time_utc, snapshot_id):
                snap = build_market_state_snapshot(
                    repo, symbol=symbol, analysis_time_utc=analysis_time_utc,
                    mode="opportunity_watch",
                )
                captured["snapshot"] = snap
                return _confirmed_decision(symbol)

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch["id"]}, send_message=None, _analyze=_fresh_analyze,
            )
            assert result.get("paper_order_id"), result
            snap = captured.get("snapshot")
            assert snap is not None, "the handler must build a fresh snapshot (B2 RED: old handler reused no snapshot)"
            assert snap["symbol"] == _SYMBOL
            # The latest closed 15m candle was upserted at close_time=base+span*2-1.
            last_close = snap["data_quality"]["health"]["15m"]["last_close_time"]
            assert int(last_close) == _BASE + _SPAN * 2 - 1, (
                "B2 RED: the fresh snapshot must reflect the latest closed candle "
                f"(got last_close={last_close!r}, want {_BASE + _SPAN * 2 - 1})"
            )
            assert snap["data_quality"]["closed_candles_only"] is True
        finally:
            handle.close()


# ── B3: recheck creates paper order when the full gate clears ───────────────


class TestRecheckCreatesOrderWhenGateClears:
    def test_confirmed_decision_creates_linked_paper_order(self) -> None:
        """B3: S/A + llm ok + llm confirmed + risk_ok + valid plan + account ok
        + direction valid -> exactly one linked paper order is created."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            assert result.get("ok") is True, result
            order_id = result.get("paper_order_id")
            assert order_id, f"B3 RED: a gate-clearing recheck must create a paper order; {result}"
            assert result.get("created") is True, result

            row = conn.execute("SELECT * FROM paper_orders WHERE id=%s", (int(order_id),)).fetchone()
            assert row is not None
            assert int(row["trigger_watch_id"]) == watch_id, (
                "B3 RED: the paper order must carry trigger_watch_id (watch->order link)"
            )
            assert str(row["side"]) == "LONG", row["side"]
            assert row["source"] == "watch_recheck", row["source"]
            assert row["risk_check_passed"] is True, row["risk_check_passed"]

            fresh = repo.get_opportunity_watch(watch_id)
            assert fresh["recheck_status"] == "order_created", fresh
            assert int(fresh["recheck_order_id"]) == int(order_id), fresh
        finally:
            handle.close()


# ── B4: rejected states never create an order ───────────────────────────────


class TestRecheckRejectedNeverOrders:
    @pytest.mark.parametrize(
        "label,overrides",
        [
            ("grade_b", {"effective_signal_grade": "B", "signal_grade": "B"}),
            ("grade_c", {"effective_signal_grade": "C", "signal_grade": "C"}),
            ("grade_d", {"effective_signal_grade": "D", "signal_grade": "D"}),
            ("llm_unconfirmed", {"plan_origin": "deterministic_sop", "plan_execution_state": "unconfirmed"}),
            ("risk_rejected", {"plan_execution_state": "risk_rejected"}),
            ("continuity_invalidated", {"plan_execution_state": "invalidated"}),
            ("candidate_only", {"plan_execution_state": "no_candidate"}),
            # Watch is LONG; a SHORT plan is a direction mismatch.
            ("direction_mismatch", {
                "trade_plan": {
                    "side": "SHORT", "entry_type": "market", "entry_price": 3000.0,
                    "stop_loss": 3050.0, "take_profits": [{"price": 2800.0}],
                    "quantity": 0.1, "reason": "watch recheck",
                },
            }),
        ],
    )
    def test_rejected_states_do_not_create_order(self, label: str, overrides: dict) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(**overrides),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"B4({label}) RED: the recheck must be rejected, not create an order; {result}"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], f"B4({label}): no paper order may be created; {orders}"
            fresh = repo.get_opportunity_watch(watch_id)
            assert fresh["recheck_status"] == "recheck_rejected", fresh
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchall()
            assert outbox == [], f"B4({label}): no alert_outbox row may be written"
        finally:
            handle.close()

    def test_account_pause_blocks_order(self) -> None:
        """B3 gate: AccountRiskGuard paused (hard risk off / daily pause) must
        reject even a confirmed decision."""
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            # Seed the ONLY paper account into hard-risk-off drawdown (equity <=
            # 97% of initial 10000) so AccountRiskGuard pauses.
            repo.ensure_paper_account("default", initial_balance=10000.0)
            with repo.conn.cursor() as cur:
                cur.execute(
                    "UPDATE paper_accounts SET equity=%s, current_balance=%s "
                    "WHERE account_name='default'",
                    (9000.0, 9000.0),
                )
            repo.conn.commit()
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"B3 RED: an account-paused recheck must be rejected; {result}"
            )
            assert "pause" in str(result.get("reason") or "").lower(), result.get("reason")
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], "no order may be created while the account is paused"
        finally:
            handle.close()


# ── B5: duplicate/repeated trigger -> single analysis, single order ─────────


class TestRecheckIdempotency:
    def test_repeated_trigger_single_analysis_single_order_no_dup(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            analyze_count = {"n": 0}

            def _counting_analyze(repo, *, symbol, analysis_time_utc, snapshot_id):
                analyze_count["n"] += 1
                return _confirmed_decision(symbol)

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None, _analyze=_counting_analyze,
            )
            assert first.get("created") is True, first
            assert analyze_count["n"] == 1
            orders_after_first = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders_after_first) == 1

            # A repeated trigger (duplicate job) must NOT re-analyze or re-order.
            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None, _analyze=_counting_analyze,
            )
            assert second.get("duplicate") is True, (
                f"B5 RED: a repeated trigger must be detected as a duplicate; {second}"
            )
            assert second.get("paper_order_id") == first.get("paper_order_id"), second
            assert analyze_count["n"] == 1, (
                f"B5 RED: the analyzer must run exactly once across a duplicate "
                f"trigger (ran {analyze_count['n']} times)"
            )
            orders_after_second = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(orders_after_second) == 1, (
                "B5 RED: only one paper order may exist for the watch"
            )
        finally:
            handle.close()


# ── B6: watch -> trigger decision -> paper order link persisted/queryable ───


class TestWatchOrderLinkPersisted:
    def test_get_paper_order_by_trigger_watch_returns_linked_order(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            result = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            order_id = result.get("paper_order_id")
            assert order_id, result

            linked = repo.get_paper_order_by_trigger_watch(watch_id)
            assert linked is not None, "B6 RED: get_paper_order_by_trigger_watch must return the linked order"
            assert int(linked["id"]) == int(order_id), linked
            assert int(linked["trigger_watch_id"]) == watch_id, linked
            assert str(linked["side"]) == "LONG", linked

            # A terminal order no longer holds the link -> bridge can't re-create.
            repo.update_paper_order_status(int(order_id), "filled")
            assert repo.get_paper_order_by_trigger_watch(watch_id) is None, (
                "B6: a terminal order must not satisfy the live-link lookup"
            )
        finally:
            handle.close()


# ── B7: watch->order bridge migration is idempotent (guarded no-op) ─────────


class TestWatchOrderBridgeMigrationIdempotent:
    """B7: ``apply_08_04_watch_order_bridge_migration`` must run as a guarded
    no-op against the already-migrated schema (release path on an existing DB)
    and stay idempotent when applied twice: no error, columns kept, the partial
    unique index preserved and still enforcing one live order per watch."""

    def test_migration_applied_twice_is_noop_and_preserves_index(self) -> None:
        from plugins.crypto_guard.storage.migrations import (
            _column_exists,
            apply_08_04_watch_order_bridge_migration,
        )

        handle = make_repo()
        try:
            conn = handle.conn
            # Columns already exist under greenfield schema; apply twice.
            apply_08_04_watch_order_bridge_migration(conn)
            conn.commit()
            apply_08_04_watch_order_bridge_migration(conn)
            conn.commit()

            with conn.cursor() as cur:
                assert _column_exists(cur, "paper_orders", "trigger_watch_id"), (
                    "B7: trigger_watch_id column must be present after migration"
                )
                assert _column_exists(cur, "opportunity_watches", "recheck_status"), (
                    "B7: opportunity_watches.recheck_status must be present"
                )
                cur.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname=current_schema() AND indexname='idx_paper_orders_trigger_watch_once'
                    """
                )
                assert cur.fetchone() is not None, (
                    "B7: idx_paper_orders_trigger_watch_once must exist after migration"
                )

            # The partial unique index still enforces one live order per watch:
            # a second order for the same trigger_watch_id must be rejected.
            repo = handle.repo
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])

            from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck
            first = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            assert first.get("paper_order_id"), first
            second = handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            assert second.get("duplicate") is True, second
            assert second.get("paper_order_id") == first.get("paper_order_id"), second
        finally:
            handle.close()
