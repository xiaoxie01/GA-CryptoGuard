"""CLI repair tool for market-data gaps.

Implements R8 of the market-data-completeness P0 fix. Defaults to
``--dry-run`` which prints a per-(symbol, TF) gap report without modifying
the DB. ``--execute`` requires explicit ``--symbol``/``--interval`` or
``--all`` to run. ``--resume`` reads ``backfill_progress`` and continues.

Usage:
    python -m plugins.crypto_guard.tools.repair_market_data --dry-run
    python -m plugins.crypto_guard.tools.repair_market_data --execute --symbol BTCUSDT --interval 1h
    python -m plugins.crypto_guard.tools.repair_market_data --execute --all
    python -m plugins.crypto_guard.tools.repair_market_data --execute --symbol BTCUSDT --interval 1h --resume

Reference: .trellis/tasks/07-02-fix-market-data-completeness-p0/prd.md R8, AC20.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from typing import Any

from plugins.crypto_guard.config.loader import CryptoGuardConfig, load_config
from plugins.crypto_guard.data.candle_backfill import backfill_symbol_interval, compute_missing_ranges
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.notify.time_utils import format_event_time_cst
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import INTERVAL_MS, latest_closed_close_time_ms, utc_ms


logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m plugins.crypto_guard.tools.repair_market_data``.

    Flags:
        --dry-run   : print gap report, no DB writes (default).
        --execute   : perform backfill. Requires --symbol/--interval or --all.
        --symbol    : restrict to a single symbol.
        --interval  : restrict to a single interval.
        --all       : run for every active symbol x required interval.
        --resume    : read backfill_progress and continue from last checkpoint.

    Returns 0 on success, non-zero on error.

    Reference: PRD R8, AC20, AC21.
    """
    parser = argparse.ArgumentParser(
        prog="repair_market_data",
        description="Repair market-data gaps in the candles table (Binance USD-S-M Futures only).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print gap report, no DB writes (default).")
    mode.add_argument("--execute", action="store_true", help="Perform backfill (requires --symbol/--interval or --all).")
    parser.add_argument("--symbol", default=None, help="Restrict to a single symbol (e.g. BTCUSDT).")
    parser.add_argument("--interval", default=None, help="Restrict to a single interval (e.g. 1h).")
    parser.add_argument("--all", action="store_true", help="Run for every active symbol x required interval.")
    parser.add_argument("--resume", action="store_true", help="Read backfill_progress and continue from last checkpoint.")
    args = parser.parse_args(argv)

    # Default mode is --dry-run when neither --dry-run nor --execute is given.
    is_execute = bool(args.execute)
    is_dry_run = bool(args.dry_run) or not is_execute

    # Validate execute scope: --execute requires --symbol or --all.
    if is_execute and not args.symbol and not args.all:
        print("ERROR: --execute requires --symbol SYMBOL or --all to specify scope.", file=sys.stderr)
        print("Refusing to run backfill without an explicit scope. Aborting.", file=sys.stderr)
        return 1

    # Load config and open DB.
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"ERROR: failed to load config: {exc}", file=sys.stderr)
        return 1

    initialize_database(cfg)
    # PG cutover: pooled connection (auto-returned). Every repo write self-wraps
    # ``conn.transaction()``, so no manual commit/close is needed here.
    with pg_db.get_conn() as conn:
        repo = CryptoGuardRepository(conn)

        # Resolve symbols and intervals.
        symbols = _resolve_symbols(repo, args)
        intervals = _resolve_intervals(cfg, args)

        if not symbols:
            print("No symbols to process. Use --symbol SYMBOL or --all.", file=sys.stderr)
            return 1
        if not intervals:
            print("No intervals to process. Check config market_data.required_samples.", file=sys.stderr)
            return 1

        # Use the current latest-closed close time as analysis_time_utc for each
        # interval. We use the max across intervals so a single pass covers all.
        now_ms = utc_ms()
        # Per-interval analysis_time (each TF has its own boundary).
        analysis_times = {intv: latest_closed_close_time_ms(intv, now_ms) for intv in intervals}

        if is_dry_run:
            return _print_gap_report(repo, cfg, symbols, intervals, analysis_times)
        return _execute_backfill(repo, cfg, symbols, intervals, analysis_times, resume=bool(args.resume))


# ---------------------------------------------------------------------------
# Symbol/interval resolution
# ---------------------------------------------------------------------------


def _resolve_symbols(repo: CryptoGuardRepository, args: argparse.Namespace) -> list[str]:
    """Return the list of symbols to process based on args."""
    if args.symbol:
        return [args.symbol]
    if args.all:
        return repo.active_analysis_symbols()
    # Dry-run without --symbol or --all: default to active symbols for reporting.
    return repo.active_analysis_symbols()


def _resolve_intervals(cfg: CryptoGuardConfig, args: argparse.Namespace) -> list[str]:
    """Return the list of intervals to process based on args."""
    if args.interval:
        return [args.interval]
    md = cfg.market_data
    required = md.get("required_samples") if isinstance(md, dict) else {}
    if isinstance(required, dict) and required:
        return list(required.keys())
    # Fallback to the canonical default order.
    return ["1d", "4h", "1h", "15m", "5m"]


# ---------------------------------------------------------------------------
# Dry-run gap report
# ---------------------------------------------------------------------------


def _print_gap_report(
    repo: CryptoGuardRepository,
    cfg: CryptoGuardConfig,
    symbols: list[str],
    intervals: list[str],
    analysis_times: dict[str, int],
) -> int:
    """Print per-(symbol, TF) gap report. No DB writes.

    Reference: PRD R8, AC20.
    """
    md = cfg.market_data
    required_samples = md.get("required_samples", {}) if isinstance(md, dict) else {}
    backfill_cfg = md.get("backfill", {}) if isinstance(md, dict) else {}
    page_limit = int(backfill_cfg.get("page_limit", 1500)) if isinstance(backfill_cfg, dict) else 1500

    print("=" * 72)
    print("Market Data Gap Report (dry-run — no DB writes)")
    print("=" * 72)

    any_degraded = False
    total_estimated_pages = 0

    for symbol in symbols:
        print(f"\n[{symbol}]")
        for interval in intervals:
            required = int(required_samples.get(interval, 0)) if isinstance(required_samples, dict) else 0
            if required <= 0:
                print(f"  {interval}: required_count not configured, skipping.")
                continue
            at_ms = analysis_times.get(interval)
            if at_ms is None:
                at_ms = latest_closed_close_time_ms(interval, utc_ms())

            health = assess_health(
                repo, symbol, interval,
                analysis_time_utc=at_ms, required_count=required,
            )

            ready = bool(health["ready"])
            if not ready:
                any_degraded = True

            # Compute estimated pages from missing_ranges.
            missing_ranges = health.get("missing_ranges") or []
            span = INTERVAL_MS.get(interval, 0)
            est_pages = 0
            for gap_start, gap_end in missing_ranges:
                if span > 0:
                    gap_bars = (gap_end - gap_start) // span + 1
                    est_pages += max(1, math.ceil(gap_bars / page_limit))
            total_estimated_pages += est_pages

            _print_health_row(symbol, interval, health, at_ms, est_pages)

    print("\n" + "=" * 72)
    status = "DEGRADED — some TFs not ready" if any_degraded else "ALL READY"
    print(f"Summary: {status}")
    print(f"Total estimated pages to backfill: {total_estimated_pages}")
    print("=" * 72)
    print("Dry-run complete. No DB modifications were made.")
    return 0


def _print_health_row(
    symbol: str,
    interval: str,
    health: dict[str, Any],
    analysis_time_ms: int,
    est_pages: int,
) -> None:
    """Print one row of the gap report."""
    ready = "Y" if health["ready"] else "N"
    reason = health.get("reason", "") or ""
    total = health.get("total_closed_count", 0)
    contiguous = health.get("contiguous_tail_count", 0)
    required = health.get("required_count", 0)
    gap_count = health.get("gap_count", 0)
    largest_gap = health.get("largest_gap_bars", 0)
    last_close = health.get("last_close_time")
    expected_close = health.get("expected_last_close_time")
    stale = health.get("stale_bars", 0)

    last_close_cst = format_event_time_cst(last_close) if last_close else "N/A"
    expected_close_cst = format_event_time_cst(expected_close) if expected_close else "N/A"

    print(
        f"  {interval}: ready={ready}  total={total}  contiguous={contiguous}  "
        f"required={required}  gaps={gap_count}  largest_gap={largest_gap}bars  "
        f"stale={stale}  last_close={last_close_cst}  expected={expected_close_cst}"
    )
    if reason:
        print(f"    reason: {reason}")
    missing_ranges = health.get("missing_ranges") or []
    if missing_ranges:
        print(f"    missing_ranges ({len(missing_ranges)}):")
        for gap_start, gap_end in missing_ranges:
            gap_start_cst = format_event_time_cst(gap_start)
            gap_end_cst = format_event_time_cst(gap_end)
            print(f"      [{gap_start} .. {gap_end}]  ({gap_start_cst} .. {gap_end_cst})")
    if est_pages > 0:
        print(f"    estimated_pages: {est_pages}")


# ---------------------------------------------------------------------------
# Execute backfill
# ---------------------------------------------------------------------------


def _execute_backfill(
    repo: CryptoGuardRepository,
    cfg: CryptoGuardConfig,
    symbols: list[str],
    intervals: list[str],
    analysis_times: dict[str, int],
    *,
    resume: bool,
) -> int:
    """Run backfill_symbol_interval for each (symbol, TF) with gaps.

    Per-page commit is handled inside backfill_symbol_interval (R2). This
    function only writes to the candles table and backfill_progress table —
    it does NOT touch trades, orders, ga_decisions, or self-evolution data.

    Reference: PRD R8.
    """
    md = cfg.market_data
    required_samples = md.get("required_samples", {}) if isinstance(md, dict) else {}
    backfill_cfg = md.get("backfill", {}) if isinstance(md, dict) else {}
    max_pages_per_run = int(backfill_cfg.get("max_pages_per_run", 50)) if isinstance(backfill_cfg, dict) else 50

    print("=" * 72)
    print(f"Market Data Repair (execute mode{', resume' if resume else ''})")
    print("=" * 72)

    any_error = False

    for symbol in symbols:
        print(f"\n[{symbol}]")
        for interval in intervals:
            required = int(required_samples.get(interval, 0)) if isinstance(required_samples, dict) else 0
            if required <= 0:
                print(f"  {interval}: required_count not configured, skipping.")
                continue
            at_ms = analysis_times.get(interval)
            if at_ms is None:
                at_ms = latest_closed_close_time_ms(interval, utc_ms())

            # Pre-check: is backfill needed?
            health_before = assess_health(
                repo, symbol, interval,
                analysis_time_utc=at_ms, required_count=required,
            )
            if health_before["ready"]:
                print(f"  {interval}: already ready (contiguous={health_before['contiguous_tail_count']}). Skipping.")
                continue

            missing = compute_missing_ranges(
                repo, symbol, interval,
                analysis_time_utc=at_ms, required_count=required,
            )
            if not missing:
                print(f"  {interval}: no missing ranges detected (may be stale only). Skipping.")
                continue

            print(
                f"  {interval}: backfilling — gaps={len(missing)} "
                f"contiguous_before={health_before['contiguous_tail_count']} "
                f"required={required} max_pages={max_pages_per_run}"
            )

            try:
                result = backfill_symbol_interval(
                    repo, symbol, interval,
                    analysis_time_utc=at_ms,
                    required_count=required,
                    max_pages=max_pages_per_run,
                    progress_cb=_progress_cb_factory(symbol, interval),
                    resume=resume,
                )
            except Exception as exc:
                logger.warning("backfill %s %s failed: %s", symbol, interval, exc)
                print(f"  {interval}: BACKILL FAILED: {exc}")
                any_error = True
                continue

            skipped = result.get("skipped_due_to_lock", False)
            if skipped:
                print(f"  {interval}: skipped (another worker holds the backfill lock)")
                continue

            pages = result.get("pages_fetched", 0)
            candles_up = result.get("candles_upserted", 0)
            gaps_filled = result.get("gaps_filled", 0)
            net_errs = result.get("network_errors", 0)
            resumed = result.get("resumed_from_page", 0)
            print(
                f"  {interval}: done — pages={pages} candles_upserted={candles_up} "
                f"gaps_filled={gaps_filled} network_errors={net_errs} resumed_from_page={resumed}"
            )
            if net_errs > 0:
                any_error = True

            # Re-assess health after backfill.
            health_after = assess_health(
                repo, symbol, interval,
                analysis_time_utc=at_ms, required_count=required,
            )
            ready = "READY" if health_after["ready"] else "NOT READY"
            print(
                f"  {interval}: post-backfill status={ready} "
                f"contiguous={health_after['contiguous_tail_count']} "
                f"required={health_after['required_count']} "
                f"reason={health_after.get('reason', '')}"
            )

    print("\n" + "=" * 72)
    print("Repair complete." if not any_error else "Repair complete with errors (see above).")
    print("=" * 72)
    return 1 if any_error else 0


def _progress_cb_factory(symbol: str, interval: str):
    """Return a progress callback that prints per-page progress at info level."""
    def _cb(progress: dict[str, Any]) -> None:
        logger.info(
            "backfill %s %s progress: pages=%d candles_upserted=%d network_errors=%d",
            symbol, interval,
            progress.get("pages_fetched", 0),
            progress.get("candles_upserted", 0),
            progress.get("network_errors", 0),
        )
    return _cb


if __name__ == "__main__":
    sys.exit(main())
