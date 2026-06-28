# Research: position_conflict_revalidator.py Audit

- **Query**: Audit uncommitted changes against spec requirements 1-10
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

The file was heavily modified (+268 lines) with the following major changes:

1. **Passive decision pre-gate** (`_is_passive_decision`): New function that classifies GA decisions as passive (opportunity_watch, monitor_only, risk_check.ok=false, no trade_plan). Passive decisions skip position adjustments entirely, except S-grade strong conflict at deep adverse R which still gets emergency exit.

2. **Unified breakeven gates** (`_should_tighten_stop`): Replaced the old simple floating-profit check with 5 gates:
   - Gate 1: Holding time >= min_hold_minutes (15 min default)
   - Gate 2: 2+ consecutive reverse GA confirmations (from config `reverse_confirmations_for_tighten`)
   - Gate 3: current_r >= min_current_r_for_breakeven (0.50 default)
   - Gate 4: MFE/R >= min_mfe_r_for_breakeven (0.75 default)
   - Gate 5: Not passive (checked upstream by caller)

3. **Actionable-only reverse confirmations** (`_count_consecutive_reverse_confirmations`): Now skips passive decisions (no trade_plan, risk_check failed) but does NOT break the consecutive chain. Only truly non-reverse decisions break it. Also now queries `risk_check_json` and `trade_plan_json` columns.

4. **Proper R computation** (`_compute_current_r_for_trade`): Uses `initial_risk_usdt` if available, falls back to `initial_stop_loss * quantity`. Fail-closed: returns None if neither is available. No longer uses `stop_loss` (which may have been moved to breakeven).

5. **Proper MFE/R computation** (`_compute_mfe_r_for_trade`): Uses `max_favorable_excursion / initial_risk_usdt` instead of current unrealized R. Fail-closed on missing initial_risk_usdt.

6. **Audit trail** (`_execute_stop_tighten`): Records audit info (open_time, action_time, holding_minutes, current_r, mfe_r, gate_result) in the paper_trade_event.

7. **Backfill change**: `_execute_early_exit` now calls `backfill_active_evaluation_pnl_r` instead of `backfill_shadow_evaluation_pnl_r`.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #9 outcome_source filtering | PASS | backfill_active_evaluation_pnl_r uses exact ga_decision_id, LIMIT 1, only is_shadow=0 |
| #15 Pending Order Revalidator | PASS | Multi-dimensional review (passive gate, unified breakeven, reverse confirmations) |
| #5 TTL/event time consistency | PASS | Uses `datetime.now(timezone.utc)` with timezone awareness |

### Gaps / Issues

- None identified. The changes are consistent with the spec's direction on outcome_source filtering (#9) and the multi-dimensional revalidator (#15).

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #9 GA Decision Data Flow, #15 Pending Order Revalidator
