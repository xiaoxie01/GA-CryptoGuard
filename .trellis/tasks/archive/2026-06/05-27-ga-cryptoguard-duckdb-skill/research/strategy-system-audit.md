# Research: GA CryptoGuard Strategy System Audit

- **Query**: Audit the GA CryptoGuard strategy system for completeness in analysis, backtesting, and review capabilities
- **Scope**: internal
- **Date**: 2026-05-27

## Findings

### 1. Strategy System -- IMPLEMENTED

**Files Found:**

| File Path | Description |
|---|---|
| `plugins/crypto_guard/strategy/strategy_scorer.py` | Scoring logic: multi-factor weighted scoring (PA, momentum, trend, SMC, order flow, chanlun) |
| `plugins/crypto_guard/strategy/strategy_loader.py` | Loads active strategies from YAML config |
| `plugins/crypto_guard/strategy/version_manager.py` | Version management: create candidates, rollback, list versions |
| `plugins/crypto_guard/strategy/shadow_testing.py` | Shadow testing: record evaluations, compare active vs candidate, promote |
| `plugins/crypto_guard/strategy/self_evolution.py` | Self-evolution cycle: aggregate reviews, generate patches, shadow test, promote |
| `plugins/crypto_guard/config/strategies.yaml` | Strategy definitions (3 strategies) |

**Strategies Defined:**

| Strategy | Status |
|---|---|
| `smc_pullback_long` v1.0 | active |
| `pa_breakout_retest_long` v1.0 | active |
| `momentum_continuation_long` v1.0 | candidate |

**Scoring Logic (strategy_scorer.py):**
- Base score: 0.50
- Price action confirmation: +0.20 max (BOS/CHoCH bonus)
- Momentum confirmation: +0.15 max (healthy quality bonus)
- Trend stage: +0.15 max (early/middle stages)
- SMC liquidity/FVG: +0.10 max
- Order flow: +0.08 max (conflict penalty -0.05)
- Chanlun: +0.05 max
- Contradiction adjustment: +0.08 (low) / -0.15 (high)
- Late trend penalty: -0.08
- Range penalty: -0.10
- Final range: 0.0 to 0.95
- Grade mapping: S>=0.80, A>=0.72, B>=0.65, C>=0.50, D<0.50

### 2. Review System -- IMPLEMENTED

**Files Found:**

| File Path | Description |
|---|---|
| `plugins/crypto_guard/review/trade_reviewer.py` | Per-trade review: classify, attribute, generate candidate patches |
| `plugins/crypto_guard/review/loss_classifier.py` | Loss classification: 9 categories (good_execution, wrong_direction, entry_chasing, etc.) |
| `plugins/crypto_guard/review/daily_reviewer.py` | Daily review: batch review, strategy memory updates, Feishu summary |
| `plugins/crypto_guard/review/evolution_engine.py` | Build candidate patches from loss attribution |
| `plugins/crypto_guard/review/evolution_triggers.py` | Trigger on 3 consecutive stop losses or 10%+ drawdown |

**Loss Classification Categories:**
- `good_execution` (win)
- `wrong_direction` (stop loss, not chasing)
- `entry_chasing` (stop loss, low entry efficiency)
- `entry_too_late` (stop loss, MFE > MAE)
- `entry_too_early` (deep loss, MAE > 1.5x MFE)
- `late_trend_chasing` (high signal decay score)
- `stop_loss_too_tight` (deep loss, tight stop)
- `take_profit_too_far` (timeout close)
- `unknown` (insufficient evidence)

### 3. Backtest System -- IMPLEMENTED

**Files Found:**

| File Path | Description |
|---|---|
| `plugins/crypto_guard/backtest/historical_replay.py` | Candle-by-candle historical replay with no-lookahead validation |
| `plugins/crypto_guard/storage/parquet_archive.py` | Parquet archive for kline storage (1888 archive runs recorded) |

**Backtest Capabilities:**
- Historical replay from Parquet/JSON/CSV files
- Creates temporary SQLite DB per replay (isolated environment)
- No-lookahead violation detection
- Pseudo R-value calculation from confidence + grade + decision
- Strategy version comparison
- LLM agent analysis of replay results
- Export to JSON

