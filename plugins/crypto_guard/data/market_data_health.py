"""Market data health assessment — contiguity + freshness gate.

Implements R3 of the market-data-completeness P0 fix. Replaces the loose
row-count heuristic in ``market_state_builder._data_quality`` with a strict
contiguity-and-freshness contract.

Public surface:
    - MARKET_DATA_HEALTH_FIELDS: tuple listing the 14 MarketDataHealth fields.
    - assess_health(repo, symbol, interval, *, analysis_time_utc, required_count) -> dict

The ``assess_health`` function reads only from the DB (it never fetches from
Binance). Backfill is a separate step (see ``candle_backfill.py``).

Reference: .trellis/tasks/07-02-fix-market-data-completeness-p0/prd.md R3.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import INTERVAL_MS, _strict_positive_int_ms, latest_closed_close_time_ms


logger = logging.getLogger(__name__)


# R3: 14-field MarketDataHealth contract.
MARKET_DATA_HEALTH_FIELDS: tuple[str, ...] = (
    "symbol",                # str
    "interval",              # str
    "required_count",        # int
    "total_closed_count",    # int  — all closed rows in DB
    "contiguous_tail_count", # int  — longest contiguous suffix ending at expected_last_close_time
    "missing_ranges",        # list[tuple[int, int]] — (start_open_time, end_open_time) gaps
    "gap_count",             # int  — len(missing_ranges)
    "largest_gap_bars",      # int  — max bars spanned by any single gap
    "first_close_time",      # int | None  — oldest close_time in DB (ms)
    "last_close_time",       # int | None  — newest close_time in DB (ms)
    "expected_last_close_time", # int  — latest_closed_close_time_ms(interval, analysis_time_utc)
    "stale_bars",            # int  — (expected - last) / INTERVAL_MS, 0 when fresh
    "ready",                 # bool — True iff contiguity + freshness + no-future-candle all pass
    "reason",                # str  — "" when ready, else short status code
)

# Slack rows queried beyond required_count so we can detect gaps that fall
# just inside the analysis window without needing a full table scan.
_QUERY_SLACK = 50


def assess_health(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    *,
    analysis_time_utc: int,
    required_count: int,
) -> dict[str, Any]:
    """Assess market data health (contiguity + freshness) for one (symbol, interval).

    Returns a MarketDataHealth dict with the 14 fields listed in
    MARKET_DATA_HEALTH_FIELDS plus the 4-field split keys (total_count,
    loaded_count, contiguous_count, required_count) for snapshot
    ``data_quality.health[tf]``. Reads only from the DB — never fetches from
    Binance. Backfill is a separate step.

    Fail-closed: if the DB query itself fails or analysis_time is invalid,
    returns ``ready=False`` with a descriptive reason and zeroed fields.

    Reference: PRD R3, AC8 (unclosed candle excluded), AC9 (interval boundary),
    AC34 (one-short), AC35 (total-exceeds-but-tail-insufficient),
    AC36 (mid-250-gap), AC39 (no old+new splice).
    """
    # Validate analysis_time_utc — fail-closed on invalid input.
    at_ms = _strict_positive_int_ms(analysis_time_utc)
    if at_ms is None:
        return _health_failure(symbol, interval, required_count, "invalid_analysis_time")

    span = INTERVAL_MS.get(interval)
    if not span:
        return _health_failure(symbol, interval, required_count, "invalid_interval")

    expected_last_close = latest_closed_close_time_ms(interval, at_ms)

    try:
        # 1. Total closed count for (symbol, interval) with close_time <= analysis_time.
        # PG placeholders (%s) + boolean is_closed=TRUE (NOT SQLite ? + is_closed=1).
        total_row = repo.conn.execute(
            "SELECT COUNT(*) AS c FROM candles "
            "WHERE symbol=%s AND interval=%s AND is_closed=TRUE AND close_time <= %s",
            (symbol, interval, at_ms),
        ).fetchone()
        total_closed_count = int(total_row["c"]) if total_row else 0

        if total_closed_count == 0:
            return _health_failure(symbol, interval, required_count, "empty",
                                   expected_last_close_time=expected_last_close,
                                   total_closed_count=0)

        # 2. Fetch the tail rows (newest first) with slack to detect gaps.
        query_limit = int(required_count) + _QUERY_SLACK
        rows = repo.conn.execute(
            "SELECT open_time, close_time, is_closed FROM candles "
            "WHERE symbol=%s AND interval=%s AND is_closed=TRUE AND close_time <= %s "
            "ORDER BY open_time DESC LIMIT %s",
            (symbol, interval, at_ms, query_limit),
        ).fetchall()

        if not rows:
            return _health_failure(symbol, interval, required_count, "empty",
                                   expected_last_close_time=expected_last_close,
                                   total_closed_count=0)

        # 3. Check for future candle: a candle marked is_closed=1 whose
        #    open_time <= analysis_time_utc < close_time. This is a real
        #    integrity violation — the candle should still be forming (its
        #    close_time hasn't passed yet) but is marked closed.
        #    P0-2 fix: the old query ``close_time > analysis_time`` caught
        #    ALL candles later than analysis_time, which in historical replay
        #    legitimately exist in the DB (e.g. DB has the 14:30 candle but
        #    analysis is at 14:15). The new query only catches the candle
        #    that spans the analysis_time boundary (open <= at < close) —
        #    i.e., the currently-forming candle that shouldn't be closed yet.
        #    Fully-closed candles later than analysis_time are legitimate
        #    historical data, not a violation.
        future_check = repo.conn.execute(
            "SELECT COUNT(*) AS c FROM candles "
            "WHERE symbol=%s AND interval=%s AND is_closed=TRUE "
            "AND open_time <= %s AND close_time > %s",
            (symbol, interval, at_ms, at_ms),
        ).fetchone()
        has_future_candle = int(future_check["c"]) > 0 if future_check else False
        if has_future_candle:
            return _health_failure(symbol, interval, required_count, "future_candle",
                                   expected_last_close_time=expected_last_close,
                                   total_closed_count=total_closed_count)

        # 4. Check for duplicate open_time (shouldn't happen due to UNIQUE, but check).
        #    PG (unlike SQLite) does NOT allow a SELECT-list alias (``cnt``) in
        #    HAVING; reference the aggregate ``COUNT(*)`` directly.
        dup_row = repo.conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "  SELECT open_time FROM candles "
            "  WHERE symbol=%s AND interval=%s AND is_closed=TRUE AND close_time <= %s "
            "  GROUP BY open_time HAVING COUNT(*) > 1"
            ")",
            (symbol, interval, at_ms),
        ).fetchone()
        has_duplicate = int(dup_row["c"]) > 0 if dup_row else False
        if has_duplicate:
            return _health_failure(symbol, interval, required_count, "duplicate_open_time",
                                   expected_last_close_time=expected_last_close,
                                   total_closed_count=total_closed_count)

        # 5. Compute contiguous tail from newest backward, stopping at first gap.
        #    rows are sorted open_time DESC, so rows[0] is newest.
        contiguous_tail_count = 1  # at least the newest candle
        for i in range(1, len(rows)):
            cur_open = int(rows[i]["open_time"])
            prev_open = int(rows[i - 1]["open_time"])
            if prev_open - cur_open == span:
                contiguous_tail_count += 1
            else:
                break  # gap found — stop counting (no old+new splicing)

        # 6. Compute missing_ranges (gaps) within the analysis window.
        #    The analysis window is the last `required_count` expected candles.
        #    We scan the full queried rows to find gaps, then filter to those
        #    overlapping the window [expected_last_open, expected_last_close].
        expected_last_open = expected_last_close - span + 1
        window_start_open = expected_last_open - (required_count - 1) * span

        missing_ranges: list[tuple[int, int]] = []
        largest_gap_bars = 0
        # Walk rows from oldest to newest for gap detection.
        sorted_rows = list(reversed(rows))
        for i in range(1, len(sorted_rows)):
            prev_open = int(sorted_rows[i - 1]["open_time"])
            cur_open = int(sorted_rows[i]["open_time"])
            gap = cur_open - prev_open - span
            if gap > 0:
                gap_start = prev_open + span
                gap_end = cur_open - span
                # Only count gaps overlapping the analysis window.
                if gap_end >= window_start_open:
                    missing_ranges.append((gap_start, gap_end))
                    gap_bars = gap // span
                    if gap_bars > largest_gap_bars:
                        largest_gap_bars = gap_bars

        gap_count = len(missing_ranges)

        # 7. Freshness check.
        last_close_time = int(rows[0]["close_time"])
        first_close_time = int(rows[-1]["close_time"])
        stale_bars = (expected_last_close - last_close_time) // span if last_close_time else 0

        # 8. Determine ready status.
        ready = True
        reason = ""
        if contiguous_tail_count < required_count:
            ready = False
            # Distinguish "gapped" (tail has a gap) from "insufficient" (just short).
            if gap_count > 0 and contiguous_tail_count < required_count:
                reason = "gapped"
            else:
                reason = "insufficient"
        elif last_close_time != expected_last_close:
            ready = False
            reason = "stale"
        elif has_future_candle:
            ready = False
            reason = "future_candle"
        elif has_duplicate:
            ready = False
            reason = "duplicate_open_time"

        logger.debug(
            "assess_health: %s %s total=%d contiguous=%d required=%d gaps=%d "
            "largest_gap=%d last_close=%d expected=%d stale=%d ready=%s reason=%s",
            symbol, interval, total_closed_count, contiguous_tail_count, required_count,
            gap_count, largest_gap_bars, last_close_time, expected_last_close,
            stale_bars, ready, reason,
        )

        return {
            "symbol": symbol,
            "interval": interval,
            "required_count": int(required_count),
            "total_closed_count": total_closed_count,
            "contiguous_tail_count": contiguous_tail_count,
            "missing_ranges": missing_ranges,
            "gap_count": gap_count,
            "largest_gap_bars": largest_gap_bars,
            "first_close_time": first_close_time,
            "last_close_time": last_close_time,
            "expected_last_close_time": expected_last_close,
            "stale_bars": int(stale_bars),
            "ready": ready,
            "reason": reason,
            # 4-field split for snapshot.data_quality.health[tf]
            "total_count": total_closed_count,
            "loaded_count": 0,  # set by market_state_builder after reading profile
            "contiguous_count": contiguous_tail_count,
        }
    except Exception as exc:
        logger.warning("assess_health DB query failed for %s %s: %s", symbol, interval, exc)
        return _health_failure(symbol, interval, required_count, "query_error",
                               expected_last_close_time=expected_last_close)


def _health_failure(
    symbol: str,
    interval: str,
    required_count: int,
    reason: str,
    *,
    expected_last_close_time: int | None = None,
    total_closed_count: int = 0,
) -> dict[str, Any]:
    """Build a fail-closed health dict with zeroed fields."""
    return {
        "symbol": symbol,
        "interval": interval,
        "required_count": int(required_count),
        "total_closed_count": total_closed_count,
        "contiguous_tail_count": 0,
        "missing_ranges": [],
        "gap_count": 0,
        "largest_gap_bars": 0,
        "first_close_time": None,
        "last_close_time": None,
        "expected_last_close_time": expected_last_close_time,
        "stale_bars": 0,
        "ready": False,
        "reason": reason,
        # 4-field split
        "total_count": total_closed_count,
        "loaded_count": 0,
        "contiguous_count": 0,
    }
