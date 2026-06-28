# Research: strategy/shadow_testing.py Audit

- **Query**: Audit shadow testing stats, verdict, paired comparison, and candidate cap changes
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Major expansion (+599 lines) adding paired comparison, candidate cap enforcement, stale candidate rejection, and outcome_source-aware stats.

### Major Changes

1. **`record_shadow_evaluation`**: Now accepts ga_decision_id, outcome_source, paper_trade_id, shadow_virtual_trade_id parameters.

2. **`run_shadow_test`**: Added paired comparison gate before LLM verdict:
   - `_run_paired_comparison`: Matches active and candidate evaluations on same ga_decision_id, compares pnl_r side-by-side
   - `_paired_real_pnl_samples`: Counts ga_decision_ids where both active and candidate have real_pnl
   - LLM result now only used for explanation/notes, verdict is deterministic
   - New hard gates: active_baseline_insufficient, paired_samples_insufficient (<3), paired_underperformance
   - Stale pseudo-only candidates (7+ days with zero real_pnl) are rejected instead of kept in 'running'

3. **`_stats`**: Completely rewritten to use outcome_source filtering:
   - Only counts as real_pnl when ALL conditions met: pnl_r IS NOT NULL AND outcome_source='real_pnl' AND ga_decision_id IS NOT NULL AND (shadow_virtual_trade_id IS NOT NULL for shadow OR paper_trade_id IS NOT NULL for active)
   - legacy_fuzzy, avoided_trade, pending_outcome, ambiguous_path, NULL outcome_source all fall through to pseudo-R
   - Added legacy_fuzzy_samples count

4. **`_designate_primary_candidate`**: Returns primary candidate per strategy_name (most real_pnl_samples DESC, created_at ASC).

5. **`_enforce_candidate_cap`**: Rejects excess candidates beyond max_candidates (5). Three-table sync. Sorted by real_pnl_count DESC, created_at ASC.

6. **`_soft_reject_unknown_candidates`**: Rejects candidates with loss_pattern='unknown' in their patch.

7. **`run_shadow_verdict_runner`**: Now enforces candidate cap, soft-rejects unknown candidates, rejects stale zero-real-PnL candidates (7+ days), designates primary candidates, and guards against already-rejected candidates.

8. **`run_backtest_gate`**: Now passes `candidate_patch` (full dict) instead of `candidate_score_adjustment` (float). Handles backtest exceptions. Returns `skipped:data_unavailable` for no valid results.

9. **`_extract_score_adjustment`**: Now only sums unconditional adjustments. Conditional adjustments with 'when' clauses are NOT summed.

10. **`_promote_draft_to_candidate`**: New function for manual draft-to-candidate promotion.

11. **`_maybe_generate_draft_patch`**: Now creates patches with status='draft' instead of 'shadow_testing'.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #1 1 GA decision -> >=1 shadow eval per candidate | PASS | Multi-candidate loop in controller |
| #9 outcome_source filtering everywhere | PASS | _stats uses strict outcome_source='real_pnl' filtering |
| #10 close_shadow_virtual_trade sets correct outcome_source | PASS | close_shadow_virtual_trade in repository sets real_pnl or ambiguous_path |
| #14 Shadow Testing Data Quality | PASS | Pseudo-only blocking, stale rejection, data_quality tracking |

### Gaps / Issues

- **None identified**.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline, #14 Shadow Testing Data Quality
