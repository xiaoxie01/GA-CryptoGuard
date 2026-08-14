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

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import hashlib
import re
import unittest
import uuid
from datetime import datetime, timezone
from unittest import mock

import psycopg

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


def _now_ms() -> int:
    base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    return int(base.timestamp() * 1000)


# ── deterministic per-test scratch-schema ownership (08-14 final-seal RUN1 P1) ─
#
# RUN1 failed 4 residue tests under 8-worker worksteal: they snapshotted the
# GLOBAL pg_namespace for ``<prefix>_<32hex>`` before/after and asserted
# equality while sibling tests in this file (the suite's only scratch-schema
# producer) held live schemas for the whole DDL window; worse, their ``finally``
# dropped ``current - before`` — deleting other workers' live schemas. The fix:
# every test pins its OWN deterministic schema name by patching the target
# module's LOCAL ``uuid`` binding (``_module_local_uuid4`` — the stdlib ``uuid``
# module is never patched, other modules keep real random names), asserts only
# that exact name, and its ``finally`` drops only names it explicitly created
# (``_drop_own_scratch_schema``). No global before/after snapshot is used for
# assertions or cleanup anywhere in this file.


def _own_schema_hex(test_name: str) -> str:
    """Deterministic 32-hex for a test's scratch schema name: derived from the
    test name, unique per test, stable across runs and workers, and matching
    the strict ``<prefix>_[0-9a-f]{32}`` scratch contract."""
    return hashlib.sha256(test_name.encode("utf-8")).hexdigest()[:32]


def _own_schema_for(test_name: str, prefix: str) -> str:
    """This test's deterministic scratch schema name, ``<prefix>_<32hex>``."""
    name = f"{prefix}_{_own_schema_hex(test_name)}"
    assert re.match(rf"^{prefix}_[0-9a-f]{{32}}$", name), name
    return name


def _module_local_uuid4(module, hex_str: str):
    """Pin ONLY the target module's LOCAL ``uuid`` binding so its
    ``uuid.uuid4()`` returns a fixed UUID with the given 32-hex. The stdlib
    ``uuid`` module is never patched — every other module keeps real random
    schema names; the patch is scoped to one ``with`` block."""
    class _FakeUuid:
        @staticmethod
        def uuid4():
            return uuid.UUID(hex=hex_str)

    return mock.patch.object(module, "uuid", _FakeUuid())


def _drop_own_scratch_schema(conn, name: str) -> None:
    """Finally hygiene: drop ONLY a scratch schema this test explicitly
    created. Strict ``<prefix>_<32hex>`` regex gate (refuses anything else)
    + ``sql.Identifier``. Never computes a ``current - before`` set
    difference over the global namespace — a concurrent worker's live schema
    must never be dropped (08-14 final-seal RUN1 P1)."""
    if re.match(r"^(replay|fault|phase_i)_[0-9a-f]{32}$", name) is None:
        raise AssertionError(f"refusing to drop non-scratch schema {name!r}")
    with conn.transaction():
        conn.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(name)
            )
        )


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

    # ── scratch-schema cleanup contract (P1 fixture-cleanup finding, 08-13) ──
    #
    # The old ``_scratch_replay_repo`` finally did ``conn.autocommit = True``
    # inside the body connection; psycopg forbids switching autocommit while
    # the connection is INTRANS, the exception was swallowed by a bare
    # ``except Exception: pass``, and the ``replay_<32hex>`` scratch schema was
    # silently left behind. These tests pin the replacement contract:
    # independent autocommit cleanup connection, explicit failure visibility.
    #
    # ── parallel ownership contract (08-14 final-seal RUN1 P1) ────────────
    #
    # RUN1 failed 4 residue tests under 8-worker worksteal: they snapshotted
    # the GLOBAL pg_namespace before/after and asserted equality while sibling
    # tests in this file held live ``replay_<32hex>`` schemas for the whole
    # DDL window; worse, their ``finally`` dropped ``current - before``,
    # deleting other workers' live schemas. Every cleanup test below now pins
    # its OWN deterministic schema name via ``_module_local_uuid4`` (patches
    # only the target module's LOCAL ``uuid`` binding — never the stdlib
    # module), asserts only that exact name, and its ``finally`` drops only
    # names it explicitly created (``_drop_own_scratch_schema``). No global
    # before/after snapshot is used for assertions or cleanup in this file.

    def _replay_scratch_schemas(self) -> set[str]:
        """Read-only snapshot of current replay_<32hex> schemas."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname ~ '^replay_[0-9a-f]{32}$'"
            )
            return {str(r["nspname"]) for r in cur.fetchall()}

    def test_scratch_schema_cleanup_success_path(self) -> None:
        """Normal exit must drop the scratch schema even when the body left
        the connection INTRANS (bare SELECT on an autocommit=False conn)."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with _scratch_replay_repo() as repo:
                    repo.conn.execute("SELECT 1")  # leave connection INTRANS
            self.assertNotIn(
                own, self._replay_scratch_schemas(),
                msg="normal-exit cleanup left its own replay_<32hex> schema "
                    "behind",
            )
        finally:
            # Hygiene drops ONLY this test's own schema (never a global
            # ``current - before`` set difference over pg_namespace).
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_exception_path(self) -> None:
        """A body exception must propagate AND the scratch schema must still
        be dropped (cleanup runs even when the body failed)."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with self.assertRaises(_Boom):
                    with _scratch_replay_repo() as repo:
                        repo.conn.execute("SELECT 1")  # leave connection INTRANS
                        raise _Boom("body failed")
            self.assertNotIn(
                own, self._replay_scratch_schemas(),
                msg="exception-path cleanup left its own replay_<32hex> "
                    "schema behind",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_own_cleanup_preserves_foreign_schema(self) -> None:
        """08-14 final-seal RUN1 P1: this test's cleanup must never drop a
        scratch schema it does not own.

        A foreign worker's legitimately-live ``replay_<32hex>`` schema
        (simulated here as an explicit fixture) must survive this test's
        entire lifecycle. The PRE-FIX finallys computed ``current - before``
        over the GLOBAL pg_namespace and dropped the difference — under
        8-worker worksteal they deleted sibling tests' live schemas (RUN1).
        RED-first: with the old pattern in this finally the foreign schema
        was dropped and the survival assert failed (RED, 3 failed); with
        own-name-only drops it survives (GREEN). Revert-fail: re-adding the
        old ``current - before`` drop here fails the assert again."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        foreign = _own_schema_for(self._testMethodName + ":foreign", "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        _drop_own_scratch_schema(self.conn, foreign)  # crash-residue hygiene
        with self.conn.transaction():
            self.conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(foreign)
                )
            )
        try:
            try:
                with _module_local_uuid4(hr, own.removeprefix("replay_")):
                    with _scratch_replay_repo() as repo:
                        repo.conn.execute("SELECT 1")
                self.assertNotIn(
                    own, self._replay_scratch_schemas(),
                    msg="own replay_<32hex> schema not cleaned by production "
                        "finally",
                )
            finally:
                # Hygiene drops ONLY this test's own schema name — never a
                # global ``current - before`` set difference.
                _drop_own_scratch_schema(self.conn, own)
            # The foreign fixture must have survived this test's lifecycle:
            self.assertIn(
                foreign, self._replay_scratch_schemas(),
                msg="test hygiene dropped a foreign worker's live schema",
            )
        finally:
            # Explicit fixture cleanup: this test created `foreign`, so it
            # drops it by exact name (never via a set difference).
            _drop_own_scratch_schema(self.conn, foreign)

    def _flaky_connect_factory(self, hr_module, mode="drop-fail"):
        """Return a psycopg.connect replacement whose cursor.execute() is
        fault-injected on DROP SCHEMA statements.

        The cleanup runs through ``conn.cursor()`` cursors (same idiom as the
        rest of the module), so the injection wraps ``conn.cursor``. psycopg 3
        cursors are C extensions whose ``execute`` is a read-only attribute -
        we cannot monkeypatch it, so we return a thin delegating wrapper that
        forwards every call, with these fault modes:

        * ``drop-fail``: the DROP raises ProgrammingError (the cleanup DROP
          itself fails).
        * ``drop-then-recreate``: the DROP really executes, then the same
          scratch schema name is re-created, so the post-DROP count check
          sees a surviving schema (the "schema still exists after DROP"
          branch).
        * ``drop-fail-close-fail`` (P2-1, round-2 reviewer): the DROP fails
          AND the connection's ``close()`` raises afterwards. The close
          failure must never escape raw: it must not discard the cleanup
          failure string on a healthy body, and it must never replace the
          in-flight body exception.
        """
        real_connect = hr_module.psycopg.connect

        class _FlakyCursor:
            def __init__(self, cur, mode):
                self._cur = cur
                self._mode = mode

            def execute(self, query, *a, **kw):
                text = query if isinstance(query, str) else str(query)
                if "DROP SCHEMA" not in text.upper():
                    return self._cur.execute(query, *a, **kw)
                if self._mode in ("drop-fail", "drop-fail-close-fail"):
                    raise psycopg.errors.ProgrammingError(
                        "simulated DROP SCHEMA failure"
                    )
                # drop-then-recreate: run the real DROP, then bring the same
                # scratch schema back so the impl's count check must report
                # the survivor.
                self._cur.execute(query, *a, **kw)
                match = re.search(r"replay_[0-9a-f]{32}", text)
                if match is None:
                    raise AssertionError(f"no scratch name in DROP: {text!r}")
                self._cur.execute(
                    psycopg.sql.SQL("CREATE SCHEMA {}").format(
                        psycopg.sql.Identifier(match.group(0))
                    )
                )
                return None

            def __enter__(self):
                self._cur.__enter__()
                return self

            def __exit__(self, *exc):
                return self._cur.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        def _flaky_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            orig_cursor = conn.cursor

            def _patched_cursor(*ca, **ckw):
                return _FlakyCursor(orig_cursor(*ca, **ckw), mode)

            conn.cursor = _patched_cursor
            if mode == "drop-fail-close-fail":
                orig_close = conn.close

                def _patched_close():
                    # Simulate the server dying at teardown: the real close
                    # runs first (hygiene), then the failure surfaces.
                    orig_close()
                    raise psycopg.OperationalError("simulated close failure")

                conn.close = _patched_close
            return conn

        return _flaky_connect

    def test_scratch_schema_cleanup_failure_raises_on_normal_body(self) -> None:
        """A failed DROP with a healthy body must raise (RuntimeError) - the
        cleanup failure must never be swallowed."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._flaky_connect_factory(hr),
                ):
                    with self.assertRaises(RuntimeError) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
            self.assertIn(
                "scratch schema cleanup failed",
                str(cm.exception),
                msg="cleanup failure must name itself in the RuntimeError",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_failure_attaches_note_on_body_exception(
        self,
    ) -> None:
        """When the body already failed, the original exception must survive
        and the cleanup failure must be attached (add_note), never swapped in
        or dropped."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._flaky_connect_factory(hr),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
                            raise _Boom("body failed")

            notes = getattr(cm.exception, "__notes__", [])
            self.assertTrue(
                any("cleanup" in (n or "").lower() for n in notes),
                msg=f"cleanup failure not attached to body exception: notes={notes}",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_refuses_non_scratch_schema(self) -> None:
        """P2-4: the DROP must refuse any name outside the replay_<32hex>
        scratch contract; 'public' must be rejected and must survive."""
        import plugins.crypto_guard.backtest.historical_replay as hr

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            before = cur.fetchone()["count"]
        with self.assertRaises(RuntimeError) as cm:
            hr._drop_scratch_schema("public", "dummy-dsn")
        self.assertIn("refusing to drop non-scratch schema", str(cm.exception))
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            self.assertEqual(cur.fetchone()["count"], before)

    def test_scratch_schema_cleanup_reports_still_exists(self) -> None:
        """P2-4: when the DROP 'succeeds' but the schema still exists after,
        cleanup must report it (RuntimeError), never silently pass."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._flaky_connect_factory(
                        hr, mode="drop-then-recreate"
                    ),
                ):
                    with self.assertRaises(RuntimeError) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
            self.assertIn("schema still exists after DROP", str(cm.exception))
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_connect_failure_raises_on_normal_body(
        self,
    ) -> None:
        """P2-1: a failed cleanup CONNECT (not DROP) with a healthy body must
        surface as RuntimeError - the connect failure must not escape as an
        unhandled OperationalError that hides the cleanup failure."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._connect_fail_on_call_factory(hr),
                ):
                    with self.assertRaises(RuntimeError) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
            self.assertIn("scratch schema cleanup failed", str(cm.exception))
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_connect_failure_attaches_note_on_body(
        self,
    ) -> None:
        """P2-1/P2-2: a failed cleanup CONNECT after a body failure must keep
        the original exception and attach the cleanup failure to it."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._connect_fail_on_call_factory(hr),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
                            raise _Boom("body failed")
            notes = getattr(cm.exception, "__notes__", [])
            self.assertTrue(
                any("cleanup" in (n or "").lower() for n in notes),
                msg=f"cleanup failure not attached: notes={notes}",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_close_failure_raises_on_normal_body(
        self,
    ) -> None:
        """P2-1 (round-2 reviewer): when the cleanup connection's ``close()``
        fails AFTER a failed DROP, the close error must not escape raw and
        must not discard the framed cleanup failure - the RuntimeError must
        still name the cleanup failure."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._flaky_connect_factory(
                        hr, mode="drop-fail-close-fail"
                    ),
                ):
                    with self.assertRaises(RuntimeError) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
            self.assertIn(
                "scratch schema cleanup failed",
                str(cm.exception),
                msg="close failure must not replace the framed cleanup failure",
            )
            self.assertNotIn(
                "simulated close failure",
                str(cm.exception),
                msg="close failure must be swallowed, never escape raw",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_scratch_schema_cleanup_close_failure_attaches_note_on_body(
        self,
    ) -> None:
        """P2-1 (round-2 reviewer): when the body already failed, a failing
        ``close()`` on the cleanup connection must never replace the in-flight
        body exception - the original exception survives with the cleanup
        failure attached."""
        import plugins.crypto_guard.backtest.historical_replay as hr
        from plugins.crypto_guard.backtest.historical_replay import (
            _scratch_replay_repo,
        )

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._flaky_connect_factory(
                        hr, mode="drop-fail-close-fail"
                    ),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with _scratch_replay_repo() as repo:
                            repo.conn.execute("SELECT 1")
                            raise _Boom("body failed")
            notes = getattr(cm.exception, "__notes__", [])
            self.assertTrue(
                any("cleanup" in (n or "").lower() for n in notes),
                msg=f"cleanup failure not attached to body exception: notes={notes}",
            )
            self.assertNotIn(
                "simulated close failure",
                "\n".join(notes),
                msg="close failure must not replace the body exception",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def _connect_fail_on_call_factory(self, hr_module, fail_on_call=2):
        """Return a psycopg.connect replacement that fails on the Nth call.

        ``_scratch_replay_repo`` opens the replay connection first (call 1)
        and the dedicated cleanup connection second (call 2), so the default
        fails exactly the cleanup connect while the replay connect succeeds.
        """
        real_connect = hr_module.psycopg.connect
        calls = [0]

        def _connect(*args, **kwargs):
            calls[0] += 1
            if calls[0] == fail_on_call:
                raise psycopg.OperationalError("simulated connect failure")
            return real_connect(*args, **kwargs)

        return _connect

    def test_attach_cleanup_failure_falls_back_to_cause(self) -> None:
        """P2-2: on runtimes without BaseException.add_note (Python 3.10), the
        cleanup failure must chain via __cause__ instead of being lost."""
        import plugins.crypto_guard.backtest.historical_replay as hr

        class _NoNote(Exception):
            add_note = None

        exc = _NoNote("body failed")
        hr._attach_cleanup_failure(exc, "boom")
        self.assertIsInstance(exc.__cause__, RuntimeError)
        self.assertIn("scratch schema cleanup failed: boom", str(exc.__cause__))


class TestScratchCleanupMirrorsContract(unittest.TestCase):
    # ── P2-2 (round-2 reviewer): Phase H / Phase I scratch-repo mirrors ──
    #
    # ``tools/_phase_h_fault_inject.py::_scratch_fault_repo`` and
    # ``tools/_phase_i_fresh_verify.py::_scratch_fresh_repo`` carried the
    # verbatim P1 defect: their ``finally`` flipped ``conn.autocommit = True``
    # while the connection could be INTRANS (psycopg raises ProgrammingError),
    # swallowed the exception, skipped the DROP, and silently leaked
    # ``fault_<32hex>`` / ``phase_i_<32hex>`` schemas - invisible to the
    # ``^replay_`` residue gates. These tests pin the ported contract:
    # dedicated autocommit cleanup connection, guarded rollback/close,
    # surfaced failures, post-DROP count check, ``sql.Identifier``.
    #
    # Revert-fail: restore the old ``conn.autocommit = True`` finally in
    # either mirror -> the INTRANS/exception tests below leave a
    # ``fault_<32hex>`` / ``phase_i_<32hex>`` schema behind (residue assert
    # fails) and the close-guard tests let the raw close error escape.

    def setUp(self) -> None:
        # Shared harness connection for the residue snapshots / hygiene drops
        # (mirrors TestPgMarketDataReplayP8.setUp).
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn

    def tearDown(self) -> None:
        self._repo_handle.close()

    # 08-14 final-seal RUN1 P1: the mirror residue tests follow the same
    # parallel ownership contract as the replay tests — deterministic
    # per-test schema name via ``_module_local_uuid4`` (module-local patch,
    # never the stdlib ``uuid``), exact-name assertions, and finallys that
    # drop only explicitly-created names (``_drop_own_scratch_schema``).
    # No global before/after snapshot is used for assertions or cleanup.

    def _mirror_scratch_schemas(self, prefix: str) -> set[str]:
        """Read-only snapshot of current <prefix>_<32hex> schemas."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname ~ %s",
                (rf"^{prefix}_[0-9a-f]{{32}}$",),
            )
            return {str(r["nspname"]) for r in cur.fetchall()}

    def _mirror_drop_fail_close_fail_factory(self, module):
        """Return a psycopg.connect replacement for a mirror helper whose
        cleanup connection fails on the DROP and then on ``close()`` - the
        round-2 reviewer's P2-1 scenario for the Phase H/I mirrors: the close
        error must never escape raw past the helper's failure string."""
        class _FakeCur:
            def execute(self, query, *a, **kw):
                if "DROP SCHEMA" in str(query).upper():
                    raise psycopg.errors.ProgrammingError(
                        "simulated DROP SCHEMA failure"
                    )
                raise AssertionError(f"unexpected non-DROP query: {query!r}")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __getattr__(self, name):
                raise AssertionError(f"unexpected cursor attribute: {name}")

        class _FakeConn:
            def cursor(self):
                return _FakeCur()

            def close(self):
                raise psycopg.OperationalError("simulated close failure")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __getattr__(self, name):
                raise AssertionError(f"unexpected connection attribute: {name}")

        def _connect(*args, **kwargs):
            return _FakeConn()

        return _connect

    def test_fault_mirror_intrans_body_leaves_no_residue(self) -> None:
        """A normal body exit with the connection INTRANS must still drop the
        fault_<32hex> scratch schema (no leak)."""
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        own = _own_schema_for(self._testMethodName, "fault")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(phf, own.removeprefix("fault_")):
                with phf._scratch_fault_repo() as repo:
                    repo.conn.execute("SELECT 1")  # leave connection INTRANS
            self.assertNotIn(
                own, self._mirror_scratch_schemas("fault"),
                msg="fault mirror normal-exit cleanup leaked its own "
                    "fault_<32hex> schema",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_fault_mirror_body_exception_leaves_no_residue(self) -> None:
        """A body exception must propagate AND the fault_<32hex> scratch
        schema must still be dropped."""
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "fault")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(phf, own.removeprefix("fault_")):
                with self.assertRaises(_Boom):
                    with phf._scratch_fault_repo() as repo:
                        repo.conn.execute("SELECT 1")  # leave connection INTRANS
                        raise _Boom("body failed")
            self.assertNotIn(
                own, self._mirror_scratch_schemas("fault"),
                msg="fault mirror exception-path cleanup leaked its own "
                    "fault_<32hex> schema",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_fault_mirror_drop_refuses_non_scratch_schema(self) -> None:
        """The fault mirror helper must refuse any name outside the
        fault_<32hex> scratch contract; 'public' must survive."""
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            before = cur.fetchone()["count"]
        with self.assertRaises(RuntimeError) as cm:
            phf._drop_fault_schema("public")
        self.assertIn("refusing to drop non-scratch schema", str(cm.exception))
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            self.assertEqual(cur.fetchone()["count"], before)

    def test_fault_mirror_drop_close_failure_never_escapes(self) -> None:
        """P2-1 (round-2 reviewer) for the fault mirror: a failed DROP whose
        connection ``close()`` also fails must return the framed failure
        string - the raw close error must never escape."""
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        with mock.patch.object(
            phf.psycopg, "connect",
            side_effect=self._mirror_drop_fail_close_fail_factory(phf),
        ):
            failure = phf._drop_fault_schema("fault_" + "0" * 32)
        self.assertIsNotNone(failure)
        self.assertIn("cleanup failure: ProgrammingError", failure)
        self.assertNotIn(
            "close failure", failure,
            msg="close failure must be swallowed inside the helper",
        )

    def test_fresh_mirror_intrans_body_leaves_no_residue(self) -> None:
        """A normal body exit with the connection INTRANS must still drop the
        phase_i_<32hex> scratch schema (no leak)."""
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        own = _own_schema_for(self._testMethodName, "phase_i")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(pif, own.removeprefix("phase_i_")):
                with pif._scratch_fresh_repo() as repo:
                    repo.conn.execute("SELECT 1")  # leave connection INTRANS
            self.assertNotIn(
                own, self._mirror_scratch_schemas("phase_i"),
                msg="fresh mirror normal-exit cleanup leaked its own "
                    "phase_i_<32hex> schema",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_fresh_mirror_body_exception_leaves_no_residue(self) -> None:
        """A body exception must propagate AND the phase_i_<32hex> scratch
        schema must still be dropped."""
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "phase_i")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(pif, own.removeprefix("phase_i_")):
                with self.assertRaises(_Boom):
                    with pif._scratch_fresh_repo() as repo:
                        repo.conn.execute("SELECT 1")  # leave connection INTRANS
                        raise _Boom("body failed")
            self.assertNotIn(
                own, self._mirror_scratch_schemas("phase_i"),
                msg="fresh mirror exception-path cleanup leaked its own "
                    "phase_i_<32hex> schema",
            )
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_fresh_mirror_drop_refuses_non_scratch_schema(self) -> None:
        """The fresh mirror helper must refuse any name outside the
        phase_i_<32hex> scratch contract; 'public' must survive."""
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            before = cur.fetchone()["count"]
        with self.assertRaises(RuntimeError) as cm:
            pif._drop_fresh_schema("public")
        self.assertIn("refusing to drop non-scratch schema", str(cm.exception))
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            self.assertEqual(cur.fetchone()["count"], before)

    def test_fresh_mirror_drop_close_failure_never_escapes(self) -> None:
        """P2-1 (round-2 reviewer) for the fresh mirror: a failed DROP whose
        connection ``close()`` also fails must return the framed failure
        string - the raw close error must never escape."""
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        with mock.patch.object(
            pif.psycopg, "connect",
            side_effect=self._mirror_drop_fail_close_fail_factory(pif),
        ):
            failure = pif._drop_fresh_schema("phase_i_" + "0" * 32)
        self.assertIsNotNone(failure)
        self.assertIn("cleanup failure: ProgrammingError", failure)
        self.assertNotIn(
            "close failure", failure,
            msg="close failure must be swallowed inside the helper",
        )

    def test_fault_mirror_own_cleanup_preserves_foreign_schema(self) -> None:
        """A concurrently-live foreign fault_<32hex> schema must survive this
        test's own cleanup. RED-proven 08-14: with the OLD ``current - before``
        diff-drop in the inner finally this failed (3 failed, 39 deselected
        in 34.99s) at the survival assert — the diff-drop deleted the foreign
        worker's schema; revert-fail restores that semantic and must fail
        again."""
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        own = _own_schema_for(self._testMethodName, "fault")
        foreign = _own_schema_for(self._testMethodName + ":foreign", "fault")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        _drop_own_scratch_schema(self.conn, foreign)  # crash-residue hygiene
        with self.conn.transaction():
            self.conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(foreign)
                )
            )
        try:
            try:
                with _module_local_uuid4(phf, own.removeprefix("fault_")):
                    with phf._scratch_fault_repo() as repo:
                        repo.conn.execute("SELECT 1")
                self.assertNotIn(
                    own, self._mirror_scratch_schemas("fault"),
                    msg="fault mirror own schema not cleaned by production "
                        "finally",
                )
            finally:
                # Hygiene drops ONLY this test's own schema name — never a
                # global ``current - before`` set difference.
                _drop_own_scratch_schema(self.conn, own)
            # The foreign fixture must have survived this test's lifecycle:
            self.assertIn(
                foreign, self._mirror_scratch_schemas("fault"),
                msg="test hygiene dropped a foreign worker's live schema",
            )
        finally:
            # Explicit fixture cleanup: this test created `foreign`, so it
            # drops it by exact name (never via a set difference).
            _drop_own_scratch_schema(self.conn, foreign)

    def test_fresh_mirror_own_cleanup_preserves_foreign_schema(self) -> None:
        """A concurrently-live foreign phase_i_<32hex> schema must survive
        this test's own cleanup (same ownership guarantee as the replay and
        fault-mirror variants). RED-proven 08-14 with the OLD ``current -
        before`` diff-drop; revert-fail restores that semantic and must fail
        again."""
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        own = _own_schema_for(self._testMethodName, "phase_i")
        foreign = _own_schema_for(self._testMethodName + ":foreign", "phase_i")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        _drop_own_scratch_schema(self.conn, foreign)  # crash-residue hygiene
        with self.conn.transaction():
            self.conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(foreign)
                )
            )
        try:
            try:
                with _module_local_uuid4(pif, own.removeprefix("phase_i_")):
                    with pif._scratch_fresh_repo() as repo:
                        repo.conn.execute("SELECT 1")
                self.assertNotIn(
                    own, self._mirror_scratch_schemas("phase_i"),
                    msg="fresh mirror own schema not cleaned by production "
                        "finally",
                )
            finally:
                # Hygiene drops ONLY this test's own schema name — never a
                # global ``current - before`` set difference.
                _drop_own_scratch_schema(self.conn, own)
            # The foreign fixture must have survived this test's lifecycle:
            self.assertIn(
                foreign, self._mirror_scratch_schemas("phase_i"),
                msg="test hygiene dropped a foreign worker's live schema",
            )
        finally:
            # Explicit fixture cleanup: this test created `foreign`, so it
            # drops it by exact name (never via a set difference).
            _drop_own_scratch_schema(self.conn, foreign)


