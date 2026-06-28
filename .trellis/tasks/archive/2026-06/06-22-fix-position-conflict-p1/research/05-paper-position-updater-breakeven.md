# Research: paper_position_updater.py Audit

- **Query**: Audit unified breakeven logic changes
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

`_maybe_adjust_stop_to_breakeven` was rewritten to use the same unified breakeven gates as the conflict path.

### Major Changes

1. **Unified breakeven gates**: Replaced the old `breakeven_after_rr: 2.0` threshold with 3 gates from position_conflict config:
   - Gate 1: Holding time >= min_hold_minutes (15 min)
   - Gate 2: current_r >= min_current_r_for_breakeven (0.50)
   - Gate 3: MFE/R >= min_mfe_r_for_breakeven (0.75)
   - Does NOT require reverse_confirmations (routine breakeven doesn't need conflict confirmation)

2. **Uses `initial_stop_loss` instead of `stop_loss`**: Reads `order.get("initial_stop_loss") or order.get("stop_loss")` to avoid using a stop that may have been moved to breakeven.

3. **Uses `initial_risk_usdt` for R computation**: current_r = (current_price - entry) * quantity / initial_risk_usdt.

4. **Uses `market.get("close")` instead of `market.get("price")`**: More consistent with candle data.

5. **Audit trail**: Records audit info (open_time, action_time, holding_minutes, current_r, mfe_r, gate_result) in the job payload.

6. **Config removal**: `breakeven_after_rr: 2.0` was removed from `trading_mode.yaml` risk section.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #15 Pending Order Revalidator (unified breakeven) | PASS | Same gates as conflict path, minus reverse_confirmations |

### Gaps / Issues

- **None identified**. The unified breakeven approach is consistent.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #15 Pending Order Revalidator
