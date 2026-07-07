# Journal - yiyan (Part 1)

> AI development session journal
> Started: 2026-05-27

---



## Session 1: 实现历史回测准入门禁加速自进化反馈

**Date**: 2026-06-02
**Task**: 实现历史回测准入门禁加速自进化反馈
**Branch**: `main`

### Summary

完成 GA CryptoGuard 自进化闭环优化：实现历史回测准入门禁（run_paired_backtest + run_backtest_gate），支持成对比较 active 和 candidate 策略；根据回测结果动态调整在线影子测试样本数（5/30）；集成门禁到 evolution_triggers 和 self_evolution 流程；添加 3 个单元测试验证门禁行为；更新 code-spec 文档。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `4c2ae11` | (see git log) |
| `1022f6b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: 完成性能门禁优化实现

**Date**: 2026-06-02
**Task**: 完成性能门禁优化实现
**Branch**: `main`

### Summary

实现 context_performance_gate 和 symbol_side_cooldown 性能门禁，集成到 controller.py，修复测试问题，所有测试通过

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `95f9c26` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: 修复止血逻辑 + 补集成测试

**Date**: 2026-06-02
**Task**: 修复止血逻辑 + 补集成测试
**Branch**: `main`

### Summary

P1: S/A级信号历史表现差时强制watch-only；P2: 补controller集成测试验证suggested_actions

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `b223f91` | (see git log) |
| `74ab9ed` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: 修复自进化审核通知闭环

**Date**: 2026-06-03
**Task**: 修复自进化审核通知闭环
**Branch**: `main`

### Summary

完成自进化审核通知闭环：verdict_promotion 改走 interactive outbox，新增 approve/reject 按钮回调，卡片补充 backtest 状态和性能对比，同步更新三表状态。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `20426d3` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: 修复 evolution_review 入队逻辑

**Date**: 2026-06-03
**Task**: 修复 evolution_review 入队逻辑
**Branch**: `main`

### Summary

修复 verdict_promotion 入队被 send_message 条件挡住的 bug，补发两个 review_required 通知

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `abae373` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: 修复 evolution_review 必须用 interactive card

**Date**: 2026-06-03
**Task**: 修复 evolution_review 必须用 interactive card
**Branch**: `main`

### Summary

修正清理查询、加校验防线、重新 enqueue 正确的 interactive card

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `c929982` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 7: pending order 生命周期管理 P0

**Date**: 2026-06-03
**Task**: pending order 生命周期管理 P0
**Branch**: `main`

### Summary

实现 TTL 过期、方向冲突取消、stale 清理，清理 8 笔堆积挂单

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `d9b326d` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 8: P0 Account Risk + Trade Quality Gates

**Date**: 2026-06-04
**Task**: P0 Account Risk + Trade Quality Gates
**Branch**: `main`

### Summary

Implemented P0-A through P0-F: account risk guard (hard_risk_off, daily_loss_pause), trade quality gates (late stage, RSI), confirmation gates (order flow, chanlun), trade plan validation, risk-off pending revalidation, and 56 new tests. All 106 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `6093f42` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 9: P1 Shadow Failure + Structured Feedback + LONG Gate

**Date**: 2026-06-04
**Task**: P1 Shadow Failure + Structured Feedback + LONG Gate
**Branch**: `main`

### Summary

Implemented P1-A (shadow failure reflection with draft patches), P1-B (structured feedback with pattern_type column), P1-C (LONG quality gate). 112 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8402aee` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 10: P2 State Diagnostics + Reports + Rules Dry-Run + Feedback TTL

**Date**: 2026-06-04
**Task**: P2 State Diagnostics + Reports + Rules Dry-Run + Feedback TTL
**Branch**: `main`

### Summary

Implemented P2 observability features: state consistency diagnostics (orphan patches, status mismatches, stale shadows, draft limbo), hourly report enhancements (risk_off state, shadow data quality, failure patterns, LONG/SHORT performance), feedback_rules.yaml dry-run evaluation, and feedback TTL/decay system (fresh→decayed→archived with protection for active patch references). All 131 tests pass including 18 new P2 tests.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `240ec80` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 11: P2 Bug Fixes: Schema Health, Diagnostics, TTL, Time Comparison

