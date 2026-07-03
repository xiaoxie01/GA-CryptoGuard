"""Gap-aware paged backfill against Binance USD-S-M Futures.

Implements R2 of the market-data-completeness P0 fix. Pages through
``/fapi/v1/klines`` using ``startTime``/``endTime``, deduplicates via
``upsert_candles``, and returns partial progress + ``network_errors`` count
so the caller can retry. Never raises on network error.

Public surface:
    - compute_missing_ranges(repo, symbol, interval, *, analysis_time_utc, required_count) -> list[tuple[int,int]]
    - backfill_symbol_interval(repo, symbol, interval, *, analysis_time_utc, required_count, max_pages=None, progress_cb=None) -> dict

Reference: .trellis/tasks/07-02-fix-market-data-completeness-p0/prd.md R2.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.crypto_guard.data import binance_rest
from plugins.crypto_guard.data.binance_rest import MarketDataError
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import INTERVAL_MS, _strict_positive_int_ms, latest_closed_close_time_ms


logger = logging.getLogger(__name__)


def compute_missing_ranges(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    *,
    analysis_time_utc: int,
    required_count: int,
) -> list[tuple[int, int]]:
    """Return the gaps between the latest ``required_count`` expected candles
    and what's actually contiguous in the DB.

    Each gap is a ``(start_open_time_ms, end_open_time_ms)`` tuple inclusive
    of both endpoints. Returns an empty list when the tail is contiguous.

    Algorithm:
        1. Delegate to ``assess_health`` to get ``missing_ranges`` and
           ``contiguous_tail_count``.
        2. If the tail is already contiguous AND fresh, return ``[]``.
        3. Otherwise, return the gaps from assess_health (these are already
           filtered to the analysis window). Additionally, if the tail is
           short because of a HEAD gap (oldest candles missing within the
           analysis window), include a range from the earliest expected
           open_time to the first available open_time.

    Reference: PRD R2, AC2.
    """
    at_ms = _strict_positive_int_ms(analysis_time_utc)
    if at_ms is None:
        return []

    span = INTERVAL_MS.get(interval)
    if not span:
        return []

    health = assess_health(
        repo, symbol, interval,
        analysis_time_utc=at_ms, required_count=int(required_count),
    )

    # If already ready (contiguous + fresh), no backfill needed.
    if health["ready"]:
        return []

    missing_ranges: list[tuple[int, int]] = list(health.get("missing_ranges") or [])

    # Detect HEAD gap: if the oldest candle in DB is newer than the window
    # start, we need to backfill from the window start to the first available.
    expected_last_close = health.get("expected_last_close_time")
    if expected_last_close is None:
        return missing_ranges

    expected_last_open = int(expected_last_close) - span + 1
    window_start_open = expected_last_open - (int(required_count) - 1) * span

    last_close_time = health.get("last_close_time")

    first_close_time = health.get("first_close_time")
    if first_close_time is not None:
        first_open_time = int(first_close_time) - span + 1
        if first_open_time > window_start_open:
            # There is a head gap from window_start_open to first_open_time - span.
            head_gap_end = first_open_time - span
            if head_gap_end >= window_start_open:
                missing_ranges.append((window_start_open, head_gap_end))
    else:
        # No candles at all — backfill the entire window.
        missing_ranges.append((window_start_open, int(expected_last_close)))

    # Detect TAIL gap: if the newest candle is older than the expected last
    # close (staleness), we need to backfill from last_open + span to
    # expected_last_open. This covers the "stopped for 2 days" downtime
    # scenario where the tail is simply missing, not gapped internally.
    if last_close_time is not None and int(last_close_time) < int(expected_last_close):
        last_open_time = int(last_close_time) - span + 1
        tail_gap_start = last_open_time + span
        tail_gap_end = expected_last_open
        if tail_gap_end >= tail_gap_start:
            missing_ranges.append((tail_gap_start, tail_gap_end))

    # Sort and merge overlapping ranges.
    missing_ranges.sort()
    merged: list[tuple[int, int]] = []
    for r in missing_ranges:
        if merged and r[0] <= merged[-1][1] + span:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
        else:
            merged.append(r)

    return merged


def backfill_symbol_interval(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    *,
    analysis_time_utc: int,
    required_count: int,
    max_pages: int | None = None,
    progress_cb: Any = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Page through Binance ``/fapi/v1/klines`` to fill gaps for one (symbol, interval).

    Returns a BackfillResult dict:
        ``{pages_fetched, candles_upserted, gaps_filled, network_errors,
           resumed_from_page, skipped_due_to_lock}``

    Contract (PRD R2):
        - Uses ``fetch_klines(symbol, interval, start_time=page_start, end_time=page_end, limit=page_limit)``.
        - Skips candles where ``close_time > analysis_time_utc`` (don't write future candles).
        - Skips candles where ``is_closed=False`` (filter before upsert).
        - Handles empty page: advance ``start_time`` by one interval and continue.
        - Handles cross-page duplicates via ``upsert_candles`` ON CONFLICT.
        - Network error: keep already-fetched pages, ``network_errors += 1``, do NOT raise.
        - Reuses ``_throttle_public_request`` (0.25s global) + exponential backoff.
        - Resume: reads/writes ``backfill_progress`` for ``(symbol, interval)``.

    Reference: PRD R2, AC1, AC5, AC6, AC7, AC19.
    """
    at_ms = _strict_positive_int_ms(analysis_time_utc)
    if at_ms is None:
        return _empty_result()

    span = INTERVAL_MS.get(interval)
    if not span:
        return _empty_result()

    # Load backfill config defaults.
    page_limit = 1500
    max_pages_per_run = 50
    try:
        from plugins.crypto_guard.config.loader import load_config
        cfg = load_config()
        md = cfg.market_data
        bf = md.get("backfill") if isinstance(md, dict) else None
        if isinstance(bf, dict):
            page_limit = int(bf.get("page_limit", page_limit))
            max_pages_per_run = int(bf.get("max_pages_per_run", max_pages_per_run))
    except Exception:
        # Config load failure — use defaults.
        pass

    if max_pages is None:
        max_pages = max_pages_per_run
    max_pages = max(1, int(max_pages))

    # Acquire task lock to prevent concurrent backfill on the same (symbol, interval).
    lock_name = f"backfill:{symbol}:{interval}"
    lock_acquired = False
    owner = ""
    try:
        from plugins.crypto_guard.scheduler.task_locks import acquire_lock
        got, owner = acquire_lock(repo, lock_name, ttl_seconds=600)
        if not got:
            # Another worker is already backfilling — skip.
            return {
                "pages_fetched": 0,
                "candles_upserted": 0,
                "gaps_filled": 0,
                "network_errors": 0,
                "resumed_from_page": 0,
                "skipped_due_to_lock": True,
            }
        # P0-7: Commit the lock transaction immediately so the SQLite write
        # lock is released before network requests begin. Without this, the
        # uncommitted INSERT holds the write lock for the entire backfill
        # duration (including network I/O), blocking all other writers.
        try:
            repo.conn.commit()
        except Exception:
            pass
        lock_acquired = True
    except Exception:
        # task_locks not available (e.g. test env without full scheduler) —
        # proceed without lock. This is acceptable for single-worker tests.
        pass

    try:
        return _do_backfill(
            repo, symbol, interval,
            analysis_time_utc=at_ms,
            required_count=int(required_count),
            page_limit=page_limit,
            max_pages=max_pages,
            progress_cb=progress_cb,
            resume=resume,
        )
    finally:
        if lock_acquired:
            try:
                from plugins.crypto_guard.scheduler.task_locks import release_lock
                release_lock(repo, lock_name, owner)
                # P0-7: Commit the release so the lock row is actually deleted.
                try:
                    repo.conn.commit()
                except Exception:
                    pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_result() -> dict[str, Any]:
    return {
        "pages_fetched": 0,
        "candles_upserted": 0,
        "gaps_filled": 0,
        "network_errors": 0,
        "resumed_from_page": 0,
        "skipped_due_to_lock": False,
    }


