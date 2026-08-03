# -*- coding: utf-8 -*-
"""08-02 production fix P0-2 (2026-08-02): auto opportunity-watch
materialization — behavioral test + revert-fail.

Production evidence (audit-confirmed 2026-08-02): 190 decisions/24h; 92
decisions declared ``create_opportunity_watch`` in their feishu_actions but the
``opportunity_watches`` table stayed at 0 rows. The watch only ever existed as
a Feishu button a human had to click; nothing in the decision pipeline ever
materialized it. Separately, the pre-P0-2 dedupe index
(``WHERE dedupe_key IS NOT NULL``) held a terminal watch's dedupe_key forever,
so even a future auto-create could never re-arm the same symbol+direction.

Contracts (P0-2 PRD, verbatim):

- Final B-grade or safety-gate-degraded S/A, with feishu_actions containing
  ``create_opportunity_watch``, a valid structured watch, and no open paper
  order, auto-materializes opportunity_watches.
- Wire into ``_post_decision_effects`` on the real fair-batch path; no reliance
  on user clicking Feishu buttons.
- New atomic, idempotent ``upsert_auto_opportunity_watch``; only one active
  auto watch per symbol+direction; a new batch refreshes conditions, TTL,
  ga_decision_id.
- Fix the dedupe_key index contract so a terminal watch does NOT block future
  re-creation.
- Sync schema, migration, schema health.
- Keep the existing manual button creation path.

Revert-fail: undoing any single production edit (the schema index predicate,
the ON CONFLICT UPSERT, or the ``_post_decision_effects`` wire-in) must turn
the matching test RED again.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo

_ANALYSIS_TIME_UTC = 1785487499999


def _structured_watch(*, level: float = 180.0, expires_minutes: int = 120) -> dict:
    """A fully schema-valid structured watch (side on conditions AND invalid)."""
    return {
        "needed": True,
        "direction": "LONG",
        "reason": "等待回踩确认",
        "conditions": [{"type": "pullback", "side": "LONG", "level": level, "timeframe": "15m"}],
        "invalid_condition": {"type": "close_below", "side": "LONG", "level": 172.0, "timeframe": "15m"},
        "expires_minutes": expires_minutes,
    }


def _save_risk_approved_snapshot(repo, symbol: str = "SOLUSDT") -> int:
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
                        "candle_close_time": 1_700_000_000_000,
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


def _signal_decision(symbol: str = "SOLUSDT", *, grade: str = "B", watch: dict | None = None) -> dict:
    return {
        "symbol": symbol,
        "decision": "wait_for_pullback",
        "signal_grade": grade,
        "confidence": 0.67,
        "summary": "测试机会监控",
        "market_bias": "bullish",
        "risk_notes": ["仅用于测试"],
        "has_trade_plan": False,
        "opportunity_watch": watch,
        "suggested_actions": ["create_opportunity_watch"],
    }


def _compat_decision(
    symbol: str = "SOLUSDT",
    *,
    grade: str = "B",
    signal_id: int,
    ga_decision_id: int,
    watch: dict | None = None,
    suggested_actions: list[str] | None = None,
) -> dict:
    """A legacy-compat decision exactly as ``legacy_decision_from_ga_decision``
    produces for ``_post_decision_effects`` (opportunity_watch + suggested_
    actions = feishu_actions)."""
    return {
        "signal_id": int(signal_id),
        "symbol": symbol,
        "decision": "opportunity_watch" if grade == "B" else "trade_plan_available",
        "signal_grade": grade,
        "confidence": 0.72,
        "summary": "测试",
        "market_bias": "bullish",
        "risk_check": {"ok": False},
        "has_trade_plan": False,
        "trade_plan": None,
        "opportunity_watch": watch,
        "suggested_actions": (
            list(suggested_actions) if suggested_actions is not None
            else ["create_opportunity_watch"]
        ),
        "ga_decision_id": int(ga_decision_id),
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
    }


# ── 1. upsert_auto_opportunity_watch: atomic, idempotent, per-key single ────


class TestUpsertAutoOpportunityWatch:
    def test_creates_active_auto_watch(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id, action = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(),
                source_signal_id=7, ga_decision_id=9001,
            )
            assert action == "created"
            row = repo.get_opportunity_watch(watch_id)
            assert row is not None
            assert row["status"] == "active"
            assert row["symbol"] == "SOLUSDT"
            assert row["direction"] == "LONG"
            assert row["created_by_user_action"] is False
            assert row["dedupe_key"] == "auto:SOLUSDT:LONG"
            assert row["ga_decision_id"] == 9001
            assert row["source_signal_id"] == 7
            assert row["expires_at"] is not None
            assert row["watch_condition_json"] == _structured_watch()["conditions"]
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert len(active) == 1
        finally:
            handle.close()

    def test_second_call_refreshes_same_watch(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            watch_id, _ = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(level=180.0),
                source_signal_id=7, ga_decision_id=9001,
            )
            refreshed_id, action = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(level=190.0, expires_minutes=240),
                source_signal_id=8, ga_decision_id=9002,
            )
            assert action == "refreshed"
            assert refreshed_id == watch_id, (
                "idempotent refresh must reuse the SAME watch, never duplicate"
            )
            row = repo.get_opportunity_watch(watch_id)
            assert row["status"] == "active"
            assert row["watch_condition_json"][0]["level"] == 190.0, (
                "new batch must refresh conditions"
            )
            assert row["ga_decision_id"] == 9002, "new batch must refresh ga_decision_id"
            assert row["source_signal_id"] == 8
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert len(active) == 1, "still exactly one active auto watch"
        finally:
            handle.close()

    def test_terminal_watch_does_not_block_recreation(self) -> None:
        # The P0-2 dedupe contract: a triggered/invalidated/expired watch
        # RELEASES its dedupe_key so a fresh active watch can be re-created.
        handle = make_repo()
        try:
            repo = handle.repo
            first_id, _ = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(), ga_decision_id=9001,
            )
            assert repo.update_opportunity_watch_status(
                first_id, "triggered", triggered_at="2026-08-02T00:00:00Z"
            )
            second_id, action = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(), ga_decision_id=9002,
            )
            assert action == "created"
            assert second_id != first_id, (
                "terminal watch must NOT block re-creation with the same key"
            )
            rows = repo.conn.execute(
                "SELECT id, status FROM opportunity_watches "
                "WHERE symbol='SOLUSDT' AND dedupe_key='auto:SOLUSDT:LONG' ORDER BY id"
            ).fetchall()
            assert len(rows) == 2
            assert rows[0]["status"] == "triggered"
            assert rows[1]["status"] == "active"
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert len(active) == 1
        finally:
            handle.close()

    def test_manual_watch_keeps_null_dedupe_key_and_coexists(self) -> None:
        # Manual button watches keep dedupe_key NULL so they never collide
        # with (or are ever deduplicated against) auto watches.
        handle = make_repo()
        try:
            repo = handle.repo
            manual_id = repo.create_opportunity_watch(
                "SOLUSDT", _structured_watch(),
                source_signal_id=1, created_by_user_action=True,
                source_button_action="create_opportunity_watch",
            )
            manual = repo.get_opportunity_watch(manual_id)
            assert manual["dedupe_key"] is None
            assert manual["created_by_user_action"] is True
            auto_id, action = repo.upsert_auto_opportunity_watch(
                "SOLUSDT", _structured_watch(), ga_decision_id=9001,
            )
            assert action == "created"
            assert auto_id != manual_id
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert {w["id"] for w in active} == {manual_id, auto_id}
        finally:
            handle.close()

    def test_raises_without_direction(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            with pytest.raises(ValueError):
                repo.upsert_auto_opportunity_watch(
                    "SOLUSDT", {
                        "needed": True,
                        "conditions": [{"type": "pullback", "level": 180.0}],
                    },
                )
        finally:
            handle.close()

    def test_auto_ttl_normalizes_bool_string_garbage_to_240(self) -> None:
        """RED (revert-fail, R2 P2-3): the pre-fix ``or 240`` + ``int(...)``
        diverged from the manual path (create_opportunity_watch) — a bool
        ``expires_minutes=True`` became a 1-minute TTL and the numeric string
        ``"60"`` became 60 minutes, while the manual path fails closed to 240.
        After the fix the auto path uses the SAME strict positive-int
        normalization: bool, numeric string, zero, negative, and None all yield a
        240-minute TTL, so no materialized auto watch is ever accidentally
        short-lived or permanent."""
        from datetime import datetime, timezone

        handle = make_repo()
        try:
            repo = handle.repo
            for expires_minutes in [True, "60", 0, -5, None]:
                watch = _structured_watch()
                watch["expires_minutes"] = expires_minutes
                watch_id, _ = repo.upsert_auto_opportunity_watch(
                    "SOLUSDT", watch, ga_decision_id=9001,
                )
                row = repo.get_opportunity_watch(watch_id)
                assert row is not None
                expires_at = row["expires_at"]
                assert expires_at is not None, (
                    f"auto watch must NOT be permanent for expires_minutes={expires_minutes!r}"
                )
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
                assert 3 * 3600 < delta <= 4 * 3600, (
                    f"expires_minutes={expires_minutes!r} -> TTL {delta}s (want 240min)"
                )
                # Release the dedupe key so the next iteration creates a fresh row.
                repo.update_opportunity_watch_status(
                    watch_id, "invalidated", invalidated_reason="clear key for next iteration"
                )
        finally:
            handle.close()


# ── 2. Dedupe index contract (the schema fix) ──────────────────────────────


class TestDedupeIndexContract:
    def test_index_forbids_two_active_rows_same_key(self) -> None:
        # The UNIQUE PARTIAL index must forbid 2 ACTIVE rows with one key.
        import psycopg

        handle = make_repo()
        try:
            repo = handle.repo
            repo.upsert_auto_opportunity_watch("SOLUSDT", _structured_watch())
            try:
                repo.conn.execute(
                    "INSERT INTO opportunity_watches("
                    "symbol, direction, watch_reason, watch_condition_json, status, dedupe_key) "
                    "VALUES ('SOLUSDT','LONG','dup','[]'::jsonb,'active','auto:SOLUSDT:LONG')"
                )
                pytest.fail("expected UniqueViolation for a second ACTIVE row with the same dedupe_key")
            except psycopg.errors.UniqueViolation:
                # The violation aborts the connection's transaction; roll back
                # so the pooled connection returns READY, never aborted.
                repo.conn.rollback()
        finally:
            handle.close()

    def test_index_allows_terminal_and_active_same_key(self) -> None:
        # The new predicate (status='active') must NOT block a fresh active row
        # once the prior row is terminal.
        handle = make_repo()
        try:
            repo = handle.repo
            first_id, _ = repo.upsert_auto_opportunity_watch("SOLUSDT", _structured_watch())
            repo.update_opportunity_watch_status(first_id, "invalidated", invalidated_reason="x")
            repo.conn.execute(
                "INSERT INTO opportunity_watches("
                "symbol, direction, watch_reason, watch_condition_json, status, dedupe_key) "
                "VALUES ('SOLUSDT','LONG','re-arm','[]'::jsonb,'active','auto:SOLUSDT:LONG')"
            )
            rows = repo.conn.execute(
                "SELECT id FROM opportunity_watches WHERE dedupe_key='auto:SOLUSDT:LONG'"
            ).fetchall()
            assert len(rows) == 2
        finally:
            handle.close()


# ── 3. _post_decision_effects wire-in (real fair-batch side-effect path) ────


def _run_effects(repo, decision: dict) -> dict:
    # Lazy import matches _smoke_suite.py's convention: run_ga_workers has heavy
    # module-level imports that tests deliberately keep out of import time.
    from psycopg.pq import TransactionStatus

    from plugins.crypto_guard.run_ga_workers import _post_decision_effects

    result = _post_decision_effects(
        repo, decision,
        {"allow_realtime_signal_alert": False},
        send_message=None,
    )
    # Production runs each effects unit inside ``with get_conn():``, whose
    # scope-exit COMMITS any open implicit transaction (pg_db.get_conn
    # else-branch). make_repo holds the checkout open for the whole test, so
    # mirror that boundary: created/refreshed detection relies on NOW() being
    # transaction-stable, and two batches must land in separate transactions.
    if repo.conn.info.transaction_status == TransactionStatus.INTRANS:
        repo.conn.commit()
    return result


class TestPostDecisionEffectsAutoMaterialize:
    def test_b_grade_auto_materializes_watch(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            decision = _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001,
                watch=_structured_watch(),
            )
            result = _run_effects(repo, decision)
            assert result["auto_watch"]["action"] == "created", result
            row = repo.get_opportunity_watch(result["auto_watch"]["watch_id"])
            assert row["status"] == "active"
            assert row["created_by_user_action"] is False
            assert row["dedupe_key"] == "auto:SOLUSDT:LONG"
            assert row["source_signal_id"] == signal_id
        finally:
            handle.close()

    def test_next_batch_refreshes_watch_in_place(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            first = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001, watch=_structured_watch(level=180.0),
            ))
            second = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9002, watch=_structured_watch(level=190.0),
            ))
            assert second["auto_watch"]["action"] == "refreshed"
            assert second["auto_watch"]["watch_id"] == first["auto_watch"]["watch_id"]
            row = repo.get_opportunity_watch(first["auto_watch"]["watch_id"])
            assert row["watch_condition_json"][0]["level"] == 190.0
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert len(active) == 1
        finally:
            handle.close()

    def test_open_paper_order_blocks_watch(self) -> None:
        # "no open paper order" gate: with a pending order the watch must NOT
        # materialize (the order supersedes the watch).
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            repo.create_paper_order(
                signal_id,
                {"symbol": "SOLUSDT"},
                {"side": "LONG", "entry_type": "limit", "entry_price": 180.0,
                 "stop_loss": 172.0, "take_profits": [{"price": 196.0}]},
                ga_decision_id=9001, source="test", risk_check_passed=True,
            )
            assert len(repo.list_open_paper_orders_for_symbol("SOLUSDT")) == 1
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001,
                watch=_structured_watch(),
            ))
            assert result["auto_watch"] is None, (
                "no watch when an open paper order exists for the symbol"
            )
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()

    def test_action_missing_does_not_materialize(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001,
                watch=_structured_watch(), suggested_actions=[],
            ))
            assert result["auto_watch"] is None
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()

    def test_unstructured_watch_does_not_materialize(self) -> None:
        # Fail-closed: a text-condition watch (pre-P0-3 shape) must NOT create
        # an auto watch; the wire-in refuses it (no fabrication).
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            bad_watch = {
                "needed": True, "direction": "LONG",
                "conditions": ["15M 收盘突破上沿或跌破下沿"],
                "invalid_condition": None,
            }
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001, watch=bad_watch,
            ))
            assert result["auto_watch"] is None
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()

    def test_manual_button_path_preserved(self) -> None:
        # The manual Feishu button still works alongside auto materialization;
        # both watches coexist because the manual row keeps dedupe_key NULL.
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(
                _signal_decision(watch=_structured_watch()), snapshot_id,
            )
            from plugins.crypto_guard.run_ga_workers import handle_button_callback

            button = handle_button_callback(
                repo,
                {"action": "create_opportunity_watch", "symbol": "SOLUSDT",
                 "signal_id": signal_id},
            )
            assert button["ok"] is True, f"manual button must keep working; {button}"
            manual = repo.get_opportunity_watch(button["watch_id"])
            assert manual["created_by_user_action"] is True
            assert manual["dedupe_key"] is None
            auto_result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9001,
                watch=_structured_watch(),
            ))
            assert auto_result["auto_watch"]["action"] == "created"
            assert auto_result["auto_watch"]["watch_id"] != button["watch_id"]
            active = repo.list_active_opportunity_watches_for_symbol("SOLUSDT")
            assert {w["id"] for w in active} == {manual["id"], auto_result["auto_watch"]["watch_id"]}
        finally:
            handle.close()


# ── 4. Schema health: the tightened index contract is enforced ──────────────


class TestSchemaHealthP0_2:
    def test_schema_health_ok_after_initialize(self) -> None:
        from plugins.crypto_guard.storage.migrations import check_schema_health

        handle = make_repo()
        try:
            health = check_schema_health(conn=handle.conn)
            assert health["ok"] is True, f"schema must be healthy; {health}"
        finally:
            handle.close()