**Date**: 2026-06-04
**Task**: P2 Bug Fixes: Schema Health, Diagnostics, TTL, Time Comparison
**Branch**: `main`

### Summary

Fixed 6 critical bugs from production audit: schema health check, diagnostics coverage (duplicate patches + active_patch_but_deprecated_version), time comparison consistency, TTL protection scope, shadow data quality classification, and feedback rules loading. All 138 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `e23a70d` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 12: P2 Hotfix: Schema Migration, Time Comparison, Test Fixes

**Date**: 2026-06-05
**Task**: P2 Hotfix: Schema Migration, Time Comparison, Test Fixes
**Branch**: `main`

### Summary

Fixed 5 blocking issues: schema migration, time comparison consistency, schema health protection, test timing, and JSON encoding. All 138 tests pass. Production database verification successful.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `c3885ba` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 13: Account-level feedback rules dry-run

**Date**: 2026-06-05
**Task**: Account-level feedback rules dry-run
**Branch**: `main`

### Summary

Added account-level feedback rules dry-run bridge layer. 4 rules for consecutive_stop_losses and daily_loss_threshold. 1,688 events checked, 3,376 matches. 140 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8f5673e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 14: Account feedback rules dry-run fixes

**Date**: 2026-06-05
**Task**: Account feedback rules dry-run fixes
**Branch**: `main`

### Summary

Fixed lookback_days filtering, added schema health guard, added unique_event_count, infer symbols/sides from all patches. 141 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `02d8a29` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 15: Account feedback gate: shadow/annotate controlled execution

**Date**: 2026-06-05
**Task**: Account feedback gate: shadow/annotate controlled execution
**Branch**: `main`

### Summary

Implemented account feedback gate module, integrated into paper_broker (both order creation paths), added migration, hourly report stats, and 4 tests. 145 tests passed, state consistency = 0.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `ef5f3c4` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 16: P0 feedback gate hotfix — 6 rounds of review, 182 tests, 7 defects closed

**Date**: 2026-06-16
**Task**: P0 feedback gate hotfix — 6 rounds of review, 182 tests, 7 defects closed
**Branch**: `main`

### Summary

P0 反馈门禁热修复：7 个初始缺陷 + 5 轮审查共修复 16 P1 + 11 P2。核心变更：config 层级修正、gate 重写（paired symbol-side、entry_quality fail-closed、would_decide/controlled_projection）、broker 双入口受控执行、schema.sql 同步、migration 顺序修复、opportunity watcher recheck 确定性检查、报表 shadow/controlled 分离、dedupe_key + 唯一索引幂等。测试 145→182。真实库 2 个 stale shadow 运维残留待单独处理。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `6ab9dd3` | (see git log) |
| `fd5daad` | (see git log) |
| `c2f00ec` | (see git log) |
| `d37051c` | (see git log) |
| `eb7d7cb` | (see git log) |
| `cee0a1b` | (see git log) |
| `ef1d67e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 17: P0 market regime engine + 6 integration hotfixes

**Date**: 2026-06-20
**Task**: P0 market regime engine + 6 integration hotfixes
**Branch**: `main`

### Summary

Implemented market regime engine (score_market_regime, apply_regime_gate, 5 loss patterns, daily_reviewer regime feedback) and 6 P0 hotfixes: schema column, shadow/controlled mode, GA decision path wiring, controlled mode adjustments, trade review regime context, consecutive loss check. 219 tests passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8b4f293` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 18: Fix 8 failing tests + real DB migration

**Date**: 2026-06-20
**Task**: Fix 8 failing tests + real DB migration
**Branch**: `main`

### Summary

Fixed 8 failing tests: _insert_closed_trade hours_ago→minutes_ago for time-robustness, updated daily_loss_pause assertion, fixed structured feedback closed_at to use noon-ish times. Executed real DB migration to add market_regime_gate_json column.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `375c0ce` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 19: P1 round 2: 5 regime gate business-logic fixes

**Date**: 2026-06-20
**Task**: P1 round 2: 5 regime gate business-logic fixes
**Branch**: `main`

### Summary

Fixed 5 P1 issues: missing data safety (unknown/unclear), daily review pattern preservation, trade review regime context from gate, controlled mode eligibility downgrade, lookahead time fix. Plus 7 check-found fixes. 224 tests passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `3014ed1` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 20: P2: regime engine consistency - 8 fixes

**Date**: 2026-06-20
**Task**: P2: regime engine consistency - 8 fixes
**Branch**: `main`

### Summary

8 P2 fixes: ETH confirmation, config weights scoring, independent_trend config, risk_multiplier unification, hourly report regime stats, self_evolution JSON parsing, check_schema_health keyword-only, fallback_now visibility. 232 tests passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `5c5373a` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 21: Session 21 - P1/P2 review closure: deterministic PnL, backtest gate, shadow hard gate, evolution evidence

**Date**: 2026-06-21
**Task**: Session 21 - P1/P2 review closure: deterministic PnL, backtest gate, shadow hard gate, evolution evidence
**Branch**: `main`

### Summary

3 rounds of P1/P2 fixes: (1) Daily review deterministic PnL + backtest gate no-silent-failure + shadow real PnL gate + failure reflection win_rate=None + evolution trigger original/latest evidence. (2) sqlite3.Row.get crash fix + active baseline data quality gate + backtest exception rejection + PnL override replaces wrong line + strategy_name top-level compat. (3) Shadow verdict post-LLM hard gate priority chain: merge LLM with fallback, A->B->C->D priority prevents LLM override of data quality gates. 14 new tests, 279 passed. Also archived 06-20-p1-unclear-enforcement (P2 regime consistency fixes done).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `4b148af` | (see git log) |
| `9999bbf` | (see git log) |
| `b24b552` | (see git log) |
| `fa3a29e` | (see git log) |
| `4f22e86` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 22: Session 22 - Position conflict revalidator for open paper trades

**Date**: 2026-06-22
**Task**: Session 22 - Position conflict revalidator for open paper trades
**Branch**: `main`

### Summary

New module paper/position_conflict_revalidator.py that turns passive position conflict alerts into auditable actions. Three-tier action model: (1) S-grade >= 0.85 with strong evidence → conflict_exit close, (2) A/B grade or S without exit criteria → tighten stop to breakeven if profitable, else needs_position_recheck, (3) neutral/mixed → skip. Deduplication by dedupe_key per trade+GA decision+action. Integrated in run_ga_workers.py (post-GA-analysis) and run_scheduler.py (periodic ~10min). Config section: position_conflict in trading_mode.yaml. 8 new tests, 281 total passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `84cb212` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 23: Breakeven Stop-Loss Idempotency — 9 Fixes + Production Recovery

**Date**: 2026-06-27
**Task**: Breakeven Stop-Loss Idempotency — 9 Fixes + Production Recovery
**Branch**: `main`

### Summary

Fixed 5-layer idempotency defect causing 72 duplicate stop_loss_adjustment rows for paper order #1. Root causes: (1) breakeven check read initial_stop_loss instead of current stop_loss, (2) non-atomic SELECT-then-UPDATE in update_paper_order_stop_loss, (3) dual dispatch from _paper_loop + scheduler, (4) enqueue_job instead of enqueue_job_once, (5) alert_outbox dedupe_key unique index covering sent rows. Fixes: atomic conditional UPDATE with direction+status guards returning bool, pending-only outbox dedupe index, time-bucketed periodic alert keys, IntegrityError-safe enqueue_alert, marker-guarded one-shot migration, removed dead _paper_loop thread. Production DB recovered: 17 hourly_summary + 1 paper_order_filled rows restored from duplicate, 71 spurious stop_loss_adjustment rows soft-marked. 431 tests passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `ef2ea8c` | (see git log) |
| `ebc9899` | (see git log) |
| `efd3101` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 24: Hourly report accuracy: batch gate, opportunity classification, deterministic text override, grade hysteresis

**Date**: 2026-06-28
**Task**: Hourly report accuracy: batch gate, opportunity classification, deterministic text override, grade hysteresis
**Branch**: `main`

### Summary

Implemented hourly report market accuracy fix: batch completion gate (analysis_batches table + _await_batch_completion), three-tier opportunity classification (executable/observation/no_edge), deterministic summary validator (report_consistency.py strips FORBIDDEN_EXECUTABLE_PHRASES when execution gates fail), grade hysteresis (grade_with_hysteresis + clamp_grade + SA_MAX_COUNTER_EVIDENCE), 10 P2 diagnostic checks in report_diagnostics.py, drawdown sign convention fix, previous_ga_decision_grade exclude_batch_id, skipped-pending batch completion fix. 491 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `3677a6f` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 25: Fix hourly report accuracy: 15 review issues (P0-7, P1-5, P2-3)

