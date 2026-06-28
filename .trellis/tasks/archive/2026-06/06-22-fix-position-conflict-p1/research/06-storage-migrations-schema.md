# Research: storage/migrations.py + schema.sql Audit

- **Query**: Audit database migration idempotency and new table/column additions
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

Three new migration phases added, plus schema changes for shadow_virtual_trades and audit columns.

### Major Changes

1. **`_apply_legacy_fuzzy_migration`**: Marks legacy strategy_evaluations as `outcome_source='legacy_fuzzy'`:
   - All rows WHERE ga_decision_id IS NULL -> legacy_fuzzy
   - All rows WHERE paper_trade_id IS NULL AND outcome_source IS NULL -> legacy_fuzzy
   - Cleans stalled momentum_continuation_long candidates (>48h in 'candidate' status)

2. **`_apply_phase_shadow_vt_v2_migration`**: Adds columns to shadow_virtual_trades:
   - `entry_type`, `opened_at`, `expires_at`
   - `strategy_name` with unique index `idx_shadow_vt_unique`
   - `last_processed_candle_time` for per-candle replay cursor
   - Adds `shadow_virtual_trade_id` to strategy_evaluations

3. **`_apply_candidate_cap_cleanup`**: Rejects excess candidates beyond 5 per strategy_name. Sorted by real_pnl_count DESC, created_at ASC. Three-table sync (strategy_versions, strategy_patches, evolution_triggers). Idempotent: no-op if cap already satisfied.

4. **`_backfill_historical_shadow_pnl_r`** (rewritten): Now uses exact ga_decision_id matching (no +/-1h fuzzy). Only backfills active evaluations (is_shadow=0). Shadow evaluations are NOT backfilled.

5. **Schema additions**:
   - `paper_orders.initial_stop_loss`
   - `paper_trades.initial_stop_loss`, `paper_trades.initial_risk_usdt`
   - `strategy_evaluations.ga_decision_id`, `paper_trade_id`, `shadow_virtual_trade_id`, `outcome_source`
   - New `shadow_virtual_trades` table with full schema
   - `strategy_patches.status` default changed from 'candidate' to 'draft'

6. **`check_schema_health`**: Updated required columns and indexes list.

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #6 Three-table status sync | PASS | candidate_cap_cleanup syncs all three tables |
| #8 Database migration idempotency | PASS | All migrations use IF NOT EXISTS, _add_column checks existence, cap cleanup is no-op if satisfied |
| #9 outcome_source filtering | PASS | legacy_fuzzy migration marks all pre-existing rows |

### Gaps / Issues

- **None identified**. Migrations are idempotent and follow the spec.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #10 Strategy Evolution Pipeline, #14 Shadow Testing Data Quality
