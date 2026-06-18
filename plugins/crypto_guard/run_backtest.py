"""Run historical backtest for all symbols and generate summary report.

Usage:
    python -m plugins.crypto_guard.run_backtest
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.crypto_guard.backtest.historical_replay import run_historical_replay
from plugins.crypto_guard.config.loader import PROJECT_ROOT, load_config
from plugins.crypto_guard.fetch_historical import ALL_SYMBOLS
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db

LOGGER = get_logger("crypto_guard.backtest")

BACKTEST_OUTPUT_DIR = PROJECT_ROOT / "data" / "backtest_results"


def _load_candles_from_parquet(symbol: str, interval: str) -> list[dict[str, Any]]:
    """Load candles from Parquet via DuckDB. Returns empty list on failure."""
    try:
        from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
        duckdb = DuckDBAnalytics()
        rows = duckdb.query_klines(symbol, interval)
        if rows:
            LOGGER.info("Loaded %d candles from Parquet for %s %s", len(rows), symbol, interval)
            return rows
    except Exception as exc:
        LOGGER.debug("Parquet load failed for %s %s: %s", symbol, interval, exc)
    return []


def _load_candles_from_repo(repo: CryptoGuardRepository, symbol: str, interval: str) -> list[dict[str, Any]]:
    """Load candles from SQLite repository."""
    candles = repo.get_candles(symbol, interval, limit=50000)
    if candles:
        LOGGER.info("Loaded %d candles from SQLite for %s %s", len(candles), symbol, interval)
    return candles


def run_full_backtest(
    repo: CryptoGuardRepository,
    *,
    symbols: list[str] | None = None,
    interval: str = "15m",
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Run backtest for all symbols on the given interval.

    Data source priority: Parquet (via DuckDB) > SQLite.
    Uses real trade simulation (not pseudo-R) and market regime classification.
    """
    targets = symbols or ALL_SYMBOLS
    if end_time is None:
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_time is None:
        # Default: 6 months of data
        start_time = int(datetime(2025, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)

    results: list[dict[str, Any]] = []
    total_signals = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    all_regime_counts: dict[str, int] = {}

    for symbol in targets:
        LOGGER.info("Running backtest for %s %s...", symbol, interval)

        # Priority: Parquet > SQLite
        candles = _load_candles_from_parquet(symbol, interval)
        data_source = "parquet"
        if not candles:
            candles = _load_candles_from_repo(repo, symbol, interval)
            data_source = "sqlite"
        if not candles:
            LOGGER.warning("No candles for %s %s (neither Parquet nor SQLite), skipping", symbol, interval)
            continue

        result = run_historical_replay(
            repo,
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            candles=candles,
        )

        stats = result.get("stats", {})
        regime_dist = result.get("regime_distribution", {})
        signal_count = stats.get("signal_count", 0)
        trade_count = stats.get("trade_count", 0)
        win_count = stats.get("win_count", 0)
        loss_count = stats.get("loss_count", 0)

        total_signals += signal_count
        total_trades += trade_count
        total_wins += win_count
        total_losses += loss_count

        for regime, count in regime_dist.items():
            all_regime_counts[regime] = all_regime_counts.get(regime, 0) + count

        results.append({
            "symbol": symbol,
            "interval": interval,
            "data_source": data_source,
            "candle_count": len(candles),
            "signal_count": signal_count,
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": stats.get("win_rate", 0.0),
            "avg_r": stats.get("avg_r", 0.0),
            "sharpe_ratio": stats.get("sharpe_ratio", 0.0),
            "drawdown": stats.get("drawdown", 0.0),
            "regime_distribution": regime_dist,
            "replay_result_id": result.get("replay_result_id"),
        })

        LOGGER.info(
            "  %s: %d signals, %d trades, win_rate=%.1f%%, avg_r=%.3f",
            symbol, signal_count, trade_count,
            stats.get("win_rate", 0) * 100,
            stats.get("avg_r", 0),
        )

    overall_win_rate = total_wins / total_trades if total_trades > 0 else 0.0
    overall_avg_r = sum(r["avg_r"] * r["trade_count"] for r in results) / total_trades if total_trades > 0 else 0.0

    summary = {
        "ok": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": interval,
        "start_time": start_time,
        "end_time": end_time,
        "symbols_tested": len(results),
        "total_signals": total_signals,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "overall_win_rate": overall_win_rate,
        "overall_avg_r": overall_avg_r,
        "regime_distribution": all_regime_counts,
        "per_symbol": results,
    }

    # Save to file
    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = BACKTEST_OUTPUT_DIR / f"backtest_{timestamp}.json"
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["output_path"] = str(output_path)

    LOGGER.info("Backtest complete: %d trades, win_rate=%.1f%%, avg_r=%.3f, output=%s",
                total_trades, overall_win_rate * 100, overall_avg_r, output_path)

    return summary


def process_backtest_for_evolution(repo: CryptoGuardRepository, backtest_result: dict[str, Any]) -> dict[str, Any]:
    """Analyze backtest results and generate evolution triggers/patches.

    Identifies underperforming strategies by market regime and generates
    candidate patches for self-evolution.
    """
    from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers

    per_symbol = backtest_result.get("per_symbol", [])
    regime_stats: dict[str, dict[str, Any]] = {}

    # Aggregate stats by regime
    for sym_result in per_symbol:
        for regime, count in sym_result.get("regime_distribution", {}).items():
            if regime not in regime_stats:
                regime_stats[regime] = {"total_signals": 0, "total_trades": 0, "wins": 0, "losses": 0}
            regime_stats[regime]["total_signals"] += count

    # Identify issues
    findings: list[dict[str, Any]] = []
    overall_wr = backtest_result.get("overall_win_rate", 0)

    if overall_wr < 0.40:
        findings.append({
            "type": "low_win_rate",
            "severity": "high",
            "detail": f"Overall win rate {overall_wr:.1%} is below 40%",
            "suggestion": "Consider increasing score threshold or adding more filters",
        })

    for symbol_result in per_symbol:
        sym = symbol_result.get("symbol")
        wr = symbol_result.get("win_rate", 0)
        if symbol_result.get("trade_count", 0) >= 5 and wr < 0.30:
            findings.append({
                "type": "symbol_underperform",
                "severity": "medium",
                "symbol": sym,
                "detail": f"{sym} win rate {wr:.1%} with {symbol_result['trade_count']} trades",
                "suggestion": f"Review {sym} market conditions, may need symbol-specific tuning",
            })

    # Check for existing evolution triggers
    triggers = evaluate_evolution_triggers(repo)

    evolution_result = {
        "ok": True,
        "findings": findings,
        "triggers_evaluated": len(triggers),
        "regime_stats": regime_stats,
        "recommendations": _generate_recommendations(findings, backtest_result),
    }

    LOGGER.info("Evolution analysis: %d findings, %d triggers", len(findings), len(triggers))
    return evolution_result


def _generate_recommendations(findings: list[dict[str, Any]], backtest: dict[str, Any]) -> list[str]:
    """Generate actionable recommendations from backtest findings."""
    recs: list[str] = []
    for f in findings:
        if f["type"] == "low_win_rate":
            recs.append("提高模拟盘准入阈值：将 confidence 阈值从 0.72 提升到 0.78")
            recs.append("增加趋势过滤：只在 trend_stage=early/middle 时允许开仓")
        elif f["type"] == "symbol_underperform":
            recs.append(f"对 {f.get('symbol', '?')} 增加额外风控过滤或降低仓位权重")
    if not recs:
        recs.append("当前策略表现可接受，继续积累样本后评估")
    return recs


def main() -> None:
    """CLI entry point for running backtests."""
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        print("Running full backtest...")
        result = run_full_backtest(repo)
        print(f"\nBacktest Summary:")
        print(f"  Symbols tested: {result['symbols_tested']}")
        print(f"  Total signals: {result['total_signals']}")
        print(f"  Total trades: {result['total_trades']}")
        print(f"  Win rate: {result['overall_win_rate']:.1%}")
        print(f"  Avg R: {result['overall_avg_r']:.3f}")
        print(f"  Output: {result.get('output_path', 'N/A')}")
        print(f"\nPer-symbol:")
        for r in result["per_symbol"]:
            src = r.get("data_source", "?")
            print(f"  {r['symbol']:>10}: {r['trade_count']} trades, wr={r['win_rate']:.1%}, avg_r={r['avg_r']:.3f} [{src}]")

        print("\nRunning evolution analysis...")
        evo = process_backtest_for_evolution(repo, result)
        if evo["findings"]:
            print(f"\nFindings ({len(evo['findings'])}):")
            for f in evo["findings"]:
                print(f"  [{f['severity']}] {f['detail']}")
        print(f"\nRecommendations:")
        for r in evo["recommendations"]:
            print(f"  - {r}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
