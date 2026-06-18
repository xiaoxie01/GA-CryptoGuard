"""Fetch historical klines from Binance Futures API and persist to SQLite + Parquet.

Usage:
    python -m plugins.crypto_guard.fetch_historical
    # or
    python -c "from plugins.crypto_guard.fetch_historical import main; main()"
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_klines, normalize_symbol
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.parquet_archive import ParquetKlineArchive
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.utils import INTERVAL_MS, utc_ms

LOGGER = get_logger("crypto_guard.fetch_historical")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAJOR_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
MINOR_SYMBOLS = ["ADAUSDT", "AVAXUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT", "XRPUSDT"]
ALL_SYMBOLS = MAJOR_SYMBOLS + MINOR_SYMBOLS

TIMEFRAMES = ["1d", "4h", "1h", "15m"]

# All symbols start from 2025-01-01 (16 months of data)
# Covers: bull market, bear market, ranging, volatile periods
ALL_START_MS = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

# Binance limit per request
BATCH_LIMIT = 1500
# Delay between requests to stay under rate limit (1200 req/min)
REQUEST_DELAY_S = float(os.environ.get("CRYPTO_GUARD_HISTORICAL_DELAY", "0.1"))
# Max retries per batch
MAX_RETRIES = 3


def _start_ms_for_symbol(symbol: str) -> int:
    """Return the start timestamp in ms for a given symbol."""
    return ALL_START_MS


def _fetch_all_klines_for_pair(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Fetch all klines for a symbol+interval pair in batches."""
    all_candles: list[dict[str, Any]] = []
    current_start = start_ms
    batch_num = 0

    while current_start < end_ms:
        batch_num += 1
        batch: list[dict[str, Any]] = []
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                batch = fetch_klines(
                    symbol,
                    interval,
                    start_time=current_start,
                    end_time=end_ms,
                    limit=BATCH_LIMIT,
                )
                break
            except Exception as exc:
                LOGGER.warning(
                    "Batch fetch failed attempt=%d symbol=%s interval=%s start=%d error=%s",
                    attempt, symbol, interval, current_start, exc,
                )
                if attempt == MAX_RETRIES:
                    LOGGER.error(
                        "Giving up batch for %s %s after %d attempts",
                        symbol, interval, MAX_RETRIES,
                    )
                    return all_candles
                time.sleep(1.0)

        if not batch:
            break

        all_candles.extend(batch)

        # Advance start to after the last candle's close_time
        last_open_time = max(int(c["open_time"]) for c in batch)
        current_start = last_open_time + INTERVAL_MS[interval]

        # If we got fewer than BATCH_LIMIT, we've reached the end
        if len(batch) < BATCH_LIMIT:
            break

        time.sleep(REQUEST_DELAY_S)

    return all_candles


def fetch_and_persist_historical(
    repo: CryptoGuardRepository,
    archive: ParquetKlineArchive | None = None,
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch historical klines for all configured symbols/timeframes.

    Persists to SQLite via repo.upsert_candles() and archives to Parquet.
    """
    if archive is None:
        archive = ParquetKlineArchive()

    targets = symbols or ALL_SYMBOLS
    intervals = timeframes or TIMEFRAMES
    end_ms = utc_ms()

    results: list[dict[str, Any]] = []
    total_candles = 0

    for symbol in targets:
        normalized = normalize_symbol(symbol)
        start_ms = _start_ms_for_symbol(symbol)

        for interval in intervals:
            LOGGER.info(
                "Fetching %s %s from %s to now",
                normalized, interval,
                datetime.fromtimestamp(start_ms / 1000, timezone.utc).strftime("%Y-%m-%d"),
            )

            candles = _fetch_all_klines_for_pair(normalized, interval, start_ms, end_ms)

            if not candles:
                LOGGER.warning("No candles fetched for %s %s", normalized, interval)
                results.append({
                    "symbol": normalized,
                    "interval": interval,
                    "candles": 0,
                    "status": "empty",
                })
                continue

            # Historical data goes to Parquet only (not SQLite) to avoid DB bloat
            archive_result = archive.write_closed_klines(candles, repo=repo)
            parquet_ok = archive_result.get("ok", False)
            written = archive_result.get("closed_rows", len(candles))

            total_candles += written
            LOGGER.info("Archived %d candles to Parquet for %s %s", written, normalized, interval)

            results.append({
                "symbol": normalized,
                "interval": interval,
                "candles": written,
                "parquet_ok": parquet_ok,
                "status": "ok",
            })

            time.sleep(REQUEST_DELAY_S)

    return {
        "ok": True,
        "total_candles": total_candles,
        "pairs_processed": len(results),
        "results": results,
    }


def main() -> None:
    """CLI entry point for fetching historical klines."""
    from plugins.crypto_guard.config.loader import load_config
    from plugins.crypto_guard.storage.migrations import initialize_database

    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        archive = ParquetKlineArchive()

        LOGGER.info("Starting historical kline fetch...")
        result = fetch_and_persist_historical(repo, archive)

        LOGGER.info(
            "Fetch complete: %d candles across %d pairs",
            result["total_candles"],
            result["pairs_processed"],
        )

        # Print summary
        for r in result["results"]:
            status_marker = "OK" if r["status"] == "ok" else "EMPTY"
            parquet_marker = "parquet-ok" if r.get("parquet_ok") else "parquet-skip"
            print(f"  {r['symbol']:>10} {r['interval']:>4}  {r['candles']:>6} candles  [{status_marker}] [{parquet_marker}]")

        print(f"\nTotal: {result['total_candles']} candles")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