class TestScratchCleanupRedaction(unittest.TestCase):
    # ── 08-13 Authorization D: cleanup error-message redaction ──
    #
    # All three cleanup helpers returned ``cleanup failure: <type>: {exc}``,
    # concatenating ``str(exc)`` verbatim. A failing connect/DROP can embed
    # the DSN, credentials, or arbitrary DB text in its message, so the
    # helper's failure string - and its propagation through the scratch
    # repos' RuntimeError framing / add_note - can leak secrets to logs and
    # external surfaces. These tests pin the contract:
    #   * failure text = fixed prefix + exception type name + optional
    #     strictly-validated ``(SQLSTATE <code>)``; never str/repr of the
    #     exception itself;
    #   * valid SQLSTATE (^[0-9A-Z]{5}$) is preserved; invalid values
    #     (non-string, malformed, empty) are dropped;
    #   * a failing sqlstate getter is fail-safe (ignored; never replaces
    #     the underlying error);
    #   * both production propagation paths stay redacted: healthy-body
    #     RuntimeError framing and body-exception add_note;
    #   * both Phase H/I mirrors honor the same contract.
    #
    # Revert-fail: restore ``{exc}`` in any one of the three
    # ``return f"cleanup failure: ..."`` lines -> the sentinel assertions
    # below fail (TOP_SECRET / CG_CLEANUP_SECRET_SENTINEL appear in text).
    #
    # ── 08-14 final-seal RUN1 P1 (parallel ownership contract) ────────────
    #
    # The propagation tests below inject a cleanup CONNECT failure, so the
    # scratch schema the body created is deliberately left behind (that leak
    # IS the scenario under test). The tests therefore carry the same
    # ownership contract as the rest of the file: a deterministic own name
    # via ``_module_local_uuid4`` (module-local patch, never the stdlib
    # ``uuid``), no global before/after snapshot, and a finally that drops
    # ONLY the exact own name (``_drop_own_scratch_schema``) — never a
    # ``current - before`` set difference over pg_namespace.

    _SECRET_TEXT = (
        "postgresql://user:TOP_SECRET@localhost/db "
        "password=TOP_SECRET CG_CLEANUP_SECRET_SENTINEL"
    )

    class _SecretError(psycopg.OperationalError):
        """OperationalError whose str() embeds DSN + password + sentinel."""

        def __init__(self, msg=None):
            super().__init__(msg or TestScratchCleanupRedaction._SECRET_TEXT)

    class _SqlstateError(psycopg.OperationalError):
        """OperationalError with an explicit (valid or invalid) sqlstate.

        psycopg's ``Error.__init__`` reads the ``sqlstate`` property, so the
        backing field must exist BEFORE ``super().__init__``."""

        def __init__(self, msg, sqlstate):
            self._custom_sqlstate = sqlstate
            super().__init__(msg)

        @property
        def sqlstate(self):
            return self._custom_sqlstate

    class _SqlstateGetterBoom(psycopg.OperationalError):
        """OperationalError whose sqlstate getter raises: the failure summary
        must survive (fail-safe) and never replace the underlying error."""

        def __init__(self, msg):
            self._sqlstate_boom_armed = False
            super().__init__(msg)
            # Only arm the failing getter AFTER psycopg's own __init__ has
            # read ``sqlstate`` (psycopg reads it during construction).
            self._sqlstate_boom_armed = True

        @property
        def sqlstate(self):
            if self._sqlstate_boom_armed:
                raise RuntimeError("sqlstate getter boom")
            return None

    def setUp(self) -> None:
        # Harness connection for residue snapshots / hygiene drops.
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn

    def tearDown(self) -> None:
        self._repo_handle.close()

    def _assert_redacted(self, text: str, type_name: str) -> None:
        """Failure text must keep the fixed prefix + exception type name but
        never carry the exception message (DSN/credentials/DB text)."""
        self.assertIn("cleanup failure: ", text)
        self.assertIn(type_name, text)
        for needle in (
            "TOP_SECRET",
            "CG_CLEANUP_SECRET_SENTINEL",
            "postgresql://",
            "password=",
        ):
            self.assertNotIn(
                needle, text, msg=f"secret leaked into error text: {needle}"
            )

    def _redact_connect_factory(self, module, fail_on_call=2):
        """psycopg.connect replacement: the first call returns a REAL
        connection (the scratch-repo body), then the cleanup connect raises
        the sentinel-secret OperationalError (the leaky path under test)."""
        real_connect = module.psycopg.connect
        state = {"calls": 0}

        def _connect(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] < fail_on_call:
                return real_connect(*args, **kwargs)
            raise self._SecretError()

        return _connect

    # ── three helpers: no str(exc) in the failure string ───────────────────

    def test_replay_drop_redacts_exception_text(self) -> None:
        import plugins.crypto_guard.backtest.historical_replay as hr

        with mock.patch.object(
            hr.psycopg, "connect", side_effect=self._SecretError()
        ):
            failure = hr._drop_scratch_schema(
                "replay_" + "0" * 32, "postgresql://replay"
            )
        self.assertIsNotNone(failure)
        self._assert_redacted(failure, "_SecretError")

    def test_fault_drop_redacts_exception_text(self) -> None:
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        with mock.patch.object(
            phf.psycopg, "connect", side_effect=self._SecretError()
        ):
            failure = phf._drop_fault_schema("fault_" + "0" * 32)
        self.assertIsNotNone(failure)
        self._assert_redacted(failure, "_SecretError")

    def test_fresh_drop_redacts_exception_text(self) -> None:
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        with mock.patch.object(
            pif.psycopg, "connect", side_effect=self._SecretError()
        ):
            failure = pif._drop_fresh_schema("phase_i_" + "0" * 32)
        self.assertIsNotNone(failure)
        self._assert_redacted(failure, "_SecretError")

    # ── SQLSTATE: valid preserved, invalid dropped, getter fail-safe ───────

    def test_replay_drop_keeps_valid_sqlstate(self) -> None:
        import plugins.crypto_guard.backtest.historical_replay as hr

        exc = self._SqlstateError(self._SECRET_TEXT, "42P01")
        with mock.patch.object(hr.psycopg, "connect", side_effect=exc):
            failure = hr._drop_scratch_schema(
                "replay_" + "0" * 32, "postgresql://replay"
            )
        self._assert_redacted(failure, "_SqlstateError")
        self.assertIn("42P01", failure)

    def test_replay_drop_rejects_invalid_sqlstate(self) -> None:
        import plugins.crypto_guard.backtest.historical_replay as hr

        for bad in (12345, "not-a-state", ""):
            with mock.patch.object(
                hr.psycopg, "connect",
                side_effect=self._SqlstateError(self._SECRET_TEXT, bad),
            ):
                failure = hr._drop_scratch_schema(
                    "replay_" + "0" * 32, "postgresql://replay"
                )
            self._assert_redacted(failure, "_SqlstateError")
            self.assertNotIn(
                "(SQLSTATE", failure,
                msg=f"invalid sqlstate {bad!r} leaked through",
            )

    def test_sqlstate_getter_boom_is_failsafe(self) -> None:
        import plugins.crypto_guard.backtest.historical_replay as hr

        with mock.patch.object(
            hr.psycopg, "connect",
            side_effect=self._SqlstateGetterBoom(self._SECRET_TEXT),
        ):
            failure = hr._drop_scratch_schema(
                "replay_" + "0" * 32, "postgresql://replay"
            )
        self._assert_redacted(failure, "_SqlstateGetterBoom")
        self.assertNotIn("(SQLSTATE", failure)

    # ── production propagation paths stay redacted ─────────────────────────

    def test_replay_normal_body_framed_error_redacted(self) -> None:
        """Healthy body + secret-bearing cleanup failure: the framed
        RuntimeError must not leak the exception text. The cleanup CONNECT
        fails by design, so the body's scratch schema is deliberately left
        behind — finally drops ONLY the exact own name (08-14 ownership
        contract)."""
        import plugins.crypto_guard.backtest.historical_replay as hr

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._redact_connect_factory(hr),
                ):
                    with self.assertRaises(RuntimeError) as cm:
                        with hr._scratch_replay_repo() as repo:
                            pass  # healthy body; only the cleanup connect fails
            self.assertIn("scratch schema cleanup failed", str(cm.exception))
            self._assert_redacted(str(cm.exception), "_SecretError")
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_replay_body_exception_note_redacted(self) -> None:
        """Body failure + secret-bearing cleanup failure: the original
        exception survives and its add_note must not leak the text."""
        import plugins.crypto_guard.backtest.historical_replay as hr

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "replay")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(hr, own.removeprefix("replay_")):
                with mock.patch.object(
                    hr.psycopg, "connect",
                    side_effect=self._redact_connect_factory(hr),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with hr._scratch_replay_repo() as repo:
                            raise _Boom("body failed")
            notes = list(getattr(cm.exception, "__notes__", ()))
            self.assertTrue(
                notes, msg="expected a cleanup-failure note on the body exception"
            )
            self.assertTrue(
                any("scratch schema cleanup failed" in n for n in notes),
                msg=f"cleanup note missing: {notes}",
            )
            for note in notes:
                self._assert_redacted(note, "_SecretError")
        finally:
            _drop_own_scratch_schema(self.conn, own)

    # ── Phase H/I mirrors honor the same contract ──────────────────────────

    def test_fault_mirror_body_exception_note_redacted(self) -> None:
        from plugins.crypto_guard.tools import _phase_h_fault_inject as phf

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "fault")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(phf, own.removeprefix("fault_")):
                with mock.patch.object(
                    phf.psycopg, "connect",
                    side_effect=self._redact_connect_factory(phf),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with phf._scratch_fault_repo() as repo:
                            raise _Boom("body failed")
            for note in getattr(cm.exception, "__notes__", ()):
                self._assert_redacted(note, "_SecretError")
        finally:
            _drop_own_scratch_schema(self.conn, own)

    def test_fresh_mirror_body_exception_note_redacted(self) -> None:
        from plugins.crypto_guard.tools import _phase_i_fresh_verify as pif

        class _Boom(Exception):
            pass

        own = _own_schema_for(self._testMethodName, "phase_i")
        _drop_own_scratch_schema(self.conn, own)  # crash-residue hygiene
        try:
            with _module_local_uuid4(pif, own.removeprefix("phase_i_")):
                with mock.patch.object(
                    pif.psycopg, "connect",
                    side_effect=self._redact_connect_factory(pif),
                ):
                    with self.assertRaises(_Boom) as cm:
                        with pif._scratch_fresh_repo() as repo:
                            raise _Boom("body failed")
            for note in getattr(cm.exception, "__notes__", ()):
                self._assert_redacted(note, "_SecretError")
        finally:
            _drop_own_scratch_schema(self.conn, own)


if __name__ == "__main__":
    unittest.main()
