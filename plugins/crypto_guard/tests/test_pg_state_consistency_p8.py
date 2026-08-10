"""P8-3 gate: state_consistency diagnostics on real PostgreSQL.

Guards the SQLite->PG cutover of ``diagnostics/state_consistency.py``. The old
code used SQLite-dialect constructs that BREAK on psycopg3 / PostgreSQL:

  SC-1: ``datetime(replace(replace(gd.analysis_time_utc, 'T',' '), 'Z',''))
        >= datetime(?)`` wrappers are invalid PG syntax on a TEXT
        ISO-8601 column; the migrated form casts
        ``analysis_time_utc::timestamptz >= %s::timestamptz``. If left, every
        market-data-contract window query raises -> those checks silently return
        ``[]`` (false green: "no issues").
  SC-2: ``json_extract(event_json, '$.mark_price')`` is a SQLite JSON function
        with no PG equivalent; the migrated form is ``event_json ->> 'mark_price'``.
        If left, the financial-action checks raise -> return ``[]``.
  SC-3: ``is_closed = 1`` raises "operator does not exist: boolean = integer"
        on PG; the migrated form is ``is_closed = TRUE``.
  SC-4: JSONB columns come back from psycopg as already-decoded dict/list (NOT
        str). ``json.loads(row["event_json"])`` raises TypeError inside the
        per-row ``except ...: continue`` blocks -> every row is skipped -> the
        check reports zero issues even when the defect is present. The migrated
        ``_safe_json(raw, default)`` passes dict/list through and only parses
        str, so JSONB-backed checks actually evaluate their rows.

The contract exercised here (dominant migrated paths, all real-PG):

  FA: a profit-protection financial action with NO mark_price -> surfaces
      ``financial_action_missing_mark_price`` (SC-2 JSONB ``->>`` IS NULL).
  ST: a financial action WITH mark_price but a price_as_of 200s older than
      created_at -> surfaces ``financial_action_stale_price`` (SC-4 _safe_json
      reads the event_json dict, then python compares the timestamps). This is
      the _safe_json false-green guard: if json.loads threw on the dict, the
      row would be skipped and NO stale-price issue would surface.
  FC: a closed candle with a future close_time -> surfaces
      ``market_data_future_candle`` (SC-3 boolean + BIGINT ``close_time > %s``).
  NEG: a financial action WITH a fresh mark_price/price_as_of does NOT surface
      either financial issue (proves the filters are real, not pass-everything).

NOT a mock; uses a real pooled conn on an isolated ``public`` schema.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

import os
import unittest
from datetime import datetime, timedelta, timezone

from plugins.crypto_guard.diagnostics import state_consistency
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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TestPgStateConsistencyP8(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _seed_marker(self, key: str, applied_at: datetime) -> None:
        """Upsert a _migration_state marker so the cutoff helpers resolve."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO _migration_state(key, applied_at) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET applied_at = EXCLUDED.applied_at",
                    (key, applied_at),
                )

    def _seed_financial_log(
        self,
        *,
        event_type: str,
        event_json: dict | None,
        created_at: datetime,
    ) -> int:
        """Insert a paper_trade_logs row; return its id."""
        # JSONB write: pass a JSON string (PG auto-casts str->jsonb). A bare
        # dict cannot be adapted by psycopg3 with the default '%s' format
        # ('cannot adapt type dict'), and the feedback_ttl PG test uses the same
        # json.dumps() pattern, so keep it consistent across the P8-3 tests.
        import json as _json
        ev = _json.dumps(event_json) if event_json is not None else None
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO paper_trade_logs"
                    "(position_id, event_type, symbol, side, price, quantity, "
                    " event_json, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        1, event_type, "BTCUSDT", "long", 50000.0, 0.01,
                        ev,
                        created_at,
                    ),
                )
                return int(cur.fetchone()["id"])

    def _seed_candle(self, *, close_time_ms: int, is_closed: bool) -> None:
        """Insert a candles row with the given close_time (epoch-ms) / is_closed."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO candles"
                    "(symbol, interval, open_time, close_time, open, high, low, "
                    " close, volume, is_closed) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        "BTCUSDT", "1m",
                        close_time_ms - 60_000, close_time_ms,
                        50000.0, 50100.0, 49900.0, 50050.0, 10.0,
                        is_closed,
                    ),
                )

    def _issue_types(self) -> set[str]:
        out = state_consistency.diagnose_state_consistency(self.repo)
        # The real false-green guard: diagnose_state_consistency must run to
        # completion (return a dict with an ``issues`` list) rather than raising
        # a SQL error inside any check. ``ok`` is intentionally False here when
        # error-severity issues were seeded (``ok = len(error_issues) == 0`` at
        # state_consistency.py:221), so asserting ``ok`` would be backwards for
        # the positive tests. The assertIn(...) in each test is what proves the
        # migrated SQL actually evaluated the row instead of raising -> [] .
        self.assertIn("issues", out, out)
        self.assertIsInstance(out["issues"], list, out)
        return {i["type"] for i in out["issues"]}

    # ── SC-2 + SC-4: financial_action_missing_mark_price + _safe_json ───────

    def test_financial_action_missing_mark_price_surfaces(self) -> None:
        """SC-2/SC-4: a profit_protection action with no mark_price is flagged."""
        # The profit-protection cutoff must resolve (marker present).
        self._seed_marker(
            "profit_protection_mark_price_contract_v1",
            datetime.now(timezone.utc) - timedelta(days=1),
        )
        now = datetime.now(timezone.utc)
        # event_json with NO mark_price -> financial_action_missing_mark_price.
        self._seed_financial_log(
            event_type="profit_protection",
            event_json={"reason": "tp_guard"},
            created_at=now,
        )
        # NEG control: WITH mark_price + fresh price_as_of -> NOT flagged.
        fresh = now - timedelta(seconds=10)
        self._seed_financial_log(
            event_type="profit_protection",
            event_json={"mark_price": 50000.0, "price_as_of": _iso(fresh)},
            created_at=now,
        )
        types = self._issue_types()
        self.assertIn("financial_action_missing_mark_price", types)
        # Exactly one missing-mark_price issue (the no-mark_price row only).
        out = state_consistency.diagnose_state_consistency(self.repo)
        n_missing = sum(
            1 for i in out["issues"] if i["type"] == "financial_action_missing_mark_price"
        )
        self.assertEqual(n_missing, 1, out)

    # ── SC-4 false-green guard: stale price via _safe_json dict read ─────────

    def test_financial_action_stale_price_surfaces(self) -> None:
        """SC-4: _safe_json reads the JSONB dict; a 200s-stale price is flagged.

        This is the _safe_json false-green guard. event_json is a JSONB column,
        so psycopg returns it as a dict. If the migration had kept
        ``json.loads(row["event_json"])`` it would raise TypeError inside the
        per-row ``except: continue`` and skip EVERY row -> no stale-price issue
        would ever surface. The migrated ``_safe_json`` passes the dict through,
        so the timestamp comparison actually runs.
        """
        self._seed_marker(
            "profit_protection_mark_price_contract_v1",
            datetime.now(timezone.utc) - timedelta(days=1),
        )
        now = datetime.now(timezone.utc)
        stale_as_of = now - timedelta(seconds=200)
        self._seed_financial_log(
            event_type="profit_protection",
            event_json={"mark_price": 50000.0, "price_as_of": _iso(stale_as_of)},
            created_at=now,
        )
        types = self._issue_types()
        self.assertIn("financial_action_stale_price", types)

    # ── SC-3: boolean + BIGINT future-candle ─────────────────────────────────

    def test_market_data_future_candle_surfaces(self) -> None:
        """SC-3: is_closed=TRUE + close_time in the future is flagged.

        Exercises ``is_closed = TRUE`` (boolean, not ``= 1``) and
        ``close_time > %s`` (BIGINT epoch-ms compared with an int). The
        market-data contract cutoff marker must resolve.
        """
        self._seed_marker(
            "market_data_contract_v1",
            datetime.now(timezone.utc) - timedelta(days=1),
        )
        # Future close_time: now + 5 min, in epoch-ms. utc_ms() is wall-clock.
        future_ms = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp() * 1000)
        self._seed_candle(close_time_ms=future_ms, is_closed=True)
        types = self._issue_types()
        self.assertIn("market_data_future_candle", types)

    def test_future_candle_not_flagged_when_open(self) -> None:
        """NEG control: an OPEN candle (is_closed=FALSE) with future close_time
        is NOT flagged by the future-candle check (which requires is_closed=TRUE)."""
        self._seed_marker(
            "market_data_contract_v1",
            datetime.now(timezone.utc) - timedelta(days=1),
        )
        future_ms = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp() * 1000)
        self._seed_candle(close_time_ms=future_ms, is_closed=False)
        types = self._issue_types()
        self.assertNotIn("market_data_future_candle", types)


if __name__ == "__main__":
    unittest.main()
