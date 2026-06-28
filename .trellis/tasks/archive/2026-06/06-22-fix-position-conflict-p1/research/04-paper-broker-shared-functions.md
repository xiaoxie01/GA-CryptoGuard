# Research: paper_broker.py Audit

- **Query**: Audit shared compute_fill_price/compute_position_size and backfill changes
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Added shared sizing/fill functions and changed backfill target from shadow to active evaluations.

### Major Changes

1. **`compute_position_size`** (new): Shared function computing `quantity = (account_balance * risk_pct) / abs(entry - stop)` and `initial_risk_usdt = account_balance * risk_pct`. Reused by both paper_broker and shadow_virtual_trade creation.

2. **`compute_fill_price`** (new): Shared function applying slippage to market orders. For limit/trigger orders, no slippage. For market: LONG fills at `entry * (1 + slippage)`, SHORT at `entry * (1 - slippage)`.

3. **`fill_order_if_triggered`**: Now uses `compute_fill_price` and `compute_position_size`. Size is computed AFTER fill price is determined (fill-before-size order per spec #2).

4. **`close_trade_if_needed`**: Changed from `backfill_shadow_evaluation_pnl_r` to `backfill_active_evaluation_pnl_r`.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #2 Shared compute_fill_price + compute_position_size, fill-before-size order | PASS | Both functions shared, size computed after fill price |
| #9 outcome_source filtering | PASS | backfill_active_evaluation_pnl_r uses exact ga_decision_id, LIMIT 1, only is_shadow=0 |

### Gaps / Issues

- **None identified**.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline
