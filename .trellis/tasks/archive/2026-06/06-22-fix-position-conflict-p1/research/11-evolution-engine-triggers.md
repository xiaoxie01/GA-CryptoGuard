# Research: review/evolution_engine.py + evolution_triggers.py Audit

- **Query**: Audit evolution engine conditional patches and trigger candidate creation
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

evolution_engine.py was completely rewritten to build context-aware conditional patches. evolution_triggers.py was updated for atomic candidate creation with cap enforcement.

### evolution_engine.py Changes

1. **`build_candidate_patch`**: Now uses `classify_trade` to determine loss pattern. Each pattern produces distinct conditional adjustments:
   - wrong_direction: -0.08 on SMC orderflow direction (with side + trend_stage when)
   - entry_too_late: -0.05 requiring momentum confirmation
   - entry_chasing: -0.06 entry timing penalty
   - late_trend_chasing: -0.07 late entry penalty
   - stop_loss_too_tight: -0.03 wider stop required
   - entry_too_early: -0.04 zhongshu confirmation required
   - take_profit_too_far: -0.02 TP adjustment
   - macro_selloff_long_trap_loss: -0.10 risk_off LONG pause
   - macro_rebound_short_squeeze_loss: -0.10 risk_on SHORT pause
   - counter_regime_entry_loss: -0.08 counter regime penalty
   - market_regime_mismatch: -0.06 regime mismatch penalty
   - unknown: returns None (needs_manual_classification)

2. **`_build_conditional_adjustments`**: Builds pattern-specific {value, when} dicts with real context conditions (side, trend_stage, market_phase).

### evolution_triggers.py Changes

1. **`_record_trigger_and_candidate`**: Now wraps trigger + patch + version + cap in explicit BEGIN/COMMIT. Creates candidates with status='candidate' (not 'shadow_testing'). After backtest passes/skipped, transitions to 'shadow_testing' and updates patch status.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #6 Three-table status sync | PASS | Atomic transaction with cap enforcement |
| #10 Strategy Evolution Pipeline | PASS | Backtest gate -> shadow_testing transition |

### Gaps / Issues

- **None identified**.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline
