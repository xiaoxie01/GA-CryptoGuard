"""Run backtest with 10U per-trade position sizing and calculate actual P&L.

Usage:
    python -m plugins.crypto_guard.run_pnl_backtest
    python -m plugins.crypto_guard.run_pnl_backtest --symbol ETHUSDT
    python -m plugins.crypto_guard.run_pnl_backtest --months 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.backtest.historical_replay import (
    _classify_market_regime,
    _normalize_replay_candle,
    _simulate_trade,
)
from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db

POSITION_SIZE_USDT = 10.0  # 每笔交易 10U
COOLDOWN_CANDLES = 8  # 开仓后冷却 8 根 K 线 (15m × 8 = 2小时)


def _load_candles(symbol: str, interval: str) -> list[dict[str, Any]]:
    """Load candles from Parquet via DuckDB."""
    try:
        from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
        duckdb = DuckDBAnalytics()
        rows = duckdb.query_klines(symbol, interval)
        if rows:
            print(f"  Loaded {len(rows)} candles from Parquet for {symbol} {interval}")
            return rows
    except Exception as exc:
        print(f"  Parquet load failed: {exc}")
    return []


def run_single_symbol_backtest(
    repo: CryptoGuardRepository,
    symbol: str,
    interval: str = "15m",
    start_time: int | None = None,
    end_time: int | None = None,
    position_size: float = POSITION_SIZE_USDT,
) -> dict[str, Any]:
    """Run backtest for a single symbol with real P&L calculation."""
    from tempfile import TemporaryDirectory
    from pathlib import Path

    if end_time is None:
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_time is None:
        start_time = int(datetime(2025, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)

    candles = _load_candles(symbol, interval)
    if not candles:
        return {"ok": False, "error": f"No candles for {symbol} {interval}"}

    # Filter and sort
    rows = [
        _normalize_replay_candle(c, symbol=symbol, interval=interval)
        for c in candles
        if int(c["close_time"]) >= start_time and int(c["close_time"]) <= end_time
    ]
    rows.sort(key=lambda c: int(c["close_time"]))

    warmup = 30
    if len(rows) < warmup:
        return {"ok": False, "error": f"Only {len(rows)} candles, need {warmup}+"}

    print(f"  Replaying {len(rows)} candles ({rows[0]['close_time']} → {rows[-1]['close_time']})")

    signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    cooldown_until = 0  # index until which we skip trade signals

    with TemporaryDirectory() as tmp:
        replay_db = Path(tmp) / "replay.sqlite3"
        old_db = os.environ.get("CRYPTO_GUARD_DB")
        os.environ["CRYPTO_GUARD_DB"] = str(replay_db)
        cfg = load_config()
        initialize_database(cfg)
        if old_db is not None:
            os.environ["CRYPTO_GUARD_DB"] = old_db
        else:
            os.environ.pop("CRYPTO_GUARD_DB", None)

        replay_conn = connect_db(replay_db)
        try:
            replay_repo = CryptoGuardRepository(replay_conn)
            for idx, candle in enumerate(rows):
                replay_repo.upsert_candles([candle])
                if idx + 1 < warmup:
                    continue

                analysis_time = int(candle["close_time"])
                regime = _classify_market_regime(rows[: idx + 1])

                snapshot = build_market_state_snapshot(
                    replay_repo,
                    symbol=symbol,
                    analysis_time_utc=analysis_time,
                    mode="shadow_test",
                    timeframes=[interval],
                )
                decision = run_ga_sop_decision(snapshot)

                signal = {
                    "analysis_time_utc": analysis_time,
                    "close": candle["close"],
                    "decision": decision["decision"],
                    "signal_grade": decision["signal_grade"],
                    "confidence": decision["confidence"],
                    "market_bias": decision.get("market_bias"),
                    "market_regime": regime,
                }

                # Simulate trade if trade plan exists and not in cooldown
                trade_plan = decision.get("trade_plan")
                if trade_plan and decision["decision"] == "trade_plan_available" and idx >= cooldown_until:
                    subsequent = rows[idx + 1: idx + 51]
                    sim = _simulate_trade(trade_plan, subsequent)
                    # Set cooldown: skip next N candles after opening a trade
                    cooldown_until = idx + 1 + COOLDOWN_CANDLES
                    signal["trade_simulation"] = sim
                    signal["pnl_r"] = sim["pnl_r"]
                    signal["trade_outcome"] = sim["outcome"]

                    # Calculate actual P&L in USDT
                    entry = trade_plan.get("entry_price", 0)
                    sl = trade_plan.get("stop_loss", 0)
                    risk_per_unit = abs(entry - sl)
                    risk_pct = trade_plan.get("risk_percent", 0.5) / 100.0
                    risk_usdt = position_size * risk_pct  # e.g. 10U * 0.5% = 0.05U
                    pnl_usdt = sim["pnl_r"] * risk_usdt

                    trade_record = {
                        "index": idx,
                        "time": analysis_time,
                        "side": trade_plan.get("side"),
                        "entry_price": entry,
                        "stop_loss": sl,
                        "exit_price": sim.get("exit_price"),
                        "signal_grade": decision["signal_grade"],
                        "confidence": decision["confidence"],
                        "market_regime": regime,
                        "outcome": sim["outcome"],
                        "pnl_r": sim["pnl_r"],
                        "risk_usdt": round(risk_usdt, 6),
                        "pnl_usdt": round(pnl_usdt, 6),
                        "holding_candles": sim.get("holding_candles", 0),
                    }
                    trades.append(trade_record)
                else:
                    signal["trade_simulation"] = None
                    signal["pnl_r"] = None
                    signal["trade_outcome"] = None

                signals.append(signal)
        finally:
            replay_conn.close()

    # Aggregate P&L
    total_pnl = sum(t["pnl_usdt"] for t in trades)
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    timeouts = [t for t in trades if t["outcome"] == "timeout"]
    invalids = [t for t in trades if t["outcome"] == "invalid"]

    win_pnl = sum(t["pnl_usdt"] for t in wins)
    loss_pnl = sum(t["pnl_usdt"] for t in losses)
    timeout_pnl = sum(t["pnl_usdt"] for t in timeouts)

    # Equity curve
    equity = position_size  # starting capital
    peak_equity = equity
    max_drawdown = 0.0
    equity_curve = [equity]
    for t in trades:
        equity += t["pnl_usdt"]
        equity_curve.append(round(equity, 4))
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
        max_drawdown = max(max_drawdown, dd)

    # Per-regime stats
    regime_stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        r = t["market_regime"]
        if r not in regime_stats:
            regime_stats[r] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        regime_stats[r]["trades"] += 1
        if t["outcome"] == "win":
            regime_stats[r]["wins"] += 1
        elif t["outcome"] == "loss":
            regime_stats[r]["losses"] += 1
        regime_stats[r]["pnl"] = round(regime_stats[r]["pnl"] + t["pnl_usdt"], 6)

    # Per-grade stats
    grade_stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        g = t["signal_grade"]
        if g not in grade_stats:
            grade_stats[g] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        grade_stats[g]["trades"] += 1
        if t["outcome"] == "win":
            grade_stats[g]["wins"] += 1
        elif t["outcome"] == "loss":
            grade_stats[g]["losses"] += 1
        grade_stats[g]["pnl"] = round(grade_stats[g]["pnl"] + t["pnl_usdt"], 6)

    result = {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "start_time": start_time,
        "end_time": end_time,
        "candles_replayed": len(rows),
        "total_signals": len(signals),
        "total_trades": len(trades),
        "position_size_usdt": position_size,
        "starting_balance": position_size,
        "final_balance": round(equity, 4),
        "total_pnl_usdt": round(total_pnl, 4),
        "total_pnl_pct": round((total_pnl / position_size) * 100, 2),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "invalids": len(invalids),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0,
        "avg_win_usdt": round(win_pnl / len(wins), 6) if wins else 0,
        "avg_loss_usdt": round(loss_pnl / len(losses), 6) if losses else 0,
        "profit_factor": round(abs(win_pnl / loss_pnl), 4) if loss_pnl != 0 else float("inf"),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "avg_r": round(sum(t["pnl_r"] for t in trades) / len(trades), 4) if trades else 0,
        "regime_stats": regime_stats,
        "grade_stats": grade_stats,
        "trades": trades,
        "equity_curve": equity_curve,
    }
    return result


def print_report(result: dict[str, Any]) -> None:
    """Print a formatted backtest report."""
    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}")
        return

    sym = result["symbol"]
    print(f"\n{'='*60}")
    print(f"  BACKTEST REPORT: {sym} {result['interval']}")
    print(f"{'='*60}")
    print(f"  Period: {datetime.fromtimestamp(result['start_time']/1000, timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(result['end_time']/1000, timezone.utc).strftime('%Y-%m-%d')}")
    print(f"  Candles replayed: {result['candles_replayed']}")
    print(f"  Total signals:    {result['total_signals']}")
    print(f"  Total trades:     {result['total_trades']}")
    print()

    print(f"  --- P&L Summary (每笔 {result['position_size_usdt']}U) ---")
    print(f"  Starting balance:  {result['starting_balance']:.2f} U")
    print(f"  Final balance:     {result['final_balance']:.4f} U")
    print(f"  Total P&L:         {result['total_pnl_usdt']:+.4f} U ({result['total_pnl_pct']:+.2f}%)")
    print()

    print(f"  --- Trade Stats ---")
    print(f"  Wins:     {result['wins']}")
    print(f"  Losses:   {result['losses']}")
    print(f"  Timeouts: {result['timeouts']}")
    print(f"  Invalid:  {result['invalids']}")
    print(f"  Win rate:      {result['win_rate']:.1%}")
    print(f"  Avg R:         {result['avg_r']:.4f}")
    print(f"  Avg win:       {result['avg_win_usdt']:+.6f} U")
    print(f"  Avg loss:      {result['avg_loss_usdt']:+.6f} U")
    print(f"  Profit factor: {result['profit_factor']:.4f}")
    print(f"  Max drawdown:  {result['max_drawdown_pct']:.2f}%")
    print()

    regime = result.get("regime_stats", {})
    if regime:
        print(f"  --- By Market Regime ---")
        for r, s in sorted(regime.items()):
            wr = s["wins"] / s["trades"] if s["trades"] else 0
            print(f"    {r:>15}: {s['trades']:>4} trades, {s['wins']}W/{s['losses']}L, wr={wr:.0%}, pnl={s['pnl']:+.6f}U")

    grades = result.get("grade_stats", {})
    if grades:
        print(f"\n  --- By Signal Grade ---")
        for g in ["S", "A", "B", "C", "D"]:
            s = grades.get(g)
            if not s:
                continue
            wr = s["wins"] / s["trades"] if s["trades"] else 0
            print(f"    Grade {g}: {s['trades']:>4} trades, {s['wins']}W/{s['losses']}L, wr={wr:.0%}, pnl={s['pnl']:+.6f}U")

    # Show last 20 trades
    trades = result.get("trades", [])
    if trades:
        print(f"\n  --- Last 20 Trades ---")
        print(f"  {'Time':>12} {'Side':>5} {'Grade':>5} {'Entry':>10} {'Exit':>10} {'R':>8} {'P&L(U)':>10} {'Outcome':>8}")
        for t in trades[-20:]:
            ts = datetime.fromtimestamp(t["time"]/1000, timezone.utc).strftime("%m-%d %H:%M")
            print(f"  {ts:>12} {t['side']:>5} {t['signal_grade']:>5} {t['entry_price']:>10.2f} {t['exit_price'] or 0:>10.2f} {t['pnl_r']:>+8.3f} {t['pnl_usdt']:>+10.6f} {t['outcome']:>8}")

    print(f"\n{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest with 10U position sizing")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol to backtest (default: BTCUSDT)")
    parser.add_argument("--symbols", nargs="+", help="Multiple symbols to backtest")
    parser.add_argument("--interval", default="15m", help="Timeframe (default: 15m)")
    parser.add_argument("--months", type=int, default=6, help="Months of data (default: 6)")
    parser.add_argument("--position", type=float, default=10.0, help="Position size in USDT (default: 10)")
    parser.add_argument("--export", help="Export results to JSON file")
    args = parser.parse_args()

    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)

    try:
        repo = CryptoGuardRepository(conn)

        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        from dateutil.relativedelta import relativedelta
        start_dt = datetime.now(timezone.utc) - relativedelta(months=args.months)
        start_time = int(start_dt.timestamp() * 1000)

        symbols = args.symbols or [args.symbol]
        all_results = []

        for sym in symbols:
            print(f"\nRunning backtest for {sym} {args.interval} ({args.months} months, {args.position}U per trade)...")
            result = run_single_symbol_backtest(
                repo,
                symbol=sym,
                interval=args.interval,
                start_time=start_time,
                end_time=end_time,
                position_size=args.position,
            )
            print_report(result)
            all_results.append(result)

        # Multi-symbol summary
        if len(all_results) > 1:
            print(f"\n{'='*60}")
            print(f"  MULTI-SYMBOL SUMMARY")
            print(f"{'='*60}")
            total_pnl = 0
            total_trades = 0
            total_wins = 0
            for r in all_results:
                if not r.get("ok"):
                    print(f"  {r.get('symbol', '?')}: FAILED - {r.get('error')}")
                    continue
                total_pnl += r["total_pnl_usdt"]
                total_trades += r["total_trades"]
                total_wins += r["wins"]
                print(f"  {r['symbol']:>10}: {r['total_trades']:>4} trades, wr={r['win_rate']:.0%}, pnl={r['total_pnl_usdt']:+.4f}U ({r['total_pnl_pct']:+.2f}%)")
            overall_wr = total_wins / total_trades if total_trades else 0
            print(f"  {'TOTAL':>10}: {total_trades:>4} trades, wr={overall_wr:.0%}, pnl={total_pnl:+.4f}U")
            print(f"{'='*60}\n")

        if args.export:
            export_data = all_results if len(all_results) > 1 else all_results[0]
            Path(args.export).parent.mkdir(parents=True, exist_ok=True)
            Path(args.export).write_text(json.dumps(export_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Exported to {args.export}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
