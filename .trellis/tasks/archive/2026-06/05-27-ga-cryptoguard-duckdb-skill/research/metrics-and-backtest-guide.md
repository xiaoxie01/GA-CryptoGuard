# Research: CryptoGuard Metrics and Backtest Implementation Guide

- **Query**: Win rate metrics and how to backtest/optimize strategies
- **Scope**: internal
- **Date**: 2026-05-27

## Current Metrics

### Win Rate and Accuracy

**No trade-level metrics exist.** The database contains:

- `paper_trades`: 0 rows (no trades executed)
- `trade_reviews`: 0 rows
- `strategy_memory`: 0 rows

The only performance data available is at the signal evaluation level:

- `strategy_evaluations`: 2,474 total
  - 2,469 = `no_edge` (score below 0.65 threshold)
  - 5 = `monitor_only` (score between 0.65-0.72)
  - 0 = `notify_candidate` (score 0.72-0.80)
  - 0 = `paper_trade_candidate` (score above 0.80)

**Interpretation**: The scoring system is very conservative. With a base of 0.50 and maximum theoretical additions of ~0.78 (all factors aligned), the practical range lands around 0.50-0.85. The 0.80 threshold for paper trading requires near-perfect multi-factor alignment. All 2,474 evaluations scored below this threshold, suggesting either:
1. Market conditions did not produce high-confidence setups, or
2. The scoring weights are too conservative

### Pseudo-R Values (from scoring)

From the 2,474 evaluations, the pseudo-R calculation in `shadow_testing.py:157` uses `(score - 0.5) * 2`:
- With scores averaging around 0.13-0.21 (from recent samples), pseudo-R values are negative
- This indicates the system correctly identifies low-quality setups

## How to Run a Backtest

### Prerequisites

1. **Parquet data exists**: 12 symbols, 5 timeframes, May 2026 data at `data/parquet/klines/binance_um/{SYMBOL}/{INTERVAL}/2026-05.parquet`
2. **Backtest module exists**: `plugins/crypto_guard/backtest/historical_replay.py`

### Execution Steps

```python
from plugins.crypto_guard.backtest.historical_replay import run_historical_replay
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db

# Connect to DB
conn = connect_db("data/crypto_guard/crypto_guard.sqlite3")
repo = CryptoGuardRepository(conn)

# Run replay for BTCUSDT 1h
result = run_historical_replay(
    repo,
    symbol="BTCUSDT",
    interval="1h",
    start_time=1714521600000,  # May 2024 start (ms)
    end_time=1717200000000,    # May 2024 end (ms)
    parquet_path="data/parquet/klines/binance_um/BTCUSDT/1h/2026-05.parquet",
    strategy_versions=["1.0"],
    warmup=30,  # Skip first 30 candles for indicator warmup
)

# Result contains:
# - result["signals"]: all generated signals with grades
# - result["trades"]: pseudo-trades (filtered by decision)
# - result["stats"]: {signal_count, avg_r, win_rate, drawdown}
# - result["strategy_comparison"]: version comparison
# - result["no_lookahead"]: validation result
# - result["agent_analysis"]: LLM analysis
```

### Backtest Output Structure

```
result = {
    "ok": bool,                    # True if no lookahead violations
    "symbol": str,
    "interval": str,
    "candles_replayed": int,
    "signals": [...],              # All signals with decision/grade/confidence
    "trades": [...],               # Filtered pseudo-trades
    "stats": {
        "signal_count": int,
        "avg_r": float,
        "win_rate": float,         # Percentage of signals with pseudo-R > 0.05
        "drawdown": float
    },
    "strategy_comparison": [...],  # Per-version stats
    "no_lookahead": {
        "ok": bool,
        "violation_count": int
    },
    "agent_analysis": {...}        # LLM analysis
}
```

### Strategy Optimization via Backtest

The system supports optimization through the self-evolution loop:

1. **Run historical replay** to get baseline stats
2. **Create candidate version** with adjusted scoring weights
3. **Run shadow test** comparing active vs candidate on same data
4. **Promote or reject** based on: avg_r improvement, win_rate maintenance, drawdown constraint

The `_compare_strategy_versions()` function in `historical_replay.py` currently uses synthetic adjustments (not real per-version scoring). For true optimization, you would need to:
1. Create a candidate with modified scoring weights in `strategies.yaml`
2. Run the same replay with both versions
3. Compare the actual `stats` output

### Limitations

1. **Pseudo-R, not real R**: The backtest calculates pseudo-R from confidence scores, not from actual price movement simulation (no entry/exit/stop simulation)
2. **No position sizing**: The backtest does not simulate position sizes or account equity
3. **Synthetic version comparison**: `_compare_strategy_versions()` adds idx*0.02 offsets, not real differential scoring
4. **1 month of data**: Only May 2026 Parquet files exist -- insufficient for robust backtesting
5. **No walk-forward**: No out-of-sample validation or rolling window optimization

### To Get Real Win Rate

The system needs paper trades to flow through. The path is:
1. Analysis produces snapshot with score >= 0.80
2. Decision becomes `paper_trade_candidate`
3. Paper order is created and filled
4. Trade closes (stop loss, take profit, or timeout)
5. `daily_reviewer.run_daily_review()` reviews closed trades
6. `trade_reviewer.review_trade()` classifies each trade
7. Metrics accumulate in `strategy_memory` and `trade_reviews`
