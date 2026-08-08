# -*- coding: utf-8 -*-
"""08-06 Codex P1 fix RED tests: the ONCE-EVER watch -> order contract.

Codex final review found a blocking P1: ``idx_paper_orders_trigger_watch_once``
only constrained ``pending/open/needs_recheck`` orders, so a terminal
(filled/expired/cancelled) order EXITED the index and
``get_paper_order_by_trigger_watch`` (live-only) returned None for it. A delayed
retry recheck that fired after the first order went terminal could therefore
create a SECOND order for the same ``trigger_watch_id`` -- violating the
"duplicate trigger/retry never produces a duplicate order" contract and the
``*_once`` index name.

The fix contract (verbatim from the directive):
  * one ``opportunity_watch`` row creates at most ONE ``paper_order`` over its
    entire lifetime; re-observation requires a NEW watch row / watch_id.
  * ``idx_paper_orders_trigger_watch_once`` is UNIQUE on ALL non-NULL
    ``trigger_watch_id`` (never released by status): ``WHERE trigger_watch_id
    IS NOT NULL``. NULL ``trigger_watch_id`` signal orders stay unconstrained.
  * ``get_paper_order_by_trigger_watch`` returns the linked order of ANY status;
    a new ``get_live_paper_order_by_trigger_watch`` serves live-only callers.
  * ``create_paper_order``'s concurrent insert is backed by the DB unique
    constraint via an ON CONFLICT path that never aborts the psycopg txn; after
    a conflict it returns the existing order_id for that ``trigger_watch_id``.
  * the handler treats a terminal existing order as duplicate (no re-analysis).
  * migration: pre-08-04 no-index path creates the once-ever predicate; a
    same-name OLD live-only index is identified and REBUILT in the same
    advisory-lock txn (never silently kept by ``CREATE INDEX IF NOT EXISTS``);
    duplicate non-NULL ``trigger_watch_id`` rows fail-closed (no auto-delete);
    schema health REJECTS any status-filtered predicate; the
    ``watch_order_bridge_contract_v1`` marker is written only after the correct
    once-ever index passes health.

RED-first + revert-fail: every test fails on the pre-fix tree (status-filtered
index + live-only lookup) and passes after the fix; reverting the DB constraint
or the lookup must fail these tests.

No production DB mutation, no service restart, no commit. ``initialize_database()``
here is the ``allow_ddl=False`` test-owner call (identical DDL branch).
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from psycopg.errors import UniqueViolation

from plugins.crypto_guard.storage import migrations as mig
from plugins.crypto_guard.storage.migrations import (
    check_schema_health,
    initialize_database,
)
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import direct_conn, make_repo

pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation]

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
LEGACY_SCHEMA_SQL = (_FIXTURES_DIR / "schema_pre_08_04.sql").read_text(encoding="utf-8")

_SYMBOL = "BTCUSDT"
_BASE = 1_700_000_000_000


# ── shared helpers (mirror the 08-04 B bridge test helpers) ─────────────────


@pytest.fixture
def handle():
    h = make_repo()
    yield h
    h.close()


@pytest.fixture
def legacy_handle():
    h = make_repo(initialize_schema=False)
    yield h
    h.close()


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


def _confirmed_decision(symbol: str = _SYMBOL, **overrides: dict) -> dict:
    """A decision dict that clears ``_recheck_order_gate`` (unless overridden)."""
    decision = {
        "symbol": symbol,
        "signal_id": None,
        "ga_decision_id": 12_345,
        "plan_execution_state": "confirmed",
        "plan_origin": "llm_confirmed",
        "llm_status": "ok",
        "effective_signal_grade": "A",
        "signal_grade": "A",
        "risk_check": {"ok": True},
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


def _gate_clearing_analyze(**overrides: dict):
    """An ``_analyze`` seam returning a gate-clearing decision dict."""
    def _analyzer(repo, *, symbol, analysis_time_utc, snapshot_id):
        return _confirmed_decision(symbol, **overrides)
    return _analyzer


def _create_watch_order(handle) -> tuple[int, int]:
    """Create a watch + one linked paper order via the real recheck handler.

    Returns ``(watch_id, order_id)``.
    """
    from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

    repo = handle.repo
    watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_id = int(watch["id"])
    result = handle_opportunity_watch_recheck(
        repo, {"watch_id": watch_id}, send_message=None,
        _analyze=_gate_clearing_analyze(grade="A", side="LONG"),
    )
    assert result.get("paper_order_id"), result
    return watch_id, int(result["paper_order_id"])


# ── once-ever: a terminal order blocks a delayed-retry recheck ─────────────


@pytest.mark.parametrize("terminal_status", ["filled", "expired", "cancelled"])
def test_terminal_order_blocks_recheck_duplicate(handle, terminal_status: str) -> None:
    """Codex req 1-5 + 08-06 final-review P2 evidence: after the order goes
    filled/expired/cancelled, a recheck on the SAME watch_id must NOT
    re-analyze, NOT create a second order, and NOT emit ANY notification: the
    counting ``send_message`` spy sees 0 calls AND ``alert_outbox`` gains 0
    rows. It returns duplicate=true + the original order_id; ``paper_orders``
    still has exactly one row."""
    from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

    repo = handle.repo
    watch_id, order_id = _create_watch_order(handle)
    repo.update_paper_order_status(order_id, terminal_status)

    def _count_alert_outbox() -> int:
        row = handle.conn.execute("SELECT COUNT(*) AS c FROM alert_outbox").fetchone()
        return int(row["c"])

    before = _count_alert_outbox()
    calls = {"n": 0}
    sent = {"n": 0}

    def _spy_analyze(repo, *, symbol, analysis_time_utc, snapshot_id):
        calls["n"] += 1
        return _confirmed_decision(symbol)

    def _spy_send(*args, **kwargs):
        sent["n"] += 1
        return {"ok": True, "sent": True}

    result = handle_opportunity_watch_recheck(
        repo, {"watch_id": watch_id}, send_message=_spy_send, _analyze=_spy_analyze,
    )
    assert result.get("duplicate") is True, result
    assert int(result["paper_order_id"]) == order_id, result
    assert calls["n"] == 0, (
        f"must NOT re-analyze after a {terminal_status} order (ran {calls['n']}x)"
    )
    assert sent["n"] == 0, (
        f"must NOT send any notification after a {terminal_status} order "
        f"(send_message called {sent['n']}x)"
    )
    assert _count_alert_outbox() == before, (
        "alert_outbox must gain 0 rows on a terminal-order duplicate recheck"
    )
    rows = handle.conn.execute(
        "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
    ).fetchall()
    assert len(rows) == 1, "exactly one paper order for the watch"
    assert int(rows[0]["id"]) == order_id


def test_db_backstop_rejects_second_order_any_status(handle) -> None:
    """Codex req: the DB unique index is the backstop for a rogue writer that
    bypasses the app pre-check -- a second order for the same trigger_watch_id
    is rejected regardless of the new row's status."""
    repo = handle.repo
    watch_id, order_id = _create_watch_order(handle)
    repo.update_paper_order_status(order_id, "filled")

    with pytest.raises(UniqueViolation):
        with handle.conn.transaction():
            with handle.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO paper_orders(signal_id, symbol, side, order_type, status, trigger_watch_id, risk_check_passed) "
                    "VALUES (%s, %s, %s, 'limit', 'pending', %s, true)",
                    (None, _SYMBOL, "buy", watch_id),
                )


def test_concurrent_create_paper_order_single_row(handle) -> None:
    """Codex req 6: two workers racing to create an order for the same watch
    (both pass the pre-check before either inserts) must resolve to exactly
    ONE row via the DB unique constraint + ON CONFLICT path, with BOTH callers
    succeeding (no exception) and resolving to the SAME order id.

    The main transaction is committed before the workers spawn so no unique-index
    lock is held open across the race (a real second-process scenario); each
    worker opens its own transaction. Worker exceptions are recorded, not
    swallowed, so a second order that would otherwise crash a rogue writer is
    surfaced as a test failure."""
    repo = handle.repo
    watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_id = int(watch["id"])
    handle.conn.commit()  # release any open txn before the parallel race

    conn_b = direct_conn(handle.schema)
    try:
        repo_b = CryptoGuardRepository(conn_b)
        signal = {"symbol": _SYMBOL, "side": "LONG", "grade": "A"}
        tp = {
            "side": "LONG", "entry_type": "market", "entry_price": 100.0,
            "stop_loss": 95.0, "take_profits": [{"price": 108.0}],
            "quantity": 0.5, "reason": "watch recheck",
        }
        results: dict[str, tuple] = {}
        barrier = threading.Barrier(2)

        def _worker(name: str, r: CryptoGuardRepository) -> None:
            try:
                barrier.wait()
                order_id, created = r.create_paper_order(
                    None, signal, tp, source="watch_recheck",
                    risk_check_passed=True, trigger_watch_id=watch_id,
                )
                results[name] = ("ok", int(order_id))
            except Exception as exc:  # record, never die silently
                results[name] = ("error", type(exc).__name__)

        t1 = threading.Thread(target=_worker, args=("a", repo))
        t2 = threading.Thread(target=_worker, args=("b", repo_b))
        t1.start()
        t2.start()
        # Timeout joins (matching the legacy-upgrade R7 concurrent-init pattern):
        # if the pre-check SELECT ever regresses OUTSIDE the transaction and the
        # SAVEPOINT deadlock returns, fail fast with an explicit message instead of
        # hanging the suite until a CI-level kill.
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert t1.is_alive() is False, "worker a deadlocked (pre-check outside txn?)"
        assert t2.is_alive() is False, "worker b deadlocked (pre-check outside txn?)"

        assert results.get("a", ("missing",))[0] == "ok", results
        assert results.get("b", ("missing",))[0] == "ok", results
        ids = {v[1] for k, v in results.items() if v[0] == "ok"}
        assert len(ids) == 1, f"both workers must resolve to one order: {results}"
        rows = handle.conn.execute(
            "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
        ).fetchall()
        assert len(rows) == 1, "exactly one paper order for the watch"
        assert int(rows[0]["id"]) == next(iter(ids))
    finally:
        conn_b.close()


