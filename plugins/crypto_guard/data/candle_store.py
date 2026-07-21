from __future__ import annotations

import logging
from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_closed_klines
from plugins.crypto_guard.storage.parquet_archive import ParquetKlineArchive
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


logger = logging.getLogger(__name__)


def fetch_and_upsert_closed_klines(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    *,
    analysis_time_utc: int,
    lookback: int,
    required_count: int | None = None,
) -> dict[str, Any]:
    """Fetch closed klines from Binance and upsert into PostgreSQL.

    When ``required_count`` is provided (R2), after the incremental fetch the
    function calls ``compute_missing_ranges`` and ``backfill_symbol_interval``
    to repair any gaps in the contiguous tail. When ``required_count`` is None
    (default), the old single-shot behavior is preserved for backward
    compatibility.
    """
    candles = fetch_closed_klines(symbol, interval, analysis_time_utc=analysis_time_utc, limit=lookback)
    closed = [c for c in candles if c["close_time"] <= analysis_time_utc and c.get("is_closed")]
    count = repo.upsert_candles(closed)
    archive = ParquetKlineArchive().write_closed_klines(closed, repo=repo) if closed else {"ok": True, "results": [], "closed_rows": 0}

    backfill_result: dict[str, Any] | None = None
    if required_count is not None and required_count > 0:
        # R2: gap-aware paged backfill to repair the contiguous tail.
        try:
            from plugins.crypto_guard.data.candle_backfill import backfill_symbol_interval
            backfill_result = backfill_symbol_interval(
                repo, symbol, interval,
                analysis_time_utc=analysis_time_utc,
                required_count=required_count,
            )
        except Exception as exc:
            logger.warning(
                "fetch_and_upsert_closed_klines: backfill failed for %s %s: %s",
                symbol, interval, exc,
            )
            backfill_result = {"error": str(exc)}

    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "upserted": count,
        "analysis_time_utc": analysis_time_utc,
        "parquet_archive": archive,
        "backfill": backfill_result,
    }
