# Research: strategy/self_evolution.py Audit

- **Query**: Audit self-evolution patch schema validation, draft status, and candidate cap
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Added patch schema validation, draft status flow, candidate cap enforcement, and context-aware patch building.

### Major Changes

1. **`build_candidate_patch`** (in evolution_engine.py): Now uses `classify_trade` to determine loss pattern and builds conditional score_adjustments with 'when' clauses. Returns None for unknown patterns (needs_manual_classification). Accepts strategy_name parameter.

2. **`_validate_patch_schema`**: Validates LLM-generated patch before persisting. Checks strategy_name presence, score_adjustments structure, risk_controls type.

3. **`_validate_score_adjustments`**: Recursive validation of score_adjustments structure. Supports flat float, {value, when} dict, named adjustments, and nested_score_adjustments.

4. **Draft status flow**: When `allow_auto_promote_to_candidate=false` (config), patches are created as 'draft' and skip backtest gate/shadow testing. Returns 'draft_pending_approval' status.

5. **Candidate cap enforcement**: After creating new candidate, `_enforce_candidate_cap` is called within the same transaction.

6. **Backtest failure handling**: Expanded to cover backtest_exception, no_lookahead_failed, and data_missing cases.

7. **Backtest pass transition**: After backtest passes or is skipped, candidate is transitioned from 'candidate' to 'shadow_testing'.

8. **`aggregate_review_attribution`**: Now picks a representative trade (most negative pnl_r) for context-aware patch building.

9. **`_latest_candidate_version`**: Now uses `_designate_primary_candidate` for multi-candidate support.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #6 Three-table status sync | PASS | Candidate cap enforces three-table sync |
| #10 Strategy Evolution Pipeline | PASS | Backtest gate -> shadow_testing transition |

### Gaps / Issues

- **None identified**.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline
