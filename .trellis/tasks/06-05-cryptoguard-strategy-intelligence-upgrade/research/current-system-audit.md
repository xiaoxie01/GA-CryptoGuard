# CryptoGuard Current System Audit

Date: 2026-06-05

## Audit Objective

Determine whether the fixes completed through P0/P1/P2 and the account feedback gate
fully resolve the original product problem:

> The paper account keeps losing. Entries and stops appear to be placed around prior
> highs/lows without sufficient price-action, order-flow, Chanlun, momentum, trend, or
> reversal confirmation. Self-evolution and shadow testing do not appear to learn from
> historical market data.

## Executive Finding

The recent work materially improved safety, state consistency, observability, and loop
reliability. It did not yet establish a proven positive-expectancy trading system.

The system is now better at:

- reducing account risk after drawdown;
- rejecting or downgrading obviously weak trades;
- expiring and revalidating pending orders;
- preventing pseudo-R-only shadow candidates from promotion;
- recording loss feedback and maintaining evolution state;
- detecting historical loss patterns in shadow mode.

The system is still weak at:

- generating entries from a complete, named setup and a closed-candle trigger;
- deriving stops from the setup's structural invalidation rather than price-distance
  repair;
- using order flow, Chanlun, momentum, and trend as entry construction inputs;
- replaying the real candidate rule set across multi-timeframe historical data;
- proving robustness out of sample and across regimes;
- converting a loss pattern into a causal, typed strategy change.

## Confirmed Strategy-Construction Defects

### 1. Trade-plan generation is level-first, not setup-first

`plugins/crypto_guard/reasoning/ga_judge.py::_build_trade_plan()` currently:

- selects recent swing lows/highs, support/resistance, or range boundaries as stop
  candidates;
- selects support/resistance, FVG midpoint, range breakout level, or range midpoint as
  entry candidates;
- creates a synthetic invalidation level when structural distance is too small;
- emits fixed 1.5R and 2.5R targets regardless of nearby liquidity or structure.

This can produce a syntactically valid plan without proving:

- which setup is being traded;
- why the level should hold or break;
- whether a reversal/continuation trigger has closed;
- whether order flow confirms execution timing;
- whether the selected stop invalidates the actual trade thesis.

### 2. Synthetic repair can hide invalid analysis

When the structural stop is on the wrong side or too close, the current code can:

- move it by 0.1%;
- fall back to `entry +/- min_risk`;
- use a range midpoint-derived fallback.

This changes an invalid trade thesis into an apparently valid trade plan. The correct
behavior is to return watch/no-trade when no defensible structural invalidation exists.

### 3. Analysis modules are mainly filters

Order flow, Chanlun, momentum, trend stage, BTC context, RSI, and late-stage checks have
been added to the risk path. They primarily veto or downgrade an already generated
plan. They do not yet collaborate to construct:

- setup type;
- entry zone;
- trigger condition;
- invalidation structure;
- target/liquidity map;
- order expiry.

### 4. Entry confirmation is not a strong contract

`entry_trigger_confirmation` is checked for presence, but the deterministic trade-plan
builder does not consistently produce a structured, auditable confirmation object tied
to a closed candle and evidence references.

### 5. LONG and SHORT need symmetric quality logic

The LONG quality gate was added because LONG performance was weak. This is useful for
containment, but the core setup engine must be symmetric. Direction-specific historical
performance can tighten confirmation, but the architecture must not assume that only
one side is defective.

## Confirmed Backtest and Evolution Defects

### 1. Candidate comparison is too narrow

`run_paired_backtest()` currently applies a score adjustment to the same decision
pipeline. This is useful only for score-based patches. It cannot validate:

- new setup rules;
- different entry triggers;
- different structural stops;
- risk-control changes;
- symbol/side/regime-specific conditions.

### 2. Replay model is simplified

Current replay limitations include:

- single-timeframe snapshot construction in the paired path;
- simplified market-regime classification;
- fixed forward-candle window;
- limited fill modeling;
- no complete fee/slippage/funding model;
- no walk-forward or dedicated out-of-sample split;
- insufficient proof that the same live decision contract is executed in replay.

### 3. Tests demonstrate code behavior, not profitability

Passing unit and integration tests proves that gates, migrations, and state transitions
work. It does not prove positive expectancy, robustness, or resistance to overfitting.

### 4. Evolution is operational but not fully causal

The repaired loop can now move through trigger, candidate, backtest/shadow, review, and
feedback states. However, candidate changes are not yet required to contain a typed,
replayable rule operation linked to:

`loss evidence -> failure pattern -> hypothesis -> changed rule -> expected effect`.

Without that chain, the system can evolve metadata or scores without improving market
understanding.

## Immediate Account Feedback Gate Defects

The latest account feedback gate needs a hotfix before controlled execution:

1. `trading_mode.yaml` hierarchy is broken:
   - `evolution.min_r_count_for_performance_gate`
   - `evolution.online_shadow`
   - `evolution.stale_cleanup`
   are currently nested under `account_feedback_rules`.
2. Not every gate check is persisted, so shadow reporting cannot measure activation,
   pass, fail, or non-applicable rates accurately.
3. Missing `entry_quality` currently passes the quality threshold.
4. Affected symbols and sides are stored as independent sets, allowing false
   symbol-side combinations.
5. Controlled decisions are logged but not enforced in the broker path.
6. `schema.sql` and schema-health checks do not fully cover
   `ga_decisions.account_feedback_gate_json`.
7. Integration tests do not prove that the real broker path persists and applies the
   gate result.

## What Must Be Preserved

- GenericAgent remains the master controller and final analysis coordinator.
- Simulation-only execution; no live trading.
- Human approval remains mandatory for strategy activation.
- Closed-candle and no-lookahead guarantees.
- Existing account risk-off, daily pause, performance gates, pending-order lifecycle,
  shadow data-quality checks, failure reflection, diagnostics, and audit history.
- No physical deletion of strategy/evolution audit records.

## Audit Conclusion

The current codebase has completed the safety-and-observability foundation. The next
qualitative leap must replace level-first plan generation with setup-first reasoning,
then make historical replay execute the same typed strategy rules used online. Further
gate accumulation without this change will reduce trading frequency but will not create
reliable alpha.
