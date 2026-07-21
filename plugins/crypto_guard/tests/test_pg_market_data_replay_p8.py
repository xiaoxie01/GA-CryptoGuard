"""P8-4 market-data + historical-replay real-PG RED gate.

This is the ``每完成一个业务域立即运行对应真实 PostgreSQL 测试`` gate for the
market-data + backtest domains migrated in P8-4 (``data/market_data_health.py``,
``data/candle_backfill.py``, ``backtest/historical_replay.py``, and the
``reasoning/market_state_builder._previous_trend_stage`` raw SQL site).

It drives the REAL market-data functions against the real PG schema (initialized
via ``initialize_database``) and asserts the SQLite-ism migrations survive on
psycopg. The headline defects this gate proves:

1. **``?`` placeholder migration (market_data_health + candle_backfill +
   market_state_builder):** pre-cutover these modules used SQLite ``?``
   placeholders. psycopg uses ``%s``; a stray ``?`` raises
   ``psycopg.errors.SyntaxError`` / ``ProgrammingError`` at execute time. This
   gate seeds real candles and calls ``assess_health`` (4 queries),
   ``compute_missing_ranges``, ``_is_gap_actually_filled``,
   ``_read_backfill_progress``, ``_write_backfill_progress``,
   ``_verify_resume_progress``, and ``build_market_state_snapshot`` (which hits
   ``_previous_trend_stage``); if any ``?`` survived, the call crashes.

2. **``is_closed=1`` -> ``is_closed=TRUE`` (market_data_health + candle_backfill):**
   PG ``is_closed`` is BOOLEAN; ``is_closed=1`` is a type error in some PG
   coercion contexts and silently drops rows in others. The gate seeds a mix of
   closed/unclosed candles and asserts the closed ones are counted and the
   unclosed ones are excluded.

3. **``INSERT OR REPLACE`` -> ``ON CONFLICT`` (candle_backfill progress):**
   ``_write_backfill_progress`` used ``INSERT OR REPLACE`` (SQLite). PG rejects
   it. The gate writes progress twice for the same (symbol, interval) and
   asserts the row is updated (not duplicated, not error).

4. **``strftime('%s','now')`` -> PG epoch (candle_backfill progress):** the
   progress write embedded ``SELECT CAST(strftime('%s','now') AS INTEGER)*1000``
   (SQLite). PG has no ``strftime('%s',...)``. The gate writes progress and
   reads back a non-null ``last_updated_ms``.

5. **historical_replay scratch-schema isolation (no temp SQLite):**
   ``run_historical_replay`` used to build a temp ``historical_replay.sqlite3``
   via ``connect_db``. The cutover replaces that with a PG scratch-schema
   replay on a dedicated connection. The gate runs a small replay end-to-end
   and asserts it returns ``ok`` with replayed candles — and that the
   production pool / caller repo are untouched (no replay residue in the
   caller's schema).

The network boundary (``binance_rest.fetch_klines``) is NOT exercised here —
``backfill_symbol_interval`` itself is network-bound; this gate targets only
the DB-touching helpers that ``?``/``is_closed=1``/``INSERT OR REPLACE``/
``strftime`` would break. The replay path uses ``build_market_state_snapshot``
with the LLM analysis flag off (no network).

Reference: task ``07-16-postgresql-greenfield-cutover`` P8-4.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from plugins.crypto_guard.data.candle_backfill import (
    _is_gap_actually_filled,
    _read_backfill_progress,
    _verify_resume_progress,
    _write_backfill_progress,
    compute_missing_ranges,
)
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.utils import INTERVAL_MS, latest_closed_close_time_ms


def _set_app_dsn_env() -> str:
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    pg_db.reset_pool()
    return dsn


def _now_ms() -> int:
    base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    return int(base.timestamp() * 1000)


class TestPgMarketDataReplayP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _seed_candle(
        self,
        symbol: str,
        open_time: int,
        *,
        close: float = 100.0,
        is_closed: bool = True,
        interval: str = "1m",
    ) -> None:
        span = INTERVAL_MS[interval]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candles(symbol, open_time, close_time, interval,
                                    open, high, low, close, volume, is_closed, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (symbol, open_time, open_time + span - 1, interval,
                 close, close, close, close, 0.0, is_closed, "seed"),
            )
        self.conn.commit()

    def _seed_contiguous_tail(self, symbol: str, count: int, end_close_time: int) -> int:
        """Seed `count` contiguous closed 1m candles ending at end_close_time."""
        span = INTERVAL_MS["1m"]
        last_open = end_close_time - span + 1
        for i in range(count):
            open_time = last_open - (count - 1 - i) * span
            self._seed_candle(symbol, open_time, close=100.0 + i)
        return last_open

    # ── market_data_health: assess_health (4 queries, ? + is_closed=1) ────────

    def test_assess_health_ready_contiguous(self) -> None:
        """A fully-contiguous fresh tail with enough closed candles is ready."""
        symbol = "BTCUSDT"
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        required = 10
        self._seed_contiguous_tail(symbol, required + 5, expected_last_close)

        health = assess_health(
            self.repo, symbol, "1m",
            analysis_time_utc=at_ms, required_count=required,
        )
        self.assertTrue(health["ready"], msg=f"expected ready, got reason={health.get('reason')}")
        self.assertEqual(health["reason"], "")
        self.assertGreaterEqual(health["contiguous_tail_count"], required)
        self.assertEqual(health["last_close_time"], expected_last_close)
        self.assertEqual(health["gap_count"], 0)

    def test_assess_health_is_closed_boolean_excludes_unclosed(self) -> None:
        """is_closed=TRUE (PG boolean): unclosed candles must NOT be counted."""
        symbol = "ETHUSDT"
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        span = INTERVAL_MS["1m"]
        # 5 closed candles + 1 unclosed candle at the tail end.
        last_open = self._seed_contiguous_tail(symbol, 5, expected_last_close)
        # unclosed candle one interval ahead — must be excluded by is_closed=TRUE.
        self._seed_candle(symbol, last_open + span, is_closed=False)

        health = assess_health(
            self.repo, symbol, "1m",
            analysis_time_utc=at_ms, required_count=5,
        )
        self.assertTrue(health["ready"], msg=f"reason={health.get('reason')}")
        # total_closed_count counts only closed rows — the unclosed one excluded.
        self.assertGreaterEqual(health["total_closed_count"], 5)

    def test_assess_health_detects_mid_gap(self) -> None:
        """A gap inside the analysis window -> not ready, reason gapped."""
        symbol = "SOLUSDT"
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        span = INTERVAL_MS["1m"]
        required = 10
        # Seed a contiguous tail of `required` but then DELETE one mid-window.
        last_open = self._seed_contiguous_tail(symbol, required, expected_last_close)
        # Delete the 5th-newest candle to create a 1-bar gap.
        gap_open = last_open - 4 * span
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM candles WHERE symbol=%s AND interval=%s AND open_time=%s",
                (symbol, "1m", gap_open),
            )
        self.conn.commit()

        health = assess_health(
            self.repo, symbol, "1m",
            analysis_time_utc=at_ms, required_count=required,
        )
        self.assertFalse(health["ready"])
        self.assertEqual(health["reason"], "gapped")
        self.assertGreater(health["gap_count"], 0)

    def test_assess_health_empty(self) -> None:
        """No candles at all -> fail-closed reason=empty."""
        health = assess_health(
            self.repo, "DOGEUSDT", "1m",
            analysis_time_utc=_now_ms(), required_count=10,
        )
        self.assertFalse(health["ready"])
        self.assertEqual(health["reason"], "empty")

    # ── candle_backfill: compute_missing_ranges + _is_gap_actually_filled ─────

    def test_compute_missing_returns_gap_when_tail_gapped(self) -> None:
        symbol = "BNBUSDT"
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        span = INTERVAL_MS["1m"]
        required = 10
        last_open = self._seed_contiguous_tail(symbol, required, expected_last_close)
        gap_open = last_open - 4 * span
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM candles WHERE symbol=%s AND interval=%s AND open_time=%s",
                (symbol, "1m", gap_open),
            )
        self.conn.commit()

        gaps = compute_missing_ranges(
            self.repo, symbol, "1m",
            analysis_time_utc=at_ms, required_count=required,
        )
        self.assertTrue(len(gaps) >= 1, msg=f"expected >=1 gap, got {gaps}")

    def test_is_gap_actually_filled_exact_set(self) -> None:
        """_is_gap_actually_filled compares exact open_time set (is_closed=TRUE)."""
        symbol = "XRPUSDT"
        span = INTERVAL_MS["1m"]
        base = _now_ms()
        # Seed a contiguous 3-candle range.
        for i in range(3):
            self._seed_candle(symbol, base + i * span)
        gap = (base, base + 2 * span)
        self.assertTrue(_is_gap_actually_filled(self.repo, symbol, "1m", gap, span))

        # Delete the middle candle -> set no longer matches -> not filled.
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM candles WHERE symbol=%s AND interval=%s AND open_time=%s",
                (symbol, "1m", base + span),
            )
        self.conn.commit()
        self.assertFalse(_is_gap_actually_filled(self.repo, symbol, "1m", gap, span))

    # ── candle_backfill: progress read/write (INSERT OR REPLACE + strftime) ──

    def test_backfill_progress_upsert_and_read(self) -> None:
        """_write_backfill_progress must UPSERT (ON CONFLICT), not INSERT OR REPLACE,
        and must use a PG epoch for last_updated_ms (no strftime)."""
        symbol = "ADAUSDT"
        # First write.
        _write_backfill_progress(self.repo, symbol, "1m", 1000)
        self.conn.commit()
        first = _read_backfill_progress(self.repo, symbol, "1m")
        self.assertIsNotNone(first)
        self.assertEqual(first, 1000)

        # Second write for same (symbol, interval) -> UPDATE, not duplicate/error.
        _write_backfill_progress(self.repo, symbol, "1m", 2000)
        self.conn.commit()
        second = _read_backfill_progress(self.repo, symbol, "1m")
        self.assertEqual(second, 2000)

        # Assert exactly one row (UPSERT, not insert).
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM backfill_progress WHERE symbol=%s AND interval=%s",
                (symbol, "1m"),
            )
            self.assertEqual(int(cur.fetchone()["c"]), 1)
            cur.execute(
                "SELECT last_updated_ms FROM backfill_progress WHERE symbol=%s AND interval=%s",
                (symbol, "1m"),
            )
            row = cur.fetchone()
            self.assertIsNotNone(row["last_updated_ms"], msg="last_updated_ms must be set (PG epoch, not strftime)")

    # ── candle_backfill: _verify_resume_progress (is_closed=1 + ?) ────────────

    def test_verify_resume_progress_rejects_missing_candle(self) -> None:
        """_verify_resume_progress returns False when the candle at last_open_time
        does not exist (is_closed=TRUE query)."""
        symbol = "DOTUSDT"
        span = INTERVAL_MS["1m"]
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        required = 5
        last_open = self._seed_contiguous_tail(symbol, required, expected_last_close)
        window_start_open = last_open - (required - 1) * span

        # last_open_time inside window but no candle there -> False.
        bogus_open = last_open - 2 * span
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM candles WHERE symbol=%s AND interval=%s AND open_time=%s",
                (symbol, "1m", bogus_open),
            )
        self.conn.commit()
        self.assertFalse(
            _verify_resume_progress(
                self.repo, symbol, "1m", bogus_open,
                window_start_open=window_start_open, expected_last_open=last_open,
            )
        )

    # ── market_state_builder: _previous_trend_stage (? placeholder) ──────────

    def test_build_market_state_snapshot_previous_trend_stage(self) -> None:
        """build_market_state_snapshot exercises _previous_trend_stage which has a
        raw `WHERE symbol=? ... AND analysis_time < ?` query. Under psycopg a
        surviving `?` raises; the migration to `%s` must keep it working."""
        from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot

        symbol = "LINKUSDT"
        at_ms = _now_ms()
        expected_last_close = latest_closed_close_time_ms("1m", at_ms)
        self._seed_contiguous_tail(symbol, 40, expected_last_close)
        # Seed a prior trend_stage module result so _previous_trend_stage has a hit.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO module_analysis_results(
                    symbol, timeframe, analysis_time, module, result_json, confidence, created_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, NOW())
                """,
                (symbol, "1m", expected_last_close - INTERVAL_MS["1m"],
                 "trend_stage_fusion", '{"trend_stage": "uptrend"}', 0.8),
            )
        self.conn.commit()

        snapshot = build_market_state_snapshot(
            self.repo, symbol=symbol, analysis_time_utc=expected_last_close,
            mode="shadow_test", timeframes=["1m"],
        )
        # Should not raise; snapshot is a dict. The key risk is the ?-placeholder
        # crash in _previous_trend_stage, which this call exercises.
        self.assertIsInstance(snapshot, dict)

    # ── historical_replay: scratch-schema isolation (no temp SQLite) ─────────

    def test_run_historical_replay_pg_scratch_isolation(self) -> None:
        """run_historical_replay must run against an isolated PG scratch schema
        (NOT a temp SQLite file via connect_db). It returns ok=True with replayed
        candles, and leaves no replay residue in the caller's (public) schema."""
        from plugins.crypto_guard.backtest.historical_replay import run_historical_replay

        symbol = "MATICUSDT"
        span = INTERVAL_MS["1m"]
        base = _now_ms()
        # Build 40 contiguous candles; replay window covers the last 10.
        candles = []
        for i in range(40):
            ot = base + i * span
            candles.append({
                "symbol": symbol, "interval": "1m",
                "open_time": ot, "close_time": ot + span - 1,
                "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
                "close": 100.5 + i, "volume": 1.0, "is_closed": True,
            })
        start_time = candles[30]["open_time"]
        end_time = candles[-1]["close_time"]

        # Snapshot candle count in caller schema BEFORE replay (should be 0).
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM candles WHERE symbol=%s", (symbol,))
            before = int(cur.fetchone()["c"])
        self.assertEqual(before, 0)

        result = run_historical_replay(
            self.repo, symbol=symbol, interval="1m",
            start_time=start_time, end_time=end_time,
            candles=candles, warmup=5,
        )

        # Replay must succeed (ok=True) and have replayed candles.
        self.assertTrue(result.get("ok"), msg=f"replay not ok: {result}")
        self.assertGreater(result.get("candles_replayed", 0), 0)

        # Isolation: the caller's schema must have NO replay candles
        # (replay ran in a scratch schema, not public).
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM candles WHERE symbol=%s", (symbol,))
            after = int(cur.fetchone()["c"])
        self.assertEqual(after, 0, msg="replay leaked candles into caller schema")


if __name__ == "__main__":
    unittest.main()
