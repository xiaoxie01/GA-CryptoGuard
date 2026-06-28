# Research: Remaining Files Audit

- **Query**: Audit trade_reviewer.py, daily_reviewer.py, hourly_report.py, run_ga_workers.py, run_scheduler.py, service_manager.py, version_manager.py, historical_replay.py, config files, test_smoke.py
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### trade_reviewer.py

- Wraps patch + version + cap in explicit BEGIN/COMMIT
- Creates candidates with status='candidate'
- After backtest passes/skipped, transitions to 'shadow_testing'
- No scoring changes -> transitions directly to shadow_testing
- **Status**: PASS, consistent with spec

### daily_reviewer.py

- `_evolution_status_for_report`: Now filters by `outcome_source='real_pnl'` for real_pnl_count and avg_r
- Added pseudo_r_count tracking
- Added win_rate computation from real PnL evaluations only (requires >=5 samples)
- Added global totals (total_triggers, total_open_triggers, total_patches)
- **Status**: PASS, outcome_source filtering applied

### hourly_report.py

- `_fetch_shadow_data_quality`: Now uses `outcome_source='real_pnl'` instead of just `pnl_r IS NOT NULL`
- Pseudo count uses `outcome_source != 'real_pnl' OR outcome_source IS NULL`
- **Status**: PASS, outcome_source filtering applied

### run_ga_workers.py

- `_build_evolution_status_text`: Completely rewritten to use strategy_evaluations (is_shadow=1) for per-patch stats
- Shows data quality breakdown: total/real_pnl/pseudo_r samples
- Shows win_rate from real PnL only
- Shows backtest status, effective_min_samples, blocking reason
- Added `_fmt_utc8` helper for UTC+8 time display
- Added `_parse_json_list` helper
- **Status**: PASS, outcome_source filtering applied

### run_scheduler.py

- Added `shadow_virtual_trade_update` job handler
- **Status**: PASS, consistent with scheduler.yaml

### service_manager.py

- `_paper_loop`: Now also runs `update_shadow_virtual_trades` after paper position updates
- `_due_scheduler_jobs`: Added `shadow_virtual_trade_update` every minute
- `_tick_key`: Added tick key for shadow_virtual_trade_update (60s)
- **Status**: PASS, consistent with scheduler config

### version_manager.py

- `create_candidate_version_from_patch`: Now accepts `initial_status` parameter (default 'draft')
- `_candidate_config`: Now accepts `initial_status` parameter
- **Status**: PASS, supports draft status flow

### historical_replay.py

- Added `_evaluate_conditional_adjustment`: Evaluates when-conditioned score adjustments per candle
- Added `_matches_when`: Checks context against when-condition dict (supports side, market_phase, trend_stage, entry_type, market_bias)
- `run_paired_backtest`: Now accepts `candidate_patch` (full dict) instead of `candidate_score_adjustment` (float)
- `_build_signal`: Now includes side, entry_type, trend_stage in signal dict
- Result includes `when_rule_stats` for trigger count tracking
- **Status**: PASS, conditional adjustment evaluation per spec

### config/scheduler.yaml

- Added `shadow_virtual_trade_update` job (every minute)
- **Status**: PASS, consistent with service_manager and run_scheduler

### config/trading_mode.yaml

- Removed `breakeven_after_rr: 2.0` from risk section
- Added `allow_auto_promote_to_candidate: false` to evolution section
- Added unified breakeven gates to position_conflict section (min_hold_minutes, min_current_r_for_breakeven, min_mfe_r_for_breakeven, reverse_confirmations_for_tighten)
- **Status**: PASS, config changes match code changes

### test_smoke.py

- Massive expansion (+2402 lines) with new test categories:
  - Category 8: Final Review Assertion Tests (one_trade_one_active_evaluation, trade_not_broadcast_to_shadow, monitor_only_candidate_not_inherit_active_loss, active_one_real_sample_blocks_verdict, etc.)
  - Updated existing tests to include outcome_source, ga_decision_id, paper_trade_id, shadow_virtual_trade_id fields
  - Updated _stats test rows to include audit fields
  - Updated shadow verdict test to use ga_decision_id matching for paired comparison
  - Updated self_evolution test to pass allow_auto_promote=True
- **Status**: PASS, tests cover the new outcome_source filtering and VT lifecycle

### Gaps / Issues

- **None identified** across all remaining files.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #8 Scheduler Config Sync, #9 GA Decision Data Flow, #10 Strategy Evolution Pipeline, #14 Shadow Testing Data Quality, #19 Hourly Report Enhancements