def _read_backfill_progress(repo: CryptoGuardRepository, symbol: str, interval: str) -> int | None:
    """Read last_open_time_fetched from backfill_progress table.

    Returns None if the table doesn't exist or no row exists.
    """
    try:
        row = repo.conn.execute(
            "SELECT last_open_time_fetched FROM backfill_progress WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        if row and row["last_open_time_fetched"] is not None:
            return int(row["last_open_time_fetched"])
    except Exception:
        pass
    return None


def _write_backfill_progress(repo: CryptoGuardRepository, symbol: str, interval: str, last_open_time: int) -> None:
    """Upsert backfill_progress row WITHOUT committing.

    P1-8 R2: previously this function did its own INSERT + commit, which
    created a second transaction separate from the candles upsert. If the
    process crashed between the candles commit and the progress commit,
    candles were written but progress was lost — on resume the same page
    was re-fetched. Now the caller is responsible for committing both the
    candles upsert and the progress write in a single transaction.

    Also removed the silent ``except Exception: pass`` — exceptions now
    propagate to the caller, which can decide whether to abort the backfill.
    """
    repo.conn.execute(
        "INSERT OR REPLACE INTO backfill_progress(symbol, interval, last_open_time_fetched, last_updated_ms) "
        "VALUES (?, ?, ?, ?)",
        (symbol, interval, int(last_open_time), int(repo.conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER) * 1000 AS ms").fetchone()["ms"])),
    )


def _verify_resume_progress(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    last_open_time: int,
    *,
    window_start_open: int,
    expected_last_open: int,
) -> bool:
    """Verify that ``last_open_time_fetched`` is trustworthy before resuming.

    P0-1 R3: Stale resume progress could skip all gaps and falsely report
    success. Three conditions must ALL hold for the progress to be trusted:

    1. **Bound to the target window**: ``last_open_time`` must fall within
       ``[window_start_open, expected_last_open]`` for the CURRENT analysis
       window. If it's outside (e.g., from a different analysis window or a
       far-future stale value), ignore it.
    2. **Candle exists**: a candle with ``open_time == last_open_time`` must
       actually exist in the DB for this (symbol, interval). If candles were
       deleted after progress was written, the progress is stale.
    3. **Contiguous chain backward**: the candle at ``last_open_time`` must be
       part of a contiguous chain extending backward to at least
       ``window_start_open`` (or the first available candle). If there's a gap
       between ``last_open_time`` and the candle before it, the progress is
       unreliable.

    Returns ``True`` if all checks pass, ``False`` otherwise.
    """
    span = INTERVAL_MS.get(interval)
    if not span:
        return False

    # 1. Bound to the target window.
    if last_open_time < window_start_open or last_open_time > expected_last_open:
        logger.warning(
            "backfill %s %s: resume progress last_open_time=%d is outside "
            "window [%d, %d] — ignoring stale progress",
            symbol, interval, last_open_time, window_start_open, expected_last_open,
        )
        return False

    # 2. Candle exists at last_open_time.
    try:
        row = repo.conn.execute(
            "SELECT 1 FROM candles "
            "WHERE symbol=? AND interval=? AND is_closed=1 AND open_time=? "
            "LIMIT 1",
            (symbol, interval, last_open_time),
        ).fetchone()
        if not row:
            logger.warning(
                "backfill %s %s: resume progress last_open_time=%d has no "
                "matching candle in DB — ignoring stale progress",
                symbol, interval, last_open_time,
            )
            return False
    except Exception as exc:
        logger.warning(
            "backfill %s %s: resume progress verification query failed: %s "
            "— ignoring stale progress",
            symbol, interval, exc,
        )
        return False

    # 3. Contiguous chain backward: verify each expected candle exists by
    #    exact open_time set comparison. P1-1 R4: the old COUNT-based check
    #    was fooled by misaligned/duplicate open_times — e.g. a window of 10
    #    expected 1h candles where one expected open_time is missing and a
    #    misaligned open_time is present in its place: COUNT=10 passes the
    #    old check but exact-set comparison fails.
    try:
        expected_count = (last_open_time - window_start_open) // span + 1
        if expected_count > 0:
            # Generate the complete set of expected open_time values:
            #   {window_start_open, window_start_open + span, ..., last_open_time}
            expected_opens: set[int] = {
                window_start_open + i * span
                for i in range(expected_count)
            }
            cur = repo.conn.execute(
                "SELECT open_time FROM candles "
                "WHERE symbol=? AND interval=? AND is_closed=1 "
                "AND open_time >= ? AND open_time <= ?",
                (symbol, interval, window_start_open, last_open_time),
            )
            actual_opens: set[int] = {int(r["open_time"]) for r in cur.fetchall()}
            if actual_opens != expected_opens:
                missing = expected_opens - actual_opens
                extra = actual_opens - expected_opens
                logger.warning(
                    "backfill %s %s: resume progress last_open_time=%d has "
                    "broken contiguity (expected %d candles in [%d, %d], "
                    "found %d, missing %d, extra %d) — ignoring stale progress",
                    symbol, interval, last_open_time, expected_count,
                    window_start_open, last_open_time, len(actual_opens),
                    len(missing), len(extra),
                )
                return False
    except Exception as exc:
        logger.warning(
            "backfill %s %s: resume contiguity verification query failed: %s "
            "— ignoring stale progress",
            symbol, interval, exc,
        )
        return False

    return True


def _is_gap_actually_filled(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    gap: tuple[int, int],
    span: int,
) -> bool:
    """Check whether a specific gap range is now fully covered by candles.

    P0-1 R3: The old ``gaps_filled`` computation counted a gap as "filled" if
    it was no longer in ``health_after.missing_ranges``. But an empty DB has
    no ``missing_ranges`` (``assess_health`` returns early with
    ``reason="empty"`` and ``missing_ranges=[]``), so ALL original gaps
    appeared "filled" when none were.

    P1-1 R4: The previous fix used a COUNT-based check (expected_count vs
    actual COUNT(*)) which was fooled by misaligned/duplicate open_times —
    e.g. a gap of 10 expected 1h candles where one expected open_time is
    missing and a misaligned open_time is present in its place: COUNT=10
    passes the old check but exact-set comparison fails.

    This helper queries the DB directly and compares the returned open_time
    set to the expected set. Returns ``True`` only if the sets match exactly.
    """
    gap_start, gap_end = gap
    # Expected open_times: gap_start, gap_start + span, ..., gap_end.
    expected_count = (gap_end - gap_start) // span + 1
    if expected_count <= 0:
        return True  # degenerate gap

    # Generate the complete set of expected open_time values.
    expected_opens: set[int] = {
        gap_start + i * span
        for i in range(expected_count)
    }

    try:
        cur = repo.conn.execute(
            "SELECT open_time FROM candles "
            "WHERE symbol=? AND interval=? AND is_closed=1 "
            "AND open_time >= ? AND open_time <= ?",
            (symbol, interval, gap_start, gap_end),
        )
        actual_opens: set[int] = {int(r["open_time"]) for r in cur.fetchall()}
        return actual_opens == expected_opens
    except Exception:
        return False


def _do_backfill(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str,
    *,
    analysis_time_utc: int,
    required_count: int,
    page_limit: int,
    max_pages: int,
    progress_cb: Any,
    resume: bool = False,
) -> dict[str, Any]:
    """Core backfill loop — assumes inputs are already validated."""

    span = INTERVAL_MS[interval]

    # 1. Compute missing ranges.
    gaps = compute_missing_ranges(
        repo, symbol, interval,
        analysis_time_utc=analysis_time_utc, required_count=required_count,
    )

    if not gaps:
        return _empty_result()

    # P0-1 R3: Compute the analysis window for resume-progress validation.
    # The window is [window_start_open, expected_last_open] where:
    #   expected_last_close = latest_closed_close_time_ms(interval, analysis_time_utc)
    #   expected_last_open = expected_last_close - span + 1
    #   window_start_open = expected_last_open - (required_count - 1) * span
    expected_last_close = latest_closed_close_time_ms(interval, analysis_time_utc)
    expected_last_open = expected_last_close - span + 1
    window_start_open = expected_last_open - (int(required_count) - 1) * span

    # 2. Read resume progress — only when explicitly resuming.
    # P0-8: Previously this was always called, causing stale progress entries
    # from prior runs to permanently skip real gaps. Now the caller must
    # explicitly opt in via resume=True.
    # P0-1 R3: Before trusting resume progress, verify it is bound to the
    # current analysis window AND that the corresponding candles actually
    # exist in the DB. Stale progress (from a different window, or from
    # candles that were deleted) must be ignored.
    resume_last_open = None
    if resume:
        raw_progress = _read_backfill_progress(repo, symbol, interval)
        if raw_progress is not None:
            if _verify_resume_progress(
                repo, symbol, interval, raw_progress,
                window_start_open=window_start_open,
                expected_last_open=expected_last_open,
            ):
                resume_last_open = raw_progress
            else:
                logger.warning(
                    "backfill %s %s: resume progress verified as stale — "
                    "starting from gap starts instead",
                    symbol, interval,
                )
    resumed_from_page = 0

    # 3. Track original gap count for gaps_filled computation.
    original_gap_count = len(gaps)

    pages_fetched = 0
    candles_upserted = 0
    network_errors = 0

    # 4. Iterate over each gap range.
    for gap_start, gap_end in gaps:
        # If resuming, skip gaps that are entirely before the resume point.
        # P0-1 R3: This branch now only fires when resume_last_open has been
        # verified as trustworthy (bound to window + candle exists + contiguous
        # chain). Stale progress that would skip all gaps is now ignored
        # above, so this skip is safe.
        if resume_last_open is not None and gap_end <= resume_last_open:
            # This gap was already covered by prior pages.
            # Count how many pages would have been needed for this gap.
            gap_span = gap_end - gap_start + span
            pages_needed = max(1, (gap_span + page_limit * span - 1) // (page_limit * span))
            resumed_from_page += pages_needed
            continue

        # Page start: either the gap start, or resume point + span if resuming
        # within this gap.
        page_start = gap_start
        if resume_last_open is not None and resume_last_open >= gap_start:
            # Resuming within this gap: advance page_start past the resume point.
            page_start = resume_last_open + span
            # Count how many pages were skipped from gap_start to resume_last_open.
            skipped_span = resume_last_open - gap_start + span
            pages_needed = max(1, (skipped_span + page_limit * span - 1) // (page_limit * span))
            resumed_from_page += pages_needed

        while page_start <= gap_end and pages_fetched < max_pages:
            # Compute page_end: the last open_time we want in this page.
            page_end_open = min(page_start + (page_limit - 1) * span, gap_end)
            # fetch_klines endTime is inclusive (close_time boundary). We pass
            # endTime = page_end_open + span - 1 (the close_time of the last
            # candle we want).
            page_end_close = page_end_open + span - 1
            # But we must not request candles with close_time > analysis_time.
            if page_end_close > analysis_time_utc:
                page_end_close = analysis_time_utc
                page_end_open = page_end_close - span + 1

            try:
                candles = binance_rest.fetch_klines(
                    symbol, interval,
                    start_time=page_start, end_time=page_end_close, limit=page_limit,
                )
            except MarketDataError as exc:
                logger.warning(
                    "backfill %s %s: network error on page %d (start=%d): %s",
                    symbol, interval, pages_fetched + 1, page_start, exc,
                )
                network_errors += 1
                # Preserve already-fetched pages; break out of this gap.
                break
            except Exception as exc:
                logger.warning(
                    "backfill %s %s: unexpected error on page %d (start=%d): %s",
                    symbol, interval, pages_fetched + 1, page_start, exc,
                )
                network_errors += 1
                break

            pages_fetched += 1

            # Handle empty page: advance start_time by one interval and continue.
            if not candles:
                logger.debug(
                    "backfill %s %s: empty page at start=%d, advancing by one interval",
                    symbol, interval, page_start,
                )
                page_start += span
                continue

            # Filter: drop future candles (close_time > analysis_time_utc) and
            # unclosed candles (is_closed=False).
            filtered = [
                c for c in candles
                if int(c["close_time"]) <= analysis_time_utc and c.get("is_closed", True)
            ]

            # Deduplicate in-memory by open_time before upsert.
            seen: dict[int, dict[str, Any]] = {}
            for c in filtered:
                ot = int(c["open_time"])
                if ot not in seen:
                    seen[ot] = c
            deduped = list(seen.values())

            if deduped:
                count = repo.upsert_candles(deduped)
                candles_upserted += count

            # P1-8: write progress in the SAME transaction as candles upsert,
            # then commit once. Previously upsert_candles + commit ran first,
            # then _write_backfill_progress did its own INSERT + commit — two
            # separate transactions. If the process crashed between them,
            # candles were written but progress was lost (on resume the same
            # page was re-fetched). Now both writes land in one atomic commit.
            last_open = max(int(c["open_time"]) for c in deduped) if deduped else page_start
            _write_backfill_progress(repo, symbol, interval, last_open)
            repo.conn.commit()

            # Advance page_start to the next candle after the last fetched.
            page_start = last_open + span

            # Call progress callback if provided.
            if progress_cb is not None:
                try:
                    progress_cb({
                        "pages_fetched": pages_fetched,
                        "candles_upserted": candles_upserted,
                        "network_errors": network_errors,
                    })
                except Exception:
                    pass

    # 5. Re-check health to compute gaps_filled.
    health_after = assess_health(
        repo, symbol, interval,
        analysis_time_utc=analysis_time_utc, required_count=required_count,
    )
    # P0-1 R3: gaps_filled computation must NOT count gaps as "filled" when
    # the DB is empty or the health check itself crashed. The old logic
    # counted a gap as "filled" if it was no longer in
    # ``health_after.missing_ranges`` — but an empty DB has no
    # ``missing_ranges`` (assess_health returns early with reason="empty"
    # and missing_ranges=[]), so ALL original gaps appeared "filled" when
    # none were.
    #
    # New logic:
    #   - If health_after is ready → all gaps filled.
    #   - If health_after reason is in the fail-closed set (empty,
    #     query_error, invalid_analysis_time, invalid_interval) → gaps_filled=0.
    #   - Otherwise, for each original gap, use _is_gap_actually_filled to
    #     verify the DB now has candles for every expected open_time in the
    #     gap range.
    fail_closed_reasons = {"empty", "query_error", "invalid_analysis_time", "invalid_interval"}
    if health_after["ready"]:
        gaps_filled = original_gap_count
    elif health_after.get("reason", "") in fail_closed_reasons:
        gaps_filled = 0
    else:
        gaps_filled = 0
        for og in gaps:
            if _is_gap_actually_filled(repo, symbol, interval, og, span):
                gaps_filled += 1

    logger.info(
        "backfill %s %s: pages=%d candles_upserted=%d gaps_filled=%d/%d network_errors=%d resumed_from_page=%d",
        symbol, interval, pages_fetched, candles_upserted, gaps_filled,
        original_gap_count, network_errors, resumed_from_page,
    )

    return {
        "pages_fetched": pages_fetched,
        "candles_upserted": candles_upserted,
        "gaps_filled": gaps_filled,
        "network_errors": network_errors,
        "resumed_from_page": resumed_from_page,
        "skipped_due_to_lock": False,
    }
