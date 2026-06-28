# Research: CryptoGuard Strategy Improvement Areas

- **Query**: How to improve the strategy system
- **Scope**: internal
- **Date**: 2026-05-27

## Findings

### Current Bottleneck: No Trades Generated

The primary issue is that the scoring system never produces a score above 0.80 (the `paper_trade_candidate` threshold). All 2,474 evaluations scored in the `no_edge` or `monitor_only` range.

**Analysis of scoring path** (from `strategy_scorer.py`):

The base score is 0.50. Maximum theoretical additions:
- Price action (bullish/bearish + BOS/CHoCH): +0.20
- Momentum (direction + healthy quality): +0.15
- Trend stage (early/middle + policy): +0.15
- SMC (liquidity sweep + FVG): +0.10
- Order flow (directional support): +0.08
- Chanlun (class signal): +0.05
- Low contradiction: +0.08

**Maximum theoretical: 0.50 + 0.73 = 1.23** (capped at 0.95)

But in practice, achieving all factors simultaneously is extremely rare. The 0.80 threshold requires most factors to align.

### Specific Code Observations

1. **Strategy_scorer.py line 112**: `strategies[0] if strategies` -- always uses first active strategy regardless of which strategy best matches the snapshot. This means the strategy_name/version in the output may not reflect the actual strategy being scored.

2. **Shadow_testing.py lines 155-173**: The `_stats()` function calculates pseudo-R as `(score - 0.5) * 2`. This is a rough proxy, not a real R-value from price movement. The win_rate threshold of `> 0.1` for pseudo-R means any score above 0.55 counts as a "win."

3. **Evolution_engine.py**: `build_candidate_patch()` always targets `smc_pullback_long` v1.0 with a hardcoded `-0.05` score adjustment. This is a minimal rule-based fallback, not a sophisticated optimization.

4. **Historical_replay.py lines 185-201**: `_compare_strategy_versions()` uses synthetic adjustments (`idx * 0.02`) to differentiate versions. This does not reflect actual scoring differences between strategy configurations.

5. **Daily_reviewer.py lines 171-197**: `_write_skill_memory_updates()` writes identical feedback to all 5 skills regardless of which skill contributed to the loss. This dilutes the signal for targeted improvement.

### Structural Gaps

1. **No signal-to-trade pipeline execution**: The system evaluates signals but does not automatically create paper orders when score >= 0.80. The decision schema has `paper_trade_candidate` as an action, but the actual order creation logic is not connected.

2. **No real backtest R-values**: The backtest uses pseudo-R from confidence scores. A real backtest would need to simulate entry at signal price, stop at invalidation level, and track MFE/MAE through subsequent candles.

3. **No walk-forward optimization**: The self-evolution loop is reactive (triggered by losses). There is no proactive optimization using historical data to find optimal scoring weights.

4. **No multi-strategy comparison**: Only `smc_pullback_long` is actively evaluated. The other two strategies (`pa_breakout_retest_long`, `momentum_continuation_long`) are defined but not scored against snapshots.

5. **No regime-adaptive scoring**: The scoring weights are static. Different market regimes (trending, ranging, volatile) may benefit from different weight distributions.

### What Exists vs What's Missing

| Capability | Status | Detail |
|---|---|---|
| Multi-factor scoring | Exists | 7 factors, weighted sum |
| Strategy versioning | Exists | Active/candidate/deprecated lifecycle |
| Shadow testing | Exists | Compare active vs candidate evaluations |
| Loss classification | Exists | 9 categories with improvement suggestions |
| Daily review | Exists | Batch review + Feishu summary |
| Self-evolution | Exists | Review aggregation -> candidate patch -> shadow test |
| Historical replay | Exists | Candle-by-candle with no-lookahead |
| Parquet archival | Exists | 1,888 runs, 12 symbols |
| Real trade simulation | Missing | No entry/exit/stop simulation in backtest |
| Walk-forward optimization | Missing | No rolling window validation |
| Multi-strategy scoring | Missing | Only first active strategy is scored |
| Regime-adaptive weights | Missing | Static scoring weights |
| Auto paper order creation | Missing | Decision -> order pipeline not connected |
| Real R-value tracking | Missing | Only pseudo-R from confidence |
