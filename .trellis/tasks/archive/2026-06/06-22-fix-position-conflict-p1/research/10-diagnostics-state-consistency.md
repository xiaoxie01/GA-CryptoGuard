# Research: diagnostics/state_consistency.py Audit

- **Query**: Audit diagnostic rule completeness (12+ check types)
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Added 14 new diagnostic check types, bringing total from 6 to 20 check types.

### New Check Types

| # | Check Function | Type | Severity | Description |
|---|---------------|------|----------|-------------|
| 7 | `_check_candidate_queue_overflow` | candidate_queue_overflow | warning | >5 shadow_testing candidates per strategy_name |
| 8 | `_check_stalled_candidate` | stalled_candidate | warning | 'candidate' status >48h without transitioning |
| 9 | `_check_no_real_pnl_progress` | no_real_pnl_progress | warning | No real PnL samples in 7+ days |
| 10 | `_check_strategy_name_mismatch` | strategy_name_mismatch | error | Patch strategy_name differs from trigger |
| 11 | `_check_zero_quantity_virtual_trades` | zero_quantity_virtual_trade | error | Open VT with quantity <= 0 |
| 12 | `_check_zero_risk_virtual_trades` | zero_risk_virtual_trade | error | Open VT with initial_risk_usdt <= 0 |
| 13 | `_check_three_table_status_mismatch` | three_table_status_mismatch | error/warning | Inconsistent trigger/patch/version statuses |
| 14 | `_check_closed_vt_missing_real_pnl_eval` | closed_vt_missing_real_pnl | warning/error | Closed VT with no real_pnl evaluation or pnl_r mismatch |
| 15 | `_check_ambiguous_vt_missing_ambiguous_eval` | ambiguous_vt_missing_ambiguous_eval | error | Ambiguous VT with wrong/missing outcome_source |
| 16 | `_check_ambiguous_eval_not_real_pnl` | ambiguous_eval_not_real_pnl | warning | Ambiguous eval with VT close_reason mismatch |
| 17 | `_check_duplicate_vt_per_candidate_decision` | duplicate_vt_per_candidate_decision | error | >1 VT per (strategy_name, candidate_version, ga_decision_id) |
| 18 | `_check_closed_vt_still_processed` | closed_vt_still_processed | warning | Closed VT with non-null last_processed_candle_time |
| 19 | `_check_cursor_regression` | cursor_regression | error | last_processed_candle_time < created_at |
| 20 | `_check_illegal_status_transitions` | illegal_status_transition | error | VT with unknown status value |

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #7 Complete diagnostic rules (12+ check types) | PASS | Now 20 check types, well above the 12+ requirement |

### Gaps / Issues

- **None identified**. All 14 new checks are well-structured with proper severity levels and suggested actions.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #18 State Consistency Diagnostics
