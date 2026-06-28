# Research: storage/repository.py Audit

- **Query**: Audit repository changes for backfill, shadow_virtual_trades CRUD, and outcome_source
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Major refactoring of backfill logic and addition of shadow_virtual_trades CRUD operations.

### Major Changes

1. **`backfill_active_evaluation_pnl_r`** (replaces `backfill_shadow_evaluation_pnl_r`): Uses exact ga_decision_id matching with LIMIT 1. Only updates is_shadow=0 rows where outcome_source='pending_outcome'. Sets outcome_source='real_pnl', ga_decision_id, paper_trade_id.

2. **`create_shadow_virtual_trade`**: Idempotent creation (checks existing by strategy_name, candidate_version, ga_decision_id). Sets status based on entry_type (market -> open, limit/trigger -> pending_entry). Computes expires_at.

3. **`update_shadow_virtual_trade_prices`**: Updates unrealized PnL, MFE, MAE for open VTs.

4. **`close_shadow_virtual_trade`**: Closes VT and backfills strategy_evaluations with pnl_r and correct outcome_source:
   - `activation_ambiguous_path` or `ambiguous_path` close_reason -> outcome_source='ambiguous_path'
   - All other close reasons -> outcome_source='real_pnl'

5. **`update_shadow_virtual_trade_status`**: Transitions VT status with event_time support.

6. **`save_strategy_evaluation`**: Now saves ga_decision_id, outcome_source, paper_trade_id, shadow_virtual_trade_id. Active evaluations start as 'pending_outcome'.

7. **`create_paper_order`**: Now saves initial_stop_loss (= stop_loss at creation).

8. **`create_paper_trade`**: Now saves initial_stop_loss and initial_risk_usdt (computed via `_compute_initial_risk_usdt`).

9. **`save_strategy_patch_candidate`**: Now accepts `status` parameter (default 'draft').

10. **`save_strategy_version`**: Now accepts 'draft' as valid status.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #6 Three-table status sync | PASS | VT lifecycle independent from paper_trades |
| #9 outcome_source filtering | PASS | backfill_active_evaluation_pnl_r uses exact ga_decision_id, LIMIT 1 |
| #10 close_shadow_virtual_trade sets correct outcome_source | PASS | ambiguous_path for ambiguous close reasons, real_pnl otherwise |

### Gaps / Issues

- **None identified**.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline, #14 Shadow Testing Data Quality