### 4. Current Metrics -- NO TRADE DATA

**Database State (as of 2026-05-27):**

| Table | Count | Notes |
|---|---|---|
| `paper_trades` | 0 | No trades executed yet |
| `trade_reviews` | 0 | No reviews (no trades to review) |
| `strategy_evaluations` | 2,474 | All for `smc_pullback_long`, 2469 `no_edge`, 5 `monitor_only` |
| `strategy_versions` | 3 | 2 active, 1 candidate |
| `shadow_test_results` | 0 | No shadow tests run |
| `evolution_triggers` | 0 | No triggers fired |
| `skill_feedback_memory` | 0 | No feedback recorded |
| `strategy_memory` | 0 | No memory entries |
| `historical_replay_results` | 0 | No backtests run |
| `strategy_patches` | 0 | No patches generated |
| `self_evolution_runs` | 0 | No evolution cycles run |
| `daily_review_reports` | 0 | No daily reviews |
| `candles` | 10,622 | Market data present |
| `parquet_archive_runs` | 1,888 | Parquet archiving active |

**Parquet Data:**
- 12 symbols archived (BTCUSDT, ETHUSDT, SOLUSDT, ADAUSDT, AVAXUSDT, BNBUSDT, DOGEUSDT, etc.)
- 5 timeframes per symbol (5m, 15m, 1h, 4h, 1d)
- Only May 2026 data (1 month)
- Root: `E:\GenericAgent_crypto\data\parquet\klines\binance_um\`

### 5. Self-Evolution -- IMPLEMENTED BUT UNTESTED

**Flow:**
1. `evaluate_evolution_triggers()` checks: 3 consecutive stop losses OR 10%+ drawdown
2. `run_self_evolution_cycle()` aggregates reviews, checks gates (min_reviews=5, min_symbols=2)
3. Generates candidate patch via LLM or rule-based fallback
4. Creates candidate version, runs shadow test
5. Auto-promote if `allow_auto_promote=True` and shadow passes

**Gates:**
- Minimum 5 reviews required
- Minimum 2 symbols required (anti-overfit)
- Extreme market regime blocks evolution
- Minimum 30 shadow test samples for promotion
- Manual confirmation required by default

---

## Implementation Status Summary

| Subsystem | Code Status | Data Status | Gap |
|---|---|---|---|
| Strategy Scoring | Fully implemented | 2,474 evaluations (all `no_edge`) | No real trade signals generated |
| Trade Review | Fully implemented | 0 reviews | No paper trades to review |
| Loss Classification | Fully implemented (9 categories) | 0 classifications | No trades to classify |
| Daily Review | Fully implemented | 0 reports | No trades to review |
| Shadow Testing | Fully implemented | 0 shadow tests | No candidates tested |
| Self-Evolution | Fully implemented | 0 runs, 0 triggers | No trades to trigger evolution |
| Historical Backtest | Fully implemented | 0 replays run | Never executed |
| Parquet Archive | Fully implemented | 1,888 runs, 12 symbols | Only 1 month of data |
| Version Management | Fully implemented | 3 versions seeded | No version transitions |

## Key Finding

The system is architecturally complete. All five subsystems (analysis, scoring, review, backtest, evolution) are fully implemented with proper safety gates. However, the system has never produced a paper trade -- all 2,474 strategy evaluations resulted in `no_edge` or `monitor_only` decisions, meaning the scoring thresholds (0.80 for paper trade candidate) were never met. The entire review/evolution pipeline is untested because there is no trade data flowing through it.

## Caveats

- The `_compare_strategy_versions()` in `historical_replay.py` uses synthetic adjustments (idx * 0.02) rather than actual per-version scoring -- this is a placeholder, not real strategy comparison
- Parquet data covers only May 2026 (1 month) -- insufficient for meaningful backtesting
- The `mfe` column was expected in queries but the actual column name is `max_favorable_excursion`
- The `read_klines_file` function used by backtest references `plugins/crypto_guard/storage/parquet_archive.py`, not a separate module
