# Market Regime Engine + Counter-Regime Gate — Implementation Plan

## Context

Current `market_regime_engine.py` only classifies extreme volatility (ATR percentile, wick count). It doesn't analyze BTC/ETH bias, market phase, breadth, or relative strength. This plan adds a full market regime scoring system that integrates into the GA decision pipeline, risk gates, loss classification, shadow testing, and reporting.

## P0: Core Engine + Soft Gate + Loss Patterns + Tests

### P0.1: Rewrite `market_regime_engine.py`

**File**: `plugins/crypto_guard/analysis/market_regime_engine.py`

New function: `score_market_regime(repo, symbol, analysis_time_utc) -> dict`

Inputs:
- BTCUSDT, ETHUSDT Binance futures K-lines: 15m, 1h, 4h (from repo.get_candles)
- Current symbol's 15m, 1h, 4h K-lines
- Watchlist breadth from active_analysis_symbols

Output `market_regime_json`:
- btc_bias: bullish/bearish/range/transition
- eth_bias: bullish/bearish/range/transition
- market_phase: risk_on/risk_off/rebound/selloff/chop
- breadth_score: -1 to +1
- volatility_state: normal/elevated/spike
- symbol_relative_strength: strong/weak/neutral
- regime_alignment: aligned/counter_regime/independent_trend/unclear
- confidence_adjustment: -0.15 to +0.10
- risk_multiplier: 0.5/0.75/1.0
- require_stronger_confirmation: bool
- reasons: list[str]

Keep existing `classify_market_regime()` for backward compat but rename to `classify_extreme_regime()`.

### P0.2: Counter-Regime Gate in `risk_engine.py`

**File**: `plugins/crypto_guard/risk/risk_engine.py`

New function: `apply_regime_gate(decision, market_regime) -> dict`

Logic:
- If market_phase is rebound/risk_on and side is SHORT → counter_regime
- If market_phase is selloff/risk_off and side is LONG → counter_regime
- Unless regime_alignment is independent_trend

Counter-regime effects:
- Grade downgrade: S→A, A→B, B→C
- confidence -0.10
- risk_multiplier 0.5
- min_rr raised to 2.0
- Only allow trigger/retest order types
- If same-side consecutive losses >= 2 today → watch_only

### P0.3: Loss Pattern Expansion

**File**: `plugins/crypto_guard/review/loss_classifier.py`

Add new patterns:
- market_regime_mismatch_short_loss
- market_regime_mismatch_long_loss
- counter_regime_entry_loss
- macro_rebound_short_squeeze_loss
- macro_selloff_long_trap_loss

**File**: `plugins/crypto_guard/review/daily_reviewer.py`

Enhance `_write_skill_memory_updates()` to write structured fields:
- market_phase, regime_alignment, btc_bias, eth_bias, symbol_relative_strength
- suggested_adjustment_json with specific adjustments

### P0.4: Config

**File**: `plugins/crypto_guard/config/trading_mode.yaml`

Add `market_regime` section under existing config.

### P0.5: Tests

**File**: `plugins/crypto_guard/tests/test_smoke.py`

Add tests:
1. BTC/ETH rebound → SHORT downgraded
2. BTC/ETH selloff → LONG downgraded
3. independent_trend bypass
4. Consecutive same-side losses → watch_only
5. market_regime_mismatch_short_loss written to skill_feedback_memory
6. Shadow evaluation records market_regime context

## P1: Backtest Regime Bucketing + Shadow Verdict

### P1.1: Regime-Aware Backtest

**File**: `plugins/crypto_guard/backtest/historical_replay.py`

Enhance `run_paired_backtest()` to tag each simulated trade with market_regime context.

### P1.2: Shadow Verdict by Regime

**File**: `plugins/crypto_guard/strategy/shadow_testing.py`

Add regime-bucketed stats:
- avg_r_by_market_phase
- win_rate_by_market_phase
- counter_regime_trade_count
- avoided_loss_count
- missed_winner_count

### P1.3: Review-Required Gate

If patch reduces stop losses but misses too many winners → review_required

## P2: Reporting + Order Lifecycle

### P2.1: Hourly Report Enhancement

**File**: `plugins/crypto_guard/notify/hourly_report.py`

Add:
- Market regime section (BTC bias, ETH bias, market_phase, breadth_score)
- New order regime alignment status
- Today's stop losses by regime mismatch
- LONG/SHORT performance by market_phase
- Slowdown measures after consecutive losses

### P2.2: Daily Report Enhancement

Add:
- Top failure patterns
- market_regime_mismatch count
- Orders avoided by regime gate
- Missed winners (to prevent over-conservatism)

### P2.3: Pending Order Regime Integration

**File**: `plugins/crypto_guard/paper/pending_revalidator.py`

Add rules:
- If order direction conflicts with latest market_regime → convert_to_watch or cancel
- If order was aligned but now counter_regime → needs_recheck
- If position open and counter_regime but profitable → tighter_management
- If position open and counter_regime and MFE retraced >50% → reduce risk or move SL
