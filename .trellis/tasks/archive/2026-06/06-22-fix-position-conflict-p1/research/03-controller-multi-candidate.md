# Research: ga_master/controller.py Audit

- **Query**: Audit multi-candidate shadow evaluation and virtual trade creation
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

The controller was refactored from single-candidate to multi-candidate shadow evaluation (+298 lines).

### Major Changes

1. **`_find_shadow_candidates`** (replaces `_find_shadow_candidate`): Returns ALL shadow_testing versions for a strategy, newest first. Only returns 'shadow_testing' status (not 'candidate').

2. **`_create_virtual_trade_for_candidate`**: Creates a `shadow_virtual_trade` for each candidate that has a trade_plan. Uses shared `compute_fill_price` and `compute_position_size` from paper_broker. Applies candidate_patch trade_plan overrides (entry_price_adjustment, stop_loss_adjustment, take_profit_adjustment).

3. **`_adjustment_matches_context`**: Checks if a conditional adjustment's 'when' clause matches current context (side, market_phase, trend_stage, entry_type).

4. **`_evaluate_shadow_candidate`**: Now supports conditional `{value, when}` score_adjustments. Merges candidate_patch trade_plan overrides into shadow_decision.

5. **Main loop in `analyze_symbol`**: Iterates all shadow_candidates, creates virtual trades for candidates with trade_plan_available/opportunity_watch decisions, sets appropriate `outcome_source`:
   - `executed_virtual_trade` for trade_plan_available
   - `avoided_trade` for monitor_only when active entered
   - `no_entry` for monitor_only when active also didn't enter
   - `invalidated` for other decisions

6. **Error handling**: Shadow evaluation failures now log with exc_info instead of silently passing.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #1 1 GA decision -> >=1 shadow eval + >=1 shadow VT per candidate | PASS | Multi-candidate loop creates VT per candidate |
| #2 Shared compute_fill_price + compute_position_size, fill-before-size order | PASS | fill_price computed first, then sizing against fill price |
| #6 Three-table status sync | PASS | VT creation uses independent shadow_virtual_trades table |
| #9 outcome_source filtering | PASS | Sets outcome_source per candidate decision type |

### Gaps / Issues

- **None identified**. The multi-candidate approach with per-candidate virtual trades is consistent with spec #1.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #1 Analysis Cycle, #9 GA Decision Data Flow, #10 Strategy Evolution Pipeline