# ── repository lookup: once-ever vs live-only ────────────────────────────────


def test_get_paper_order_returns_terminal_order_too(handle) -> None:
    """Codex req: ``get_paper_order_by_trigger_watch`` returns the linked order
    of ANY status (a terminal order must still hold the once-ever link)."""
    repo = handle.repo
    watch_id, order_id = _create_watch_order(handle)
    repo.update_paper_order_status(order_id, "filled")

    linked = repo.get_paper_order_by_trigger_watch(watch_id)
    assert linked is not None, "once-ever: a terminal order must still satisfy the lookup"
    assert int(linked["id"]) == order_id
    assert int(linked["trigger_watch_id"]) == watch_id


def test_get_live_paper_order_is_live_only(handle) -> None:
    """Codex req: ``get_live_paper_order_by_trigger_watch`` is live-only."""
    repo = handle.repo
    watch_id, order_id = _create_watch_order(handle)
    repo.update_paper_order_status(order_id, "filled")

    live = repo.get_live_paper_order_by_trigger_watch(watch_id)
    assert live is None, "live lookup must return None after the order is terminal"


# ── migration: once-ever predicate, rebuild, fail-closed, health, marker ─────


def _apply_legacy_schema(handle) -> None:
    with handle.conn.cursor() as cur:
        cur.execute(LEGACY_SCHEMA_SQL)
    handle.conn.commit()


def _add_bridge_columns(handle) -> None:
    with handle.conn.cursor() as cur:
        cur.execute("ALTER TABLE paper_orders ADD COLUMN trigger_watch_id BIGINT")
        cur.execute("ALTER TABLE opportunity_watches ADD COLUMN recheck_status TEXT")
        cur.execute("ALTER TABLE opportunity_watches ADD COLUMN recheck_order_id BIGINT")
        cur.execute("ALTER TABLE opportunity_watches ADD COLUMN last_recheck_at TIMESTAMPTZ")
    handle.conn.commit()


def _index_predicate(handle) -> str | None:
    with handle.conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname='idx_paper_orders_trigger_watch_once'"
        )
        row = cur.fetchone()
    return (row["indexdef"] or "").lower() if row else None


def test_legacy_no_index_creates_once_ever_predicate(legacy_handle) -> None:
    _apply_legacy_schema(legacy_handle)
    result = initialize_database()
    assert result["ok"], result
    defn = _index_predicate(legacy_handle)
    assert defn is not None, "bridge index must exist after legacy upgrade"
    assert "trigger_watch_id is not null" in defn
    assert "status" not in defn, "once-ever index must NOT carry a status filter"


def test_legacy_old_live_only_index_is_rebuilt(legacy_handle) -> None:
    _apply_legacy_schema(legacy_handle)
    _add_bridge_columns(legacy_handle)
    # Simulate a DB that already ran the OLD 08-04 migration (live-only
    # predicate). initialize_database must REBUILD it, not silently keep it.
    with legacy_handle.conn.cursor() as cur:
        cur.execute(
            "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
            "ON paper_orders(trigger_watch_id) "
            "WHERE trigger_watch_id IS NOT NULL AND status IN ('pending','open','needs_recheck')"
        )
    legacy_handle.conn.commit()

    result = initialize_database()
    assert result["ok"], result
    defn = _index_predicate(legacy_handle)
    assert "trigger_watch_id is not null" in defn
    assert "status" not in defn, "old live-only index must be REBUILT to once-ever"


def test_duplicate_trigger_watch_id_fails_closed(legacy_handle) -> None:
    _apply_legacy_schema(legacy_handle)
    _add_bridge_columns(legacy_handle)
    # Two orders sharing a non-NULL trigger_watch_id (no index yet).
    with legacy_handle.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO paper_orders(signal_id, symbol, side, order_type, status, trigger_watch_id, risk_check_passed) "
            "VALUES (NULL,'BTCUSDT','buy','limit','pending',42,true)"
        )
        cur.execute(
            "INSERT INTO paper_orders(signal_id, symbol, side, order_type, status, trigger_watch_id, risk_check_passed) "
            "VALUES (NULL,'BTCUSDT','buy','limit','filled',42,true)"
        )
    legacy_handle.conn.commit()

    with pytest.raises(RuntimeError) as ctx:
        initialize_database()
    assert "trigger_watch_id" in str(ctx.value)

    # Fail-closed: business rows must NOT be auto-deleted.
    with legacy_handle.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM paper_orders WHERE trigger_watch_id=42")
        assert int(cur.fetchone()["c"]) == 2, "must not auto-delete business rows"


def test_schema_health_rejects_status_filtered_predicate(legacy_handle) -> None:
    _apply_legacy_schema(legacy_handle)
    result = initialize_database()
    assert result["ok"], result
    assert check_schema_health(conn=legacy_handle.conn)["ok"]

    # Rebuild to the OLD live-only predicate -> health must fail closed.
    with legacy_handle.conn.cursor() as cur:
        cur.execute("DROP INDEX idx_paper_orders_trigger_watch_once")
        cur.execute(
            "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
            "ON paper_orders(trigger_watch_id) "
            "WHERE trigger_watch_id IS NOT NULL AND status IN ('pending','open','needs_recheck')"
        )
    legacy_handle.conn.commit()

    health = check_schema_health(conn=legacy_handle.conn)
    assert not health["ok"], "health must reject a status-filtered predicate"
    cols = [m["column"] for m in health["missing_columns"]]
    assert any("status" in c for c in cols), f"status-filtered predicate not flagged: {health}"


def test_marker_only_after_once_ever_index(legacy_handle) -> None:
    _apply_legacy_schema(legacy_handle)
    original = mig._ensure_watch_order_bridge_contract_marker
    seen: dict[str, bool] = {}

    def _spy(cur):
        # 08-06 P2: the marker spy must judge the index with the SAME exact
        # pg_index/pg_attribute/pg_get_expr determination the migration and the
        # schema health gate use -- never a loose indexdef string check.
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
        facts = mig._introspect_once_ever_index(cur, schema)
        ok, _reason = mig._once_ever_index_is_exact(facts)
        seen["once_ever"] = ok
        return original(cur)

    mig._ensure_watch_order_bridge_contract_marker = _spy  # type: ignore[assignment]
    try:
        result = initialize_database()
    finally:
        mig._ensure_watch_order_bridge_contract_marker = original  # type: ignore[assignment]

    assert result["ok"], result
    assert seen.get("once_ever"), (
        "marker must be written only after the EXACT once-ever index passes health"
    )


# ── 08-06 final-review P1: cross-watch conflict must fail closed ─────────────


def _create_signal_linked_order(
    repo, *, signal_id: int, ga_decision_id: int | None, trigger_watch_id: int | None = None
) -> int:
    """Directly create a paper order with a specific signal_id / ga_decision_id."""
    signal = {"symbol": _SYMBOL, "side": "LONG", "grade": "A"}
    tp = {
        "side": "LONG", "entry_type": "market", "entry_price": 100.0,
        "stop_loss": 95.0, "take_profits": [{"price": 108.0}],
        "quantity": 0.5, "reason": "signal order",
    }
    order_id, created = repo.create_paper_order(
        signal_id, signal, tp,
        ga_decision_id=ga_decision_id,
        source="signal_compat", risk_check_passed=True,
        trigger_watch_id=trigger_watch_id,
    )
    assert created is True, "setup order must be freshly created"
    return int(order_id)


def _create_watch_order_with(handle, **decision_overrides) -> tuple[int, int]:
    """Create a watch + one linked order via the real recheck handler, with the
    analyzer decision overridden (e.g. a non-NULL signal_id). Returns
    ``(watch_id, order_id)``."""
    from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

    repo = handle.repo
    watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_id = int(watch["id"])
    result = handle_opportunity_watch_recheck(
        repo, {"watch_id": watch_id}, send_message=None,
        _analyze=_gate_clearing_analyze(**decision_overrides),
    )
    assert result.get("paper_order_id"), result
    return watch_id, int(result["paper_order_id"])


def _watch_order_tp() -> dict:
    return {
        "side": "LONG", "entry_type": "market", "entry_price": 100.0,
        "stop_loss": 95.0, "take_profits": [{"price": 108.0}],
        "quantity": 0.5, "reason": "watch recheck",
    }


def _create_signal_row(repo, symbol: str = _SYMBOL) -> int:
    """Create a real ``signals`` row (the FK target for ``paper_orders.signal_id``)
    and return its id. The P1 cross-watch tests need a signal_id that actually
    exists so the order INSERT does not trip the ``signals`` foreign key."""
    snapshot_id = _save_risk_approved_snapshot(repo, symbol)
    return repo.create_signal(
        {
            "symbol": symbol,
            "decision": "long_entry",
            "signal_grade": "A",
            "confidence": 0.9,
            "summary": "P1 cross-watch conflict setup signal",
            "market_bias": "bullish",
            "risk_notes": ["仅用于测试"],
            "has_trade_plan": False,
        },
        snapshot_id,
    )


def test_handler_cross_watch_conflict_not_marked_order_created(handle) -> None:
    """P1 (a) handler level: an existing NULL-watch signal order with the SAME
    signal_id + ga_decision_id must NOT masquerade as idempotent success for a
    NEW watch. The recheck's ``create_paper_order`` raises RuntimeError
    (fail-closed) and the watch is NOT marked ``order_created``; no order is
    silently bound to the watch."""
    from plugins.crypto_guard.run_ga_workers import handle_opportunity_watch_recheck

    repo = handle.repo
    signal_id = _create_signal_row(repo)
    ga_id = 12_345
    existing_id = _create_signal_linked_order(
        repo, signal_id=signal_id, ga_decision_id=ga_id, trigger_watch_id=None,
    )
    watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_id = int(watch["id"])
    handle.conn.commit()

    calls = {"n": 0}

    def _spy_analyze(repo, *, symbol, analysis_time_utc, snapshot_id):
        calls["n"] += 1
        return _confirmed_decision(symbol, signal_id=signal_id)

    with pytest.raises(RuntimeError):
        handle_opportunity_watch_recheck(
            repo, {"watch_id": watch_id}, send_message=None, _analyze=_spy_analyze,
        )

    with handle.conn.cursor() as cur:
        cur.execute(
            "SELECT recheck_status FROM opportunity_watches WHERE id=%s", (watch_id,),
        )
        r = cur.fetchone()
        assert r["recheck_status"] != "order_created", (
            "watch must NOT be marked order_created when the cross-watch conflict "
            "fails closed"
        )
    rows = handle.conn.execute(
        "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
    ).fetchall()
    assert len(rows) == 0, "no order may exist for the conflicted watch"
    # The pre-existing NULL-watch order is untouched (no auto-rebind, no delete).
    with handle.conn.cursor() as cur:
        cur.execute(
            "SELECT id, trigger_watch_id FROM paper_orders WHERE id=%s", (existing_id,),
        )
        r = cur.fetchone()
        assert r is not None and r["trigger_watch_id"] is None, (
            "existing NULL-watch order must not be rebound to the new watch"
        )


def test_cross_watch_conflict_same_signal_ga_for_new_watch_fails(handle) -> None:
    """P1 (c) repo level: same ga_decision_id (and signal_id) with a DIFFERENT /
    NULL watch must NOT masquerade as idempotent success -- ``create_paper_order``
    must fail closed instead of returning the unlinked order."""
    repo = handle.repo
    signal_id = _create_signal_row(repo)
    ga_id = 54_321
    existing_id = _create_signal_linked_order(
        repo, signal_id=signal_id, ga_decision_id=ga_id, trigger_watch_id=None,
    )
    watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_id = int(watch["id"])
    handle.conn.commit()

    signal = {"symbol": _SYMBOL, "side": "LONG", "grade": "A"}
    with pytest.raises(RuntimeError):
        repo.create_paper_order(
            signal_id, signal, _watch_order_tp(),
            ga_decision_id=ga_id, source="watch_recheck",
            risk_check_passed=True, trigger_watch_id=watch_id,
        )
    assert repo.get_paper_order_by_trigger_watch(watch_id) is None, (
        "no order may be silently bound to the new watch"
    )
    with handle.conn.cursor() as cur:
        cur.execute(
            "SELECT id, trigger_watch_id FROM paper_orders WHERE id=%s", (existing_id,),
        )
        r = cur.fetchone()
        assert r is not None and r["trigger_watch_id"] is None


def test_cross_watch_conflict_watch_a_order_blocks_watch_b(handle) -> None:
    """P1 (b) repo level: an existing order already bridged to watch A, then a
    create for watch B with the SAME signal_id + ga_decision_id must FAIL closed
    (old code returned A's order as idempotent 'success' for B, leaving B
    unlinked)."""
    repo = handle.repo
    signal_id = _create_signal_row(repo)
    watch_a, order_a = _create_watch_order_with(handle, signal_id=signal_id)
    watch_b = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
    watch_b_id = int(watch_b["id"])
    handle.conn.commit()

    signal = {"symbol": _SYMBOL, "side": "LONG", "grade": "A"}
    with pytest.raises(RuntimeError):
        repo.create_paper_order(
            signal_id, signal, _watch_order_tp(),
            ga_decision_id=12_345, source="watch_recheck",
            risk_check_passed=True, trigger_watch_id=watch_b_id,
        )
    assert repo.get_paper_order_by_trigger_watch(watch_b_id) is None, (
        "watch B must remain unlinked"
    )
    # Watch A's order is untouched.
    assert int(repo.get_paper_order_by_trigger_watch(watch_a)["id"]) == order_a


# ── 08-06 final-review P2: precise once-ever index verification ──────────────


MALFORMED_ONCE_EVER_DDL: dict[str, str] = {
    "gt_zero": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id) "
        "WHERE trigger_watch_id IS NOT NULL AND trigger_watch_id > 0"
    ),
    "extra_and": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id) "
        "WHERE trigger_watch_id IS NOT NULL AND status IS NOT NULL"
    ),
    "extra_or": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id) "
        "WHERE trigger_watch_id IS NOT NULL OR signal_id IS NOT NULL"
    ),
    "status_old": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id) "
        "WHERE trigger_watch_id IS NOT NULL AND status IN ('pending','open','needs_recheck')"
    ),
    "non_unique": (
        "CREATE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id) "
        "WHERE trigger_watch_id IS NOT NULL"
    ),
    "wrong_col": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(signal_id) "
        "WHERE trigger_watch_id IS NOT NULL"
    ),
    "composite": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id, signal_id) "
        "WHERE trigger_watch_id IS NOT NULL"
    ),
    "expr_key": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders((trigger_watch_id + 0)) "
        "WHERE trigger_watch_id IS NOT NULL"
    ),
    "no_pred": (
        "CREATE UNIQUE INDEX idx_paper_orders_trigger_watch_once "
        "ON paper_orders(trigger_watch_id)"
    ),
}


@pytest.mark.parametrize("variant", sorted(MALFORMED_ONCE_EVER_DDL))
def test_schema_health_rejects_malformed_once_ever(legacy_handle, variant: str) -> None:
    """08-06 P2: schema health must reject ANY index whose real catalog facts
    deviate from the exact once-ever contract -- extra AND/OR, status filter,
    non-unique, wrong/composite/expression key, or missing predicate. Judgment
    is precise pg_index/pg_attribute/pg_get_expr, never indexdef substrings."""
    _apply_legacy_schema(legacy_handle)
    result = initialize_database()
    assert result["ok"], result
    assert check_schema_health(conn=legacy_handle.conn)["ok"]

    with legacy_handle.conn.cursor() as cur:
        cur.execute("DROP INDEX idx_paper_orders_trigger_watch_once")
        cur.execute(MALFORMED_ONCE_EVER_DDL[variant])
    legacy_handle.conn.commit()

    health = check_schema_health(conn=legacy_handle.conn)
    assert not health["ok"], f"health must reject the {variant} once-ever index"
    cols = [m["column"] for m in health["missing_columns"]]
    assert any("idx_paper_orders_trigger_watch_once" in c for c in cols), (
        f"{variant} index not flagged by check_schema_health: {health}"
    )


@pytest.mark.parametrize("variant", sorted(MALFORMED_ONCE_EVER_DDL))
def test_legacy_non_exact_index_is_rebuilt(legacy_handle, variant: str) -> None:
    """08-06 P2: a same-name index that is NOT the exact once-ever contract
    (extra AND/OR, status filter, non-unique, wrong/composite/expression key,
    or missing predicate) must be DROPPED and REBUILT to the exact once-ever
    index by ``initialize_database`` -- detection must NOT be limited to the
    string ``status``."""
    _apply_legacy_schema(legacy_handle)
    _add_bridge_columns(legacy_handle)
    with legacy_handle.conn.cursor() as cur:
        cur.execute(MALFORMED_ONCE_EVER_DDL[variant])
    legacy_handle.conn.commit()

    result = initialize_database()
    assert result["ok"], result
    with legacy_handle.conn.cursor() as cur:
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
        facts = mig._introspect_once_ever_index(cur, schema)
        ok, reason = mig._once_ever_index_is_exact(facts)
        assert ok, f"{variant} index not rebuilt to the exact once-ever index: {reason}"
