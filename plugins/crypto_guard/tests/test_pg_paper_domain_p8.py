"""P8-4 paper-domain real-PG gate.

This is the ``每完成一个业务域立即运行对应真实 PostgreSQL 测试`` gate for the
paper/ domain migrated in P8-4 (``paper_position_updater.py``,
``position_conflict_revalidator.py``, ``shadow_virtual_trade_updater.py``,
``pending_order_manager.py``, ``pending_revalidator.py``). It drives the REAL
paper-domain functions against the real PG schema (initialized via
``initialize_database``), seeding a candle + GA decision + open paper trade/
position, then asserting the JSONB-returned-as-dict paths and the raw-write
transaction wraps survive on psycopg.

The headline risk this gate proves is the **JSONB-returned-as-dict** defect
class: pre-cutover the paper modules did ``json.loads(trade.get(
"take_profit_json") or "[]")`` but psycopg already decodes JSONB columns to a
Python list/dict, so ``json.loads(list)`` raises ``TypeError: the JSON object
must be str``. The cutover removed those ``json.loads`` wrappers
(``paper_position_updater.py`` and ``position_conflict_revalidator.py``). This
gate seeds a real JSONB ``take_profit_json`` list and runs the functions that
read it; if the wrapper regression returns, the call crashes with TypeError
instead of returning a structured dict.

It also proves the raw-write transaction wrap: ``position_conflict_revalidator``
has exactly ONE raw ``repo.conn.execute(UPDATE ...)`` (the
``cancel_reason``/``invalidated_by_ga_decision_id`` write in
``_execute_early_exit``), now wrapped in ``with repo.conn.transaction():`` with
the bare ``repo.conn.commit()`` calls dropped. A conflicting S-grade bearish GA
decision + a mocked deeply-adverse mark price triggers the early-exit path; the
gate asserts the UPDATE actually commits (``cancel_reason`` non-null afterwards)
without a bare ``commit()``.

The mark-price fetch is mocked (external network boundary) - this mirrors the
P8 producer test mocking ``build_market_state_snapshot``. Everything else runs
against the real PG schema.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from plugins.crypto_guard.paper import position_conflict_revalidator as pcr_mod
from plugins.crypto_guard.paper.position_conflict_revalidator import (
    run_position_conflict_revalidation,
)
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.paper.execution_quality import update_trade_path_metrics
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _set_app_dsn_env() -> str:
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    pg_db.reset_pool()
    return dsn


def _now_ms() -> int:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return int(base.timestamp() * 1000)


def _adverse_mark_price(symbol: str, price: float):
    """A canned ``get_mark_price_with_fallback`` returning a fixed adverse
    price - mock of the live binance network boundary."""
    def _impl(sym, *, repo=None, cache=None, max_cache_age_seconds=90.0, **_kw):
        return {
            "ok": True,
            "mark_price": price,
            "price_source": "test_mock",
            "price_as_of": None,
            "price_age_seconds": 0,
        }
    return _impl


class TestPgPaperDomainP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _seed_candle(self, symbol: str, close: float, close_time_ms: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candles(symbol, open, high, low, close, volume,
                                    close_time, open_time, interval)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (symbol, close, close, close, close, 0.0,
                 close_time_ms, close_time_ms - 60_000, "1m"),
            )
        self.conn.commit()

    def _seed_ga_decision(
        self, symbol: str, *, bias: str, grade: str, confidence: float,
        analysis_time_ms: int, trend_stage: str = "mid",
    ) -> int:
        """Seed a ga_decisions row via the REAL ``repo.create_ga_decision``
        production path (not a raw INSERT), so the NOT-NULL JSONB columns are
        populated correctly. Returns the new id."""
        from plugins.crypto_guard.utils import iso_utc_from_ms

        decision = {
            "symbol": symbol,
            "analysis_time": analysis_time_ms,
            "analysis_time_utc": iso_utc_from_ms(analysis_time_ms),
            "decision_type": "scheduled",
            "signal_grade": grade,
            "confidence": confidence,
            "market_bias": bias,
            "trend_stage": trend_stage,
            "decision": "enter" if bias != "neutral" else "monitor_only",
            "risk_check": {"ok": True},
            "trade_plan": None,
            "final_summary": "test summary",
        }
        return self.repo.create_ga_decision(decision)

    def _seed_open_long_trade(
        self, symbol: str, entry_price: float, stop_loss: float,
        *, close_time_ms: int,
    ) -> int:
        """Seed an OPEN long paper order + trade + position with a real JSONB
        ``take_profit_json`` list. Returns the trade id.

        Uses a raw INSERT for the order (no signal/ga_decision needed for the
        test), then the REAL ``repo.create_paper_trade`` to open the trade +
        position + open-position event log."""
        from plugins.crypto_guard.paper.pending_order_manager import compute_expires_at

        take_profits = [round(entry_price * 1.02, 2), round(entry_price * 1.04, 2)]
        take_profit_json = json.dumps(take_profits, ensure_ascii=False)
        exp = compute_expires_at("market")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_orders(symbol, side, order_type, entry_price, stop_loss,
                                             initial_stop_loss, take_profit_json, quantity,
                                             risk_percent, source, risk_check_passed, status,
                                             filled_at, fill_method, expires_at)
                    VALUES (%s, 'LONG', 'market', %s, %s, %s, %s, 0.1, 1.0,
                            'test_seed', TRUE, 'filled', %s, 'market', %s)
                    RETURNING id
                    """,
                    (symbol, entry_price, stop_loss, stop_loss, take_profit_json, exp, exp),
                )
                order_id = int(cur.fetchone()["id"])
        # Fetch the order as a dict (the shape create_paper_trade expects).
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_orders WHERE id=%s", (order_id,))
            order = dict(cur.fetchone())
        self.conn.commit()
        trade_id = self.repo.create_paper_trade(
            order, entry_price, fill_method="market",
            event_time=close_time_ms, allow_wall_clock=False,
        )
        return trade_id

    def test_jsonb_stop_take_path_survives_two_updates(self) -> None:
        """A psycopg-decoded list must be extended, never reset to empty."""
        symbol = "PATHUSDT"
        close_time = _now_ms()
        trade_id = self._seed_open_long_trade(
            symbol, 100.0, 95.0, close_time_ms=close_time,
        )
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE id=%s", (trade_id,))
            trade = dict(cur.fetchone())
        first = update_trade_path_metrics(
            trade,
            {"high": 102.0, "low": 99.0, "close": 101.0, "close_time": close_time + 60_000},
        )
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE paper_trades SET stop_take_path_json=%s WHERE id=%s",
                (json.dumps(first["stop_take_path"]), trade_id),
            )
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE id=%s", (trade_id,))
            persisted = dict(cur.fetchone())
        self.assertIsInstance(persisted["stop_take_path_json"], list)
        second = update_trade_path_metrics(
            persisted,
            {"high": 103.0, "low": 100.0, "close": 102.0, "close_time": close_time + 120_000},
        )
        self.assertEqual(
            len(second["stop_take_path"]),
            len(first["stop_take_path"]) + 1,
        )
        self.assertEqual(second["stop_take_path"][-2:], [
            first["stop_take_path"][-1], second["stop_take_path"][-1],
        ])

    # ── P8-4-A: JSONB-as-dict survival on update_paper_positions ────────────

    def test_update_paper_positions_reads_jsonb_take_profits_without_crash(self) -> None:
        """P8-4-A: ``update_paper_positions`` must not crash on the JSONB
        ``take_profit_json`` column. Pre-cutover ``json.loads(list)`` raised
        ``TypeError``; the cutover dropped the wrapper so the list is used
        directly. This seeds a real JSONB list and asserts the call returns a
        structured dict (ok=True) instead of raising."""
        symbol = "TESTUSDT"
        close_time_ms = _now_ms()
        self._seed_candle(symbol, close=100.0, close_time_ms=close_time_ms)
        self._seed_open_long_trade(
            symbol, entry_price=100.0, stop_loss=95.0, close_time_ms=close_time_ms,
        )
        self.conn.commit()
        result = update_paper_positions(self.repo, prices={symbol: 100.0})
        self.assertIsInstance(result, dict)
        self.assertTrue(
            result.get("ok"),
            "P8-4-A: update_paper_positions must return ok=True (no JSONB crash). "
            "Got %r" % (result,),
        )

    # ── P8-4-B: position_conflict_revalidator early-exit commits the raw write

    def test_conflict_revalidator_early_exit_commits_cancel_reason(self) -> None:
        """P8-4-B: a conflicting S-grade bearish GA decision against an open
        LONG trade, with a mocked deeply-adverse mark price, triggers
        ``_execute_early_exit`` (condition b: R <= early_exit_min_adverse_r).
        That issues the ONE raw ``UPDATE paper_orders SET cancel_reason=...``
        (now wrapped in ``with repo.conn.transaction():`` with the bare
        ``commit()`` dropped). The gate asserts the UPDATE actually committed -
        ``cancel_reason`` is non-null and ``invalidated_by_ga_decision_id``
        matches - proving the transaction wrap works without a bare
        ``commit()``. It also exercises the L860 JSONB ``take_profit_json``
        read inside ``_execute_early_exit`` and the L379
        ``analysis_time_utc >= %s::timestamptz`` cast in
        ``_count_consecutive_reverse_confirmations``."""
        symbol = "TESTUSDT"
        close_time_ms = _now_ms()
        self._seed_candle(symbol, close=100.0, close_time_ms=close_time_ms)
        self._seed_open_long_trade(
            symbol, entry_price=100.0, stop_loss=95.0, close_time_ms=close_time_ms,
        )
        self.conn.commit()
        # A later bearish S-grade decision at a strictly later analysis_time so
        # the ``analysis_time_utc >= %s::timestamptz`` window includes it.
        conflict_ms = close_time_ms + 60_000
        ga_id = self._seed_ga_decision(
            symbol, bias="bearish", grade="S", confidence=0.90,
            analysis_time_ms=conflict_ms, trend_stage="mid",
        )
        self.conn.commit()
        # entry=100, stop=95 -> R=(current-100)/5. current=94 -> R=-1.2 <= -0.30.
        with patch.object(
            pcr_mod, "get_mark_price_with_fallback",
            _adverse_mark_price(symbol, 94.0),
        ):
            result = run_position_conflict_revalidation(
                self.repo, symbol=symbol, ga_decision_id=ga_id,
            )
        self.assertIsInstance(result, dict)
        self.assertTrue(
            result.get("ok"),
            "P8-4-B: run_position_conflict_revalidation must return ok=True "
            "(no JSONB / timestamptz crash). Got %r" % (result,),
        )
        self.assertGreater(
            result.get("closed_count", 0), 0,
            "P8-4-B: the adverse-R early-exit must close the trade. "
            "Got %r" % (result,),
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT cancel_reason, invalidated_by_ga_decision_id, status "
                "FROM paper_orders WHERE symbol=%s ORDER BY id DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
        self.assertIsNotNone(row, "P8-4-B: expected a paper_orders row for %s" % symbol)
        self.assertIsNotNone(
            row["cancel_reason"],
            "P8-4-B: the raw UPDATE in _execute_early_exit must commit "
            "cancel_reason (transaction wrap without bare commit).",
        )
        self.assertEqual(
            int(row["invalidated_by_ga_decision_id"]), ga_id,
            "P8-4-B: invalidated_by_ga_decision_id must equal the conflicting GA id.",
        )

    # ── P8-4-C: revalidator dedupe JSONB access does not crash on re-run ────

    def test_conflict_revalidator_dedupe_jsonb_re_run_no_crash(self) -> None:
        """P8-4-C: ``_was_action_executed`` reads ``event_json ->> 'dedupe_key'``
        (was ``json_extract``). Running the revalidator twice on the same trade
        must not crash on the JSONB access. After the first run closes the
        trade, the second run finds no open trade (list_open_paper_trades
        excludes closed) and returns ok=True with checked_count=0 - proving the
        JSONB ``->>`` operator path works on PG and the re-run is safe."""
        symbol = "TESTUSDT"
        close_time_ms = _now_ms()
        self._seed_candle(symbol, close=100.0, close_time_ms=close_time_ms)
        self._seed_open_long_trade(
            symbol, entry_price=100.0, stop_loss=95.0, close_time_ms=close_time_ms,
        )
        self.conn.commit()
        conflict_ms = close_time_ms + 60_000
        ga_id = self._seed_ga_decision(
            symbol, bias="bearish", grade="S", confidence=0.90,
            analysis_time_ms=conflict_ms, trend_stage="mid",
        )
        self.conn.commit()
        with patch.object(
            pcr_mod, "get_mark_price_with_fallback",
            _adverse_mark_price(symbol, 94.0),
        ):
            r1 = run_position_conflict_revalidation(
                self.repo, symbol=symbol, ga_decision_id=ga_id,
            )
            self.assertTrue(r1.get("ok"))
            r2 = run_position_conflict_revalidation(
                self.repo, symbol=symbol, ga_decision_id=ga_id,
            )
        self.assertIsInstance(r2, dict)
        self.assertTrue(
            r2.get("ok"),
            "P8-4-C: second revalidator run must not crash on JSONB dedupe read. "
            "Got %r" % (r2,),
        )


if __name__ == "__main__":
    unittest.main()