**Date**: 2026-06-28
**Task**: Fix hourly report accuracy: 15 review issues (P0-7, P1-5, P2-3)
**Branch**: `main`

### Summary

Fixed 15 review issues from hourly report accuracy initial implementation. P0: migration ordering (before executescript), batch_symbol_status atomic detail table, batch auto-finish, skipped-pending status=pending, batch_id filter on decisions, precise incomplete detection, grade_with_hysteresis uses current_grade not confidence. P1: 4H range=conflict, rendered_summary preferred, expanded forbidden phrases + is_valid_trade_plan, 5 diagnostic false-positive fixes, configurable timeout. P2: age from analysis_time, abs() drawdown, SMC terminology. 505 tests pass. trellis-check found and fixed 4 additional bugs.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `ba1a1a1` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 26: Fix hourly report accuracy R3: worker blocking, cross-batch query, emergency_down, deterministic summary

**Date**: 2026-06-28
**Task**: Fix hourly report accuracy R3: worker blocking, cross-batch query, emergency_down, deterministic summary
**Branch**: `main`

### Summary

Fixed 12 review issues: P0-1 replace polling with re-enqueue (no worker blocking), P0-2 ROW_NUMBER window query prevents cross-batch contamination, P0-3 risk_gate before hysteresis so emergency_down actually triggers, P1-4 batch status three-way logic, P1-5 no real sleep in tests, P1-6 market_bias alone not confirmation for direction flip, P1-7 is_valid_trade_plan in diagnostics, P1-8 deterministic [观察] summary instead of blacklist cleanup, P1-9 fallback batch uses own time, P2-10 CHECK constraint on batch_symbol_status, P2-11 abs() drawdown in all paths. 518 tests pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `ba134b6` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 27: BTC#9 Trade Gate Final Seal — R3→R13 + Production Migration

**Date**: 2026-07-02
**Task**: BTC#9 Trade Gate Final Seal — R3→R13 + Production Migration
**Branch**: `main`

### Summary

Completed 11 rounds of strict final-review fixes (R3-R13) for BTC#9 'down-pullback LONG false trigger' defect chain. Each round closed specific execution-path holes: R7 snapshot-path coverage, R8 generation-end symbol sync, R9 schema contract, R10 snapshot-authoritative analysis_time, R11 strict-positive-int parser, R12 single-source-of-truth consolidation, R13 docstring accuracy. After R13 approval, executed production migration sequence: stop services → VACUUM INTO backup (642.3 MB) → initialize_database() (0.3s) → Schema Health OK + State Consistency 0 issues + row counts unchanged (ga_decisions=1490, paper_orders=9, paper_trades=4) → restart hub.pyw. btc9_trade_gate_contract_v1 marker written, paper_trade_logs.dedupe_key column + unique partial index added, 6 BTC#9 diagnostic checks all pass on production data. 785 tests passing.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `1092646b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 28: Hourly decision context continuity — R5→R14 final seal + commit

**Date**: 2026-07-07
**Task**: Hourly decision context continuity — R5→R14 final seal + commit
**Branch**: `main`

### Summary

Completed R5→R14 iterative independent review passes on the 07-05-hourly-decision-context-continuity task. R13 fixed a critical P0 regression where controller_decision_from_legacy was writing analysis_time_utc as integer (breaking 13+ SQL consumers in state_consistency.py that use datetime(replace(replace(...))) which returns NULL for integer input) — restored ISO string, added regression test exercising real controller→DB→SQL→diagnostic chain. R14 added replace(replace(...)) wrapper to remaining 2/18 state_consistency.py consumers, fixed fault injector raw_decision_json shape, added explicit parens for operator precedence, corrected misleading defense-in-depth rationale. Final state: P0=0, P1=0, P2=0, Recommended=0. Verification: full suite ×2 (953/953), fault injection 9/9, fresh DB diagnostics all ok=True. Committed as ca5376b. Production migration / marker write / service restart NOT done — to be handled separately via /trellis:crypto-guard-release.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `ca5376b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
