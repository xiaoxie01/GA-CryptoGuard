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


## Session 29: 07-09 LLM breaker over-trigger follow-up R1-R6 final seal

**Date**: 2026-07-10
**Task**: 07-09 LLM breaker over-trigger follow-up R1-R6 final seal
**Branch**: `main`

### Summary

Resolved 4 P0/P1 defects + 1 P2 in LLM breaker/agent-judge/hourly-report path surfaced in post-commit review of 07-09-llm-schema-repair-breaker-tuning.

R1: removed crypto_guard_noop placeholder tool from _call_ga_llm, clear session.tools unconditionally, probe stop_reason to classify llm_tool_call_no_text.

R2: unwrap wrapped-decision object before schema validation, conflict fails closed.

R3: added llm.circuit_breaker.min_rate_samples config (default 5), rate-based open now requires min samples so 3-sample 67% no longer kills 10-symbol batch. CRITICAL P0: production worker entrypoint run_ga_workers.process_job was missed in first round - controller never saw the configured value because worker pre-populated _batch_breakers; now passes kwarg.

R4: repairable events tracked separately, do not push into rate window.

R5/R6: hourly report distinct banner for llm_tool_call_no_text, added to _RETRYABLE_CATEGORIES and consecutive_infra_failures.

Tests: 18 new follow-up tests, worker-path test patches source module with non-default min_rate_samples=7 (revert-fail verified).

Verification: focused 56 passed, full 1024 passed/1 pre-existing mark_price failure (wall-clock-dependent test, unrelated, documented in final-seal.md), fault inject 16/16, fresh DB all green.

No production migration needed. Code requires service restart (not performed). Excluded from commit: binance_rest.py (pre-existing BASE_URL debug edit), .claude/.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `301504ce` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 30: CryptoGuard 07-10 LLM fair scheduling + context continuity — R10 terminal-repair round (commit)

**Date**: 2026-07-16
**Task**: CryptoGuard 07-10 LLM fair scheduling + context continuity — R10 terminal-repair round (commit)
**Branch**: `main`

### Summary

Closed the R10 terminal-review rejection: 3 NEW findings (2 P1 + 1 P2) - lease leak on init/recovery failure, current-symbol partial skill log stuck prepared, attempt_id races under concurrency - plus the 2 reviewer P2 (schema-health coverage for _analysis_attempt_counter, stale docstring) and the ShadowVTLifecycleTest fixture regression P2-1 caused. All RED-first + revert-fail proven; independent crypto-guard-reviewer R10 re-pass returned PASS zero findings. Verified: focused 44 passed, broader 237 passed, two consecutive full-suite 1229 passed each (0 failures/0 skips), git diff --check exit 0, AC15 zero-diff on hub.pyw/frontends/fsapp.py/binance_rest.py (bnapi endpoint preserved), task.py validate passes. Committed the 17 tracked task files (2c07137e) excluding .claude/. Terminal states remain all-false per plan section 10; production migration (initialize_database for the new _analysis_attempt_counter table) + marker write + restart + 3-batch observation gated to a separate crypto-guard-release pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `2c07137e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

---

## 2026-07-25 终审返工 (3 P1 + 1 P2 RED-first + reviewer P1 repair) - re-run in flight

### Context

Codex final review found 3 P1 + 1 P2; fixed RED-first keeping all prior fixes.
Fresh crypto-guard-reviewer then found 1 additional P1 (3m half-state) - also
fixed RED-first (fix A: reject 3m symmetric in intent_parser._timeframes scan
tuple + ga_crypto_tools canonical display tuple). No commit/push/release/
restart/finish-work. Boundary files (hub.pyw/frontends/fsapp.py/binance_rest.py)
zero-diff. CDN preserved. Production PostgreSQL untouched; fsapp (37304/38760)
not stopped.

### Rework status

- P1-1 (display_timeframes into Feishu path): GREEN 3 passed; revert-fail 3 failed @ HEAD
- P1-2 (no hardcoded 4 batches/40 rows): GREEN 7 passed; revert-fail 5 failed
- P1-3 (direction-flip strict fail-closed): GREEN 5 passed; revert-fail 5 failed
- P2 (stats source label): GREEN (in the 7); revert-fail 2 failed
- reviewer P1 (3m reject, fix A): GREEN; revert 3m-rejection-only 2 failed;
  renamed test_explicit_3m_is_truly_built_and_profiled -> _is_rejected_not_half_state

### Layered verification (post reviewer-P1 fix)

- Targeted (P1-1+P1-2/P2+P1-3+P1-4): 49 passed (365.90s)
- Phase H fault-inject: 33/33 SUCCESS
- Phase I fresh-DB: SUCCESS (0 issues)
- Suite partition: all=1434 parallel=1409 serial=25

### Complete-suite re-run (production code changed -> re-freeze + re-run)

Detached PowerShell Start-Process (survives harness lifecycle). Outer runner
PID 27608 (waits on captured subprocess); child pytest 29924; 4 xdist workers
(20716/26784/41896/45516) spawned 10:21:18. Logs: suite_detached.log (stdout,
buffered by capture_output=True until parallel stage completes) +
suite_detached.err.log (stderr, 0 bytes = no errors). Parallel stage ~100 min,
serial ~15 min -> expected completion ~12:15-12:30. Cron job f6f353ba (every
11 min) monitors; on outer-PID death -> read log for result -> dispatch fresh
crypto-guard-reviewer.

### Tree (frozen, 9 tracked-modified + 2 new test files)

Include (10): intent_parser.py, ga_crypto_tools.py, feishu_cards.py,
ga_judge.py, repository.py, hourly_report.py, _smoke_suite.py,
test_pg_hourly_report_feishu_p8.py, test_pg_direction_flip_closed_candle_p1_3.py,
test_pg_recent_failed_jobs_release_audit_p1_4.py
Exclude: .trellis/spec/backend/index.md, container-deployment-guidelines.md,
.claude/, deploy/, hub.pyw/frontends/fsapp.py/binance_rest.py (boundary).

### Complete-suite re-run RESULT

- 11:11 finished: parallel 1409 passed (2777.91s) + serial 25 passed (247.25s)
- complete_suite_ok elapsed_seconds=3039.19 (~50.6 min), 0 failed/0 skipped/0 errors
- stderr empty (0 errors); tree frozen at run start, no prod-code change mid-run -> no re-run needed

### Fresh crypto-guard-reviewer RESULT

- Verdict: ZERO FINDINGS (independent read-only reviewer, 202 tool calls)
- All 5 rework items PASS (P1-1, P1-2, P1-3, P2, reviewer P1 3m-rejection)
- All dimensions verified: boundary zero-diff, CDN preserved, test isolation,
  psycopg/PG correctness, no production-consumer breaks, degradation-chain
  integrity preserved, RED-first tests credible, git diff --check exit 0,
  no excluded-path dependencies
- Reviewer re-ran tests: P1-3 18 passed, P1-4 8 passed, P8 11 passed, smoke 2 passed

### Final state

- final-seal.md updated with suite result + reviewer zero-findings (NO commit)
- git diff --check exit 0; HEAD c9f1be7b (nothing committed); nothing staged
- Stopped at "等待用户 commit 授权"

### Next (awaiting user)

- User authorizes commit -> stage the 10 runtime files (Include list) ->
  commit with rework message -> (separate /trellis:crypto-guard-release pass
  for production mutation/restart + 3-batch observation -> production_ready/
  production_recovered flip)

---

## 2026-07-27 — Release Phase-1 guarded preflight + backup (PASS)

### Context
- R3 rework sealed in-tree; commit `660b106608706f93a73aa3d13532c4e7ef077b8a`
  (parent `c9f1be7b3d0983f7b0310aa58b9154a83e215bac`, 15 files, NOT pushed).
- Codex 终审修订发布契约 (plan-caliber):
  1. B1 closed via cold-start candidate (no pre-start of old version).
  2. NO git checkout/switch/reset/revert as auto-rollback.
  3. Rollback path = stop candidate service + restore operator backup, then
     stop and request separate Git-rollback authorization.
  4. Phase-2 operator edit MUST back up original + record SHA256; only
     `EXPECTED_HEAD` may change; `py_compile` after.
  5. Startup acceptance = live ≥20 min AND at least one post-start 10/10 batch.

### Phase-1 execution (read-only, NO service start, NO repo code change)

- Token: `crypto-guard-approval:gBz0NsjdD-...` (database-mutation,
  TTL 20min, uses 3) — acquired, used, then REVOKED (`active:false`).
- Script: `C:\Users\24714\AppData\Local\CryptoGuard\backups\phase1_preflight.py`
  (off-live-path backup dir; never echoes password/full DSN).

### Backup verification (Step A+B)
- pg_dump custom-format SUCCEEDED:
  path `C:\Users\24714\AppData\Local\CryptoGuard\backups\pre_release_660b1066_20260727T021725Z\crypto_guard.dump`
  size 155951175 bytes (~149 MB)
  SHA256 `25F54BD40C4C2C140540297FC7CEB65FC33DCE561B499EF38857CAFEDFC81266`
- pg_restore --list: returncode 0, TOC entries 263 (non-empty + readable).

### Probe evidence (all PASS, failures=[])
- identity: crypto_guard / crypto_guard_migrator / PG 16.14
- roles: app + migrator both rolsuper=false/rolcreatedb=false/rolcreaterole=false/
  rolreplication=false/rolbypassrls=false (non-dangerous)
- schema_health: ok=true, missing_columns_count=0, tables_checked_count=14
- marker: llm_provider_timeout_envelope_contract_v2 PRESENT (applied 2026-07-24)
- _service_ownership: row pid=37304 (DEAD) release_commit=c9f1be7b
  lease_until_ms < now_ms (EXPIRED) — expected cold-start state, B1 closed
- agent_jobs histogram: success=6991, failed=56, running=3
- pending_running_jobs: 3 daily_review jobs (ids 3203/3317/6322) with expired
  leases but NOT dangerous (daily_review is not a writer job)
- dangerous_jobs: [] (no stranded writer job — cutover safe)
- alert_outbox: sent=48 only (no pending/sending/failed — no stuck alerts)
- row_count_baseline: symbols=10, candles=28202, ga_decisions=2137,
  analysis_batches=215, signals=2137, scheduler_runs=9785, agent_jobs=7050,
  daily_review_reports=7
- recent_sealed_15m_batches: 3 batches (last 2026-07-26 13:31), all
  status=success, 10/10 symbols, llm_health.successful=10

### Result
- `preflight_pass: true`. Token revoked. NO service start, NO operator edit,
  NO pidfile delete, NO schtasks /Run|/End, NO commit/push/state-flip.

### Next (awaiting user)
- Stopped at "等待用户 Phase 2 operator-edit + service-control 授权".
- Phase-2 plan ready: operator backup + single EXPECTED_HEAD edit + py_compile
  + cold-start 20-min + 10/10 batch acceptance (see Phase-1 report to user).

## 2026-07-27 - Release Phase-1.5 guarded read-only supplementary audit (PASS, FAIL-CLOSED recommendation)

### Context
- User authorized Phase 1.5 受保护只读补充审计: close Codex 终审 stale daily_review
  side-effect risk + diagnostic-evidence gap. READ-ONLY: no operator edit, no
  service start, no production data mutation, no pidfile delete, no state flip.
- Guard discipline honored: short-TTL task-bound token, database-mutation
  classification even though SQL is read-only, token revoked immediately after.

### Method (phase1_5_audit.py)
- Decrypts `crypto_guard_app.dpapi` in-process (NOT migrator - migrator is
  REJECTED by `_validate_connected_identity` app-only allowlist). autocommit=True,
  row_factory=dict_row read-only snapshot.
- Section 1: precise read of agent_jobs IDs 3203/3317/6322 + invariant check.
- Section 2: per-job day_utc (from payload_json JSONB->dict) cross-ref
  daily_review_reports / alert_outbox (ALL rows matching dedupe_key
  `daily_review:<date>`) / scheduler_runs; crash-after-send classification.
- Section 3: injects `os.environ["CRYPTO_GUARD_DATABASE_URL"]=dsn` in-process
  ONLY (never shell/setx/registry), runs `diagnose_state_consistency` +
  `diagnose_report_accuracy`, builds issue_code_counts. Clears env in finally.

### Result: phase_1_5_pass: true (audit itself succeeded). Token revoked.

### Stale-jobs invariant (CONFIRMED all 3)
- 3203: daily_review/running, source=scheduler, day_utc=2026-07-24,
  lease 2026-07-25 08:36 (EXPIRED), finished_at=NULL, result_json=NULL.
- 3317: daily_review/running, source=paper_worker, day_utc=2026-07-24,
  lease 2026-07-25 09:30 (EXPIRED), finished_at=NULL, result_json=NULL.
- 6322: daily_review/running, source=scheduler, day_utc=2026-07-25,
  lease 2026-07-26 08:37 (EXPIRED), finished_at=NULL, result_json=NULL.
- count_match=true, all_daily_review=true, all_running=true,
  all_lease_expired_or_null=true.

### Per-job cold-start recovery risk matrix
- Cold-start mechanism: `recover_stale_running_jobs` (startup, owner path,
  service_manager.py:777) resets expired-lease RUNNING -> pending; then
  `claim_next_job` re-claims -> re-executes. NOT harmless-by-job_type.
- 3203 (2026-07-24): daily_review_reports MISSING for 2026-07-24,
  alert_outbox has NO daily_review row for the date -> classification
  `will_generate_and_send_fresh` -> cold-start WILL regenerate the 07-24
  daily report and (if send_message live) push it.
- 3317 (2026-07-24): SAME review_date as 3203. First claim (3203) generates +
  saves report; 3317 then `run_daily_review(force=False)` -> idempotent skip
  (daily_reviewer.py L1 returns existing report; L2 skips send).
  classification `fully_idempotent_no_resend` IF 3203 completes first.
  Single-worker sequential = 3203 then 3317, so idempotency holds.
- 6322 (2026-07-25): daily_review_reports MISSING for 2026-07-25,
  alert_outbox NO daily_review row -> `will_generate_and_send_fresh` ->
  cold-start WILL regenerate 07-25 report and push (if send_message live).

### send_message liveness (DECISIVE)
- frontends/fsapp.py:916 `start_all_services(send_message=send_message)`.
- send_message = fsapp.py:448 `_send_raw` -> `client.im.v1.message.create`
  (LIVE Feishu HTTP). client initialized at fsapp.py:912 immediately before.
- => production cold-start path has a LIVE Feishu pusher. The 2 missing
  daily reviews (07-24, 07-25) WILL be generated AND pushed to Feishu on
  cold start unless the 3 stale jobs are cleaned first. This is the
  FAIL-CLOSED trigger: stale-content generation+send risk is REAL.

### Diagnostics (full numeric evidence)
- schema_health: ok=true, missing=0, tables=14 (Phase-1, re-confirmed).
- state_consistency: ok=true, total_issues=2, error_count=0, warning_count=2,
  legacy_info_count=0, issue_code_counts={stalled_candidate:1,
  deterministic_direction_from_failed_llm:1}.
- report_accuracy: ok=true, total_issues=0, error_count=0, warning_count=0.
- daily_review_reports: 7 rows exist (06-15, 07-15, 07-16, 07-17, 07-21,
  07-22, 07-23 latest), ALL pushed_to_feishu=False. 07-24 + 07-25 MISSING.
- alert_outbox: 0 rows with dedupe_key `daily_review:*` (no prior send for
  any review date -> no crash-after-send dup window, but also no protection
  against a fresh send).

### Recommendation: FAIL-CLOSED on service start; fixed-id CAS cleanup FIRST
- Per user #6: all 3 jobs would generate missing/past-date daily reports =
  "可能生成或发送旧内容" -> DO NOT start service until cleaned.
- CAS-limited cleanup SQL (DRAFT, NOT executed this round):
  ```sql
  -- record BEFORE rows first (read-only): SELECT id,job_type,status,lease_until
  --   FROM agent_jobs WHERE id IN (3203,3317,6322);
  UPDATE agent_jobs
    SET status='failed',
        error_message='stale_daily_review_discarded_before_release'
  WHERE id IN (3203,3317,6322)
    AND job_type='daily_review'
    AND status='running'
    AND lease_until <= NOW();
  -- expected affected rows = 3
  -- record AFTER rows: same SELECT; verify 3 rows now status=failed,
  --   error_message set, lease_until UNCHANGED. Then STOP and request
  --   separate authorization before any further mutation.
  ```

### Revised Phase-2 operator-edit scheme (DRAFT, NOT executed this round)
- target commit already checked out: HEAD=660b1066... (verified).
- operator baseline SHA256 = bf2fe7026c7dce72dda2ddb0797669c0381066b91ba98cf707da82e8d20c435
  (verified UNCHANGED).
- backup: UTC-timestamp unique dir under %LOCALAPPDATA%\CryptoGuard\backups\;
  fail-closed if dir already exists; NO -Force overwrite; record pre-edit SHA256.
- single mutation: run_fsapp_phaseb_supervisor.py line 37
  EXPECTED_HEAD "c9f1be7b..." -> "660b106608706f93a73aa3d13532c4e7ef077b8a"
  (the ONLY allowed diff). py_compile after. record post-edit SHA256.
- cold-start: `python run_fsapp_phaseb_supervisor.py --attempt 1`
  (supervisor validates HEAD==660b1066, decrypts app DPAPI, injects DSN into
  fsapp child env only, blocks on child). Acceptance: live >=20 min AND >=1
  post-start 10/10 batch.

### Next (awaiting user)
- Stopped at "等待用户 stale daily_review 处置（如需要）及 Phase 2
  operator-edit + service-control 授权".
- Recommendation: authorize the fixed-id CAS cleanup FIRST (before any service
  start), then Phase-2 operator-edit + cold-start.
- NO commit/push/release/service-start-or-stop/operator-edit/pidfile-delete/
  state-flip performed this round.

## 2026-07-27 — Phase-2 F done + C scoping correction (in-tree, UNCOMMITTED)

### Requirement F (symptom #6) — DONE, GREEN
- New contract marker `llm_failed_direction_fail_closed_v1` registered by
  `initialize_database` (migrations.py `_ensure_llm_failed_direction_fail_closed_
  marker`, added after `_ensure_stop_loss_adjustment_dedup_marker` in the init
  sequence). EXPECTED_MARKERS updated in test_pg_migrations.py.
- state_consistency.py: `LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY` constant,
  `_llm_failed_direction_fail_closed_cutoff` helper,
  `_check_llm_failed_direction_fail_closed_marker_missing` (fail-closed error;
  registered in CHECKS tuple). `_check_deterministic_direction_from_failed_llm`
  split into marker-AFTER (current warning) / marker-BEFORE (historical
  legacy_info, type `deterministic_direction_from_failed_llm_historical`, does
  NOT inflate warning_count / NOT fail gate — symptom #6 fix) / marker-MISSING
  (directional check SKIPPED + marker-missing error → ok=False, fail-closed).
- Added module-level `_coerce_iso` (datetime|str ISO-8601 `...Z` or psycopg
  timestamptz `2026-... ...+00:00` → aware UTC datetime) for reliable
  current/historical comparison. SQL window widened from `>= cutoff` to
  `[cutoff - 30d, NOW+1d]` so marker-BEFORE rows reach the classifier.
- Summary keys added: `llm_failed_direction_fail_closed_marker_missing`,
  `deterministic_direction_from_failed_llm_historical`.
- Test test_pg_llm_failed_direction_fail_closed_marker_p2_1.py: 6 tests (6/6
  GREEN). P8 regression 39/39 PASS. migrations 6/6 PASS.

### Requirement C — SCOPING CORRECTION (07-27 final review)
- Original C fix fired for BOTH `failed` and `disabled` → BROKE 2 pre-existing
  07-03 semantic-accuracy tests (test_doge_countertrend_rebound_not_bullish_middle
  + test_sol_short_bullish_but_explains_htf_mixed) which run under
  CRYPTO_GUARD_LLM_ANALYSIS=0 → llm_status="disabled" → forced unknown destroyed
  the HTF-aware deterministic product.
- Correction: fail-closed block fires ONLY when `fallback_blocked and
  llm_status == "failed"`. `disabled` = deterministic-only operating mode where
  the deterministic direction IS the product (07-03 tests pin HTF-aware bias
  survives). breaker-skip/preset-None/retry-None all set "failed" → ARE
  fail-closed. Production runs with LLM enabled → leaked rows are `failed`.
- Test test_pg_llm_failed_direction_fail_closed_p2_1.py updated: failed→unknown
  vs disabled→keeps bullish. 5/5 GREEN. 07-03 tests 2/2 PASS again;
  test_hourly_report_regressions.py 187/187 PASS.

### Boundary adherence this round
- NO production DB mutation, NO marker write to production, NO service restart,
  NO commit/push/release/finish-work, NO hub.pyw/fsapp.py/binance_rest.py edit,
  NO .claude/deploy/container-deployment inclusion, NO password/DSN/DPAPI/
  approval-token output. Marker only registered in scratch schema via
  make_repo() (test-only).

### Pending (next round)
- D (suggested_actions schema repair), G (stalled_candidate recommendation at
  auth boundary), H remainder (punctuation/repeated sentences + Feishu card
  path verification). Then targeted + Phase H + Phase I + complete suite, two
  consecutive full greens, dispatch brand-new crypto-guard-reviewer (zero
  findings → final_seal_complete=true), four states, stop at "awaiting user
  commit authorization".

## 07-31 production fix: schema-breaker preset integrity (COMPLETED)

Authoritative residual work for the 07-31 production issues — implemented
RED-first, verified end-to-end, sealed offline. Full closure record:
`.trellis/tasks/07-27-hourly-summary-semantic-integrity/final-seal.md` (07-31
section). Summary:

### Production evidence (4 items)
- (1) SOL/ETH repeatedly emitted `decision` as ARRAY; (2) `take_profits`
  numeric items violating object schema; (3) batch 15m:1785487499999:
  total_attempts=13, successful=8, failed=5, breaker=open → all 10 rows
  breaker_skipped, provider_call_count=0 (8 coordinator successes destroyed);
  (4) reports showing llm_failure_rate_high + llm_circuit_breaker_open.

### Findings closed (6 production + reviewer residuals)
- P0-1: preset candidate consumed BEFORE breaker gate (no re-call / no
  record_skip / real terminal reason preserved in §8).
- P0-2: BREAKER_DRIVING_CATEGORIES single source of truth in llm_breaker.py;
  judge `_BREAKER_INFRA_REASONS` = import alias; schema/parse failures no
  longer pollute the rate window.
- P1-1: decision array repair chain (single→collapse; multi→monitor_only
  conservative + plan cancel + grade ≤B; illegal→fail-closed; audit in
  llm_parse_meta). Schema flat enum NOT loosened (D).
- P1-2: take_profits numeric → `{"price": n, "ratio": 1.0}` when unambiguous;
  junk→conservative cancel; repaired result re-validated by strict schema.
- P1-3: prompt `schema_contract.decision` = flat string enum (never bare
  array); all THREE provider tiers verbatim type contracts + examples.
- P1-4: marker `llm_schema_breaker_preset_integrity_v1` (registered once,
  release path); marker-missing fail-closed; marker-BEFORE → legacy_info;
  repaired rows = SUCCESS + llm_repair_count; compact llm_error for Feishu.
- Reviewer round-1 (ac3960dae704e1bc5) PASS + 5 findings: P2-1 (llm_error_detail
  top level, ga_master/decision_schema.py + persisted-row test) FIXED,
  P2-2 (guard repair_ rule) DEFERRED (user constraint forbids .claude/ — needs
  separate authorization), P2-3 (journal whitespace, git diff --check exit 0)
  FIXED, Recommended-1 (report_diagnostics docstring: floor >=3 STRICTER than
  breaker min_rate_samples=5, conservative direction) CLOSED, Recommended-2
  (llm_breaker side behaviors) recorded in seal.
- Reviewer round-2 (a84366e9a496e046c): code closures all VERIFIED (P2-1 7/7,
  P2-3, Recommended-1); FAIL only P2-A (P2-2 deferral record) + P2-B
  (Recommended-2 notes) — both closed by the 07-31 final-seal section. ZERO
  open code findings.

### Verification chain (frozen tree, fingerprint a091748474f7 F1==F2==F3)
- 61 targeted passed (6 new test files); Phase H 36 passed; Phase I
  PHASE_I_OK on fresh DB (marker present, ok=True, issues=0).
- Complete suite ×2 consecutive green: RUN1 1579+25 passed,
  complete_suite_ok 2280.41s; RUN2 1579+25 passed, 2207.91s; stderr 0B both;
  partition match all=1604 parallel=1579 serial=25. fsapp (PID 39540) live
  throughout — test/prod DB isolation proven again.

### Boundary adherence this round
- NO production DB mutation, NO marker write to production, NO service restart,
  NO commit/push/release/finish-work, NO hub.pyw/fsapp.py/binance_rest.py edit,
  NO .claude/deploy/container-deployment inclusion, NO password/DSN/DPAPI/
  approval-token output.

### Awaiting user decision
1. Commit authorization for the 14-file 07-31 code commit (staging list in
   final-seal.md). 2. P2-2 authorization (.claude guard one-line fix).
3. Production release via /trellis:crypto-guard-release (stays gated).

**等待用户 commit 授权。**

### 08-02 production release pre-start closure (user instruction 3, A-H)
- A: read-only preflight + zero-writer five-way evidence (pid 39540 dead, no
  app/hub processes, pg_stat_activity probe-only, lease expired ~20h,
  scheduled task Ready). 46 stale live agent_jobs (1 running id=10876 + 45
  pending) listed read-only, untouched.
- B: .claude command guard runs_pytest exclusion fixed (repair_ branch):
  RED 1 failed → GREEN 20 passed (+9 subtests); SHA256 6ec056f0…→94596b2a…;
  test file dccfef6c…; stays uncommitted.
- C: origin/main fast-forwarded to b53c2fb (force-with-lease 61e7b0d..b53c2fb);
  verified at H again.
- D: backup pre_b53c2fb_20260801T172338Z — crypto_guard.dump 258,314,773 B,
  SHA256 ad6c59d2…1f155c39, pg_restore --list 263 entries; markers_before=10.
- E: no writer to stop (zero writers confirmed).
- F: initialize_database(allow_ddl=True) under migrator role — init_ok=true;
  11/11 markers, missing_markers=[] (llm_schema_breaker_preset_integrity_v1
  added 2026-08-01T17:41:04Z, only delta); schema_health_ok, missing_columns=[];
  state_consistency error_count=0; report_accuracy error_count=1 =
  llm_symbol_starvation on 07-31 09:59-11:29Z batches (pre-existing incident
  evidence, targeted by b53c2fb, re-verify post-start); row counts match
  backup baseline (agent_jobs 11645 etc.). Verification via pg_db.get_conn()
  dict_row (tuple-row probe bug fixed).
- G: operator EXPECTED_HEAD ceb099d2… → b53c2fb12… (replace count 1);
  backup .pre_b53c2fb; SHA256 54642157… → 9FFE3B8D…; py_compile OK;
  start_hub_with_pg.py untouched.
- H: tokens revoked (active=false); scheduled task Ready; app not started.
- Final states: implementation_complete=true, final_seal_complete=true,
  production_ready=true, production_recovered=false. NO finish-work.
- **启动前全部就绪,app 尚未启动,等待用户手动启动。**

### 08-03 08-02 task fresh-reviewer closures + re-freeze double green (08-02 repair round 2)
- Fresh independent crypto-guard-reviewer dispatched on frozen tree: 1 P0 + 3 P2,
  ALL closed.
  - P0: controller_decision_from_legacy dropped top-level has_trade_plan →
    every production row final_executable=False. Fix ga_master/decision_schema.py:148
    `has_trade_plan: bool(legacy.get("has_trade_plan") and legacy.get("trade_plan"))`;
    2 tests (TestProducerWritesTopLevelHasTradePlan) + revert-fail proven.
  - P2-1: .claude command-guard repair_ rule lacks and not runs_pytest exemption.
    Fix .claude/hooks/crypto-guard-command-guard.py; 23 tests; RED 1 failed →
    GREEN 23 passed.
  - P2-2: watch conditions reject schema-forbidden keys (direction/symbol).
    Fix reasoning/watch_conditions.py _SCHEMA_FORBIDDEN_CONDITION_KEYS; +5 tests
    in p0_3 file.
  - P2-3: execution-funnel report window gates on execution_funnel_cutoff_utc.
    Fix notify/hourly_report.py; 7 tests (TestDecisionRowExecutionFunnelWindowGate
    6 + TestRenderForwardsExecutionFunnelCutoff 1).
- re1 stale-contract discovery: _smoke_suite.py
  test_missing_candidate_on_llm_failure_caught_via_real_controller had
  assertNotIn("has_trade_plan") — encoded the pre-P0-fix buggy contract. re1
  FAILED 1/1718 (1 failed, 1717 passed, 10 subtests, 1217.44s). Fixed: assert
  top-level has_trade_plan is False; fault injection pops candidate_trade_plan
  AND has_trade_plan; diagnostic proven field-independent.
- re2 re-freeze double green: partition_ok all=1743 parallel=1718 serial=25;
  RUN1 parallel 1718/1718 + serial 25/25, complete_suite_ok 1396.41s; RUN2
  parallel 1718/1718 + serial 25/25, complete_suite_ok 1411.64s; BOTH_GREEN,
  RUN1_DONE + DOUBLE_RUN_OK (task output b0kq8i8pk). Zero edits between runs.
- Phase H 33/33 faults verified SUCCESS (execution_funnel_report_contract_marker_missing
  fault included); Phase I fresh DB clean (schema_health ok, state_consistency 0,
  report_accuracy 0, legacy_info_count 0) — phase_h.log / phase_i.log 2026-08-02 22:02.
- Freeze fingerprint (cg_0802_freeze_fp.py): HEAD=b53c2fb126d98426806519f0d9ce66fffa311075,
  diff_binary_sha256=5781CC60…, untracked_tests_content_sha256=1967AB33…,
  untracked_tests_names_sha256=9EADB0DD…, pytest_ini_sha256=EA488272…,
  porcelain_sha256=30CDA720…, diff_bytes=183573, untracked_tests_count=6,
  freeze_at=2026-08-03T03:30:06+08:00.
- 115 targeted passed (6 new test files: 3+10+7+14+38+43) + command-guard 23 +
  migrations/schema-health 16+22 + P0-3 bidirectional/phase04 smoke 2.
- final-seal.md written under .trellis/tasks/08-02-08-02-execution-funnel-watch-integrity/
  with production baseline, closure table, freeze, double-green block, Phase H/I,
  reviewer verdict, staging list.
- Final states (per 08-02 mandate, BEFORE commit): implementation_complete=false,
  final_seal_complete=false, production_ready=false, production_recovered=false.
  Staging list = 24 modified + 15 untracked (39 paths). **等待用户 commit 授权。**

### 08-03 Codex terminal-review rework (rounds 1-3 closed, round-4 zero findings)
- Per Codex 终审返工 mandate: the re2 seal's verdict was treated as REJECTED
  (history preserved in final-seal.md); rounds appended, no new PRD/plan.
- Freeze script STRENGTHENED (round 2): content-hash EVERY untracked file
  (untracked_all_content_sha256), closing a hole where edits to untracked
  NON-test files (e.g. reasoning/watch_conditions.py) were invisible to
  diff/porcelain. Old gate only hashed tests/ files + paths.
- ROUND 1 (5 P2s, closed):
  - P2-1 report_diagnostics.py corrupt execution-funnel marker FAIL-OPEN → the
    last two readers now fail closed (_execution_funnel_check_created_at_lower_bound
    parses & fails closed to now; marker-missing fires marker_corrupt on
    present-but-unparseable). NOTE: _migration_state.applied_at is TIMESTAMPTZ so a
    garbage literal can't store end-to-end — defense-in-depth. 8 white-box tests
    via repo-shaped _stub_marker_repo driving REAL prod functions (P1-4 pattern).
  - P2-2 _is_schema_condition level/price type hole → rejects non-numeric/
    negative/bool; _clean_condition drops garbage sibling. 6 tests.
  - P2-3 is_structured_watch expires_minutes type hole → rejects non-None/
    non-bool-int; repair coerces default. 6 tests.
  - P2-4 _phase_h_fault_inject._ensure_all_contract_markers now seeds
    _ensure_execution_funnel_report_contract_marker (Phase H mirrors Phase I).
  - P2-5 duplicate coverage accepted-by-design (no relax/delete allowed). No change.
- ROUND 2 (1 P2, closed): is_structured_watch never required envelope key
  invalid_condition (schema:64). KEY must be present (value may be null). 2 tests.
- ROUND 3 (1 P2, closed): is_structured_condition TRUTHINESS-checked cvd trigger
  → non-string flow/value passed, _clean_condition dropped it → normalized empty
  schema-invalid shell (P1-3 contract violated). Fix: non-empty-STRING cvd trigger
  + normalize keep-loop & invalid path re-validate each _clean_condition output
  (orphaned → dropped / rebuilt from trade plan). RED-first proven: 5 failed
  pre-fix, 10 passed post-fix.
- Regression test_pg_codex_p2_rework_fixes.py = 32 tests (8+6+6+2+10). No prod
  function mocked; defect-driven (RED-first + revert-fail per class). 104 watch/P2
  tests pass.
- ROUND 4 fresh independent reviewer: ZERO findings (P0/P1/P2); all 7 verify items
  PASS. Residual (NOT findings): theoretical _condition_is_untriggerable cvd blind
  spot (unreachable via current producers); stale session-start snapshot showing
  llm_breaker/preset files absent from LIVE tree — live git status + freeze fp are
  authoritative; recompute commit path count at commit time.
- Re-freeze double green (Codex rework, all-untracked content hash):
  PAIR A combined=94313DBB… untracked_all=B9EE9C… count=70 tests=12
    RUN1b complete_suite_ok 1454.03s; RUN2b 1766.53s; serial 25/25; F4b==F3b
  PAIR C combined=6D5E49C0… untracked_all=15FB98CC… count=70 tests=12
    RUN1c complete_suite_ok 1477.98s; RUN2c 1531.12s; serial 25/25; F4c==F3c
  partition 1806 (1781 parallel + 25 serial); all four runs complete_suite_ok;
  zero edits between each RUN1/RUN2 pair. script=cg_0802_freeze_fp.py (strengthened).
- Accurate grouped staging (live, 08-03): INCLUDE 24 modified (22 code/tests +
  index.md + journal-1.md) + 12 untracked test files + watch_conditions.py +
  2 .claude commands + .claude/skills/crypto-guard-final-seal/{SKILL.md,agents/openai.yaml,
  references/closure-matrix.md} + .trellis/spec/backend/container-deployment-guidelines.md
  + deploy/grok-register-lite/{.env.example,compose.yaml}. EXCLUDE 49 suite_* run
  artifacts + gitignored (.env, data/, trellis-* skills) + boundary zero-diff.
- final-seal.md appended with Codex rework section (closure table, strengthened
  freeze, round-4 zero-findings verdict, grouped staging, open items).
- Final states (per 08-02 mandate, AFTER Codex rework, BEFORE commit):
  implementation_complete=false, final_seal_complete=false, production_ready=false,
  production_recovered=false. **等待用户终审与分组 commit 授权。**
  No push / release / restart / finish-work / production DB mutation executed.

## 2026-08-03 Codex R2 terminal-review round-4 — FINAL re-verify

- Two further P2 code findings from brand-new reviewer acb86615d171be4f2 fixed
  RED-first (revert-fail proven, both restores byte-identical):
  - F1 diagnostic mirror: per-item account_feedback_recheck special case in
    `_condition_is_untriggerable` cleared list-item/kind-only/uppercase variants
    the watcher flags. Fix: caller-level root-dict skip only (exact-case
    `type=="account_feedback_recheck"`, mirrors opportunity_watcher.py:82). 5
    regression tests (incl. list-item + kind-only + mixed-watch data-quality msg);
    revert-fail: restore pre-F1 B594… → list-item+kind-only RED.
  - F2 `needed=False` erased by repair chain (normalize force-set needed=True,
    making P1 gate vacuous on the production LLM path via
    `_try_repair_opportunity_watch`). Fix: `normalize_opportunity_watch` preserves
    explicit needed=False; absent/None/non-bool → True. 2 regression tests;
    revert-fail: revert to needed=True → both RED.
- Fresh brand-new reviewer a8f5fef52b7186f44 on the FINAL tree: **ZERO code
  findings**; full R2 scope (P0/P1/P2-1/P2-3/Fix A/B/C/F1/F2) re-verified PASS.
- Doc-drift corrections (confirmed vs live code): P1-1 finalizer is
  `ga_master/controller.py:458` (`_finalize_plan_lifecycle`), NOT
  `reasoning/decision_schema.py`; `_post_decision_effects` is
  `run_ga_workers.py:203` (gate :300), NOT controller.py — final-seal staging
  note + P0-2 code column corrected.
- Freeze + double-run (FINAL tree): HEAD=b53c2fb…, porcelain_sha256=7F463278…,
  diff_sha256=2DC27887…, staged=empty, zero edits across ALL runs (re-verified
  after each). RUN1 1767.82s, RUN2 1662.13s — both all=1838 (1813 parallel + 25
  serial), 0 fail/skip/deselect, stderr 0B. partition match RUN1==RUN2.
  logs: %TEMP%\suite_codex_r2_run1/2.{out,err}.
- Flake (NOT a finding, no code change): first RUN2 attempt exited 1 on exactly
  one PRE-EXISTING concurrency test
  (test_pg_migrations.py::TestPostgresInitializeDatabase::
  test_healthy_initialize_skips_ddl_does_not_block_concurrent_dml) — 10s
  completed_under_lock window exceeded under 8-worker load (cluster-wide
  advisory-lock contention). Unchanged by R2; passes isolation 7.14s/7.35s; green
  on RUN1 + RUN2 retry. Frozen tree never edited.
- Staging reconciled vs LIVE porcelain (44 = 25 M + 19 ??; seal's 24/15/12 was
  stale): Groups A=15 prod code, B=1 new code (watch_conditions.py), C=4
  tools+hooks, D=17 tests (4 M + 13 ?? incl. new test_pg_codex_r2_rework_p0_p1.py),
  E=7 docs/skills/deploy/trellis. Union=44, zero overlap, missing/extra=0.
- final-seal.md appended with round-4 section (F1/F2 closure table, zero-findings
  verdict, doc-drift corrections, freeze+double-run, flake note, grouped staging,
  open items).
- Final states (per 08-02 mandate, AFTER round-4, BEFORE commit):
  implementation_complete=false, final_seal_complete=false, production_ready=false,
  production_recovered=false. **等待用户终审与分组 commit 授权。**
  No push / release / restart / finish-work / production DB mutation executed.

## Session: R1-3 hard gate MET + SINGLE valid final-seal frozen double-run LAUNCHED (08-10)

**Task**: 08-08 test feedback loop acceleration (Step 6)
**Branch**: `main`, HEAD e9d7675, frozen tree

- R1-3 hard gate MEASURED-MET: single `full` complete-suite run on the CURRENT
  tree (post round-10 delta: 3-file rollback_isolation widening +
  feedback_ttl.py production fix) → `complete_suite_ok elapsed_seconds=2031.32`
  (33.9 min ≤ 2400 s hard gate, exit 0). partition_ok all=2148 parallel=2120
  serial=28 (serial 28/28 passed in 183.83s). vs pre-rework 2618.74s (43.6 min)
  = −587s (−22%) with MORE tests (2148 vs 2092, R1-4 holds). Stretch ≤1500s NOT
  the gate (machine lacks ≥13 effective cores). Recorded in
  research/step6-single-full-run-2.md; implement.md + final-seal.md updated.
- All pre-gates green: round-9 PASS, round-10 PASS, single full ≤40 min MET.
- SINGLE valid final-seal frozen double-run LAUNCHED (background, PID 3256):
  `python -m plugins.crypto_guard.tests.run_change_aware --tier final-seal
  --workers 8`. Contract: F1→RUN1→F2→RUN2→F3, F1==F2==F3, each run ≤2400s
  (exit 4), drift exit 3, BOTH_GREEN + run_elapsed_seconds, NEVER from cache,
  executed exactly once, ~70 min. Pre-launch verified: evidence.jsonl 0 bytes,
  HEAD e9d7675, no leftover pytest procs, CRYPTO_GUARD_DB_ADMIN_PASSWORD set.
- If both runs ≤40 min AND F1==F2==F3 → set implementation_complete=true +
  final_seal_complete=true in final-seal.md (operator directive), production
  states stay false → STOP at commit authorization.
## UPDATE: final-seal RUN1 aborted (runner-parser bug) → FIXED RED-first → RELAUNCHED (08-10)

- First final-seal attempt (05:57) FAILED exit 1 in RUN1: parallel pytest
  genuinely PASSED (2120 passed, exit 0, 1433.60s) but _run_exact_stage's
  UNANCHORED `(\d+) failed|skipped` regex misread -rA-captured app-log lines
  (`..._1783641599999 failed identity contract` ×2, `enabled=10 queued=10
  skipped=0` ×2) → `RuntimeError: full:parallel stage was not exact:
  {'failed': 3567283199998, 'skipped': 20}` → exit 1 BEFORE the final-seal
  contract (exit 4/3/BOTH_GREEN) could engage. NO evidence written (0 bytes).
- ROOT CAUSE: run_change_aware._run_stages passes -rA (per-node verdicts); -rA
  appends every test's captured output incl. app logs; run_complete_suite.main
  never passes -rA, so the 2031.32s single-full ran clean. Bug only surfaces at
  full-suite scale through the change-aware runner.
- FIX (test-framework file in allowed set, exactness STRENGTHENED): extracted
  `_stage_counts(combined)` in run_complete_suite.py; anchored failed/skipped/
  deselected EXACTLY like the existing passed scan `(?:^|\s)...(?:\s|,|$)`.
  2 new pure-unit regression tests (RED: revert → `{'failed':
  1783641599999, 'skipped': 10}`; GREEN: 41 passed in 98.83s full runner file;
  genuine-banner detection preserved). Mapping digest ff5de36a unchanged.
  Recorded in research/step6-final-seal-run1-parser-fix.md.
- Final-seal RELAUNCHED 06:5x (PID 11844, RUN1 parallel live, -rA path). This
  is still the SINGLE valid execution — the aborted attempt recorded no
  evidence. Evidence.jsonl still 0 bytes.

- Final-seal RUN2 (be2qv6d7k) FAILED exit 1 in RUN1 — NOT a gate failure, NO
  evidence. Parser fix WORKED: correctly detected a genuine `1 failed`
  (test_shadow_lifecycle_regressions.py::ShadowVTLifecycleTest::test_fill_before_size_order)
  and propagated exit 1. Root cause: test died in setUp at pg_db.py:135
  `pool.open(wait=True, timeout=3.0)` → psycopg_pool.PoolTimeout: pool
  initialization incomplete after 3.0 sec on [gw7] — transient PG
  connection-establishment contention under 8-worker load, NOT test logic.
  Isolated: `1 passed in 8.73s`. My +2 tests are pure-unit/PG-neutral
  (partition 2148→2150 all, 2120→2122 parallel). 3s bound unchanged and the
  all=2148 single-full (2031.32s, same 8 workers) ran clean → flake is rare.
  08-02 precedent: pre-existing concurrency flake under 8-worker load = NOT a
  finding, no code change, green on retry. Disposition: NOT a finding, NO code
  change (R1-4). Widening CRYPTO_GUARD_POOL_OPEN_TIMEOUT rejected (would change
  measurement env vs 2031.32s baseline = harness-tuning-to-pass). Recorded in
  research/step6-final-seal-run2-pool-flake.md.
- Final-seal RELAUNCHED (RUN3) on the UNCHANGED tree — the SINGLE valid
  execution (both aborted attempts recorded no evidence). STOP condition: if
  this third launch fails for any reason, no further re-launches.

## Task 08-10 Step 2 COMPLETE: policy configuration and parsing (08-11)

**Task**: 08-10 LLM-assisted risk governance (Step 2 of 11, task #40)
**Branch**: `main`, tree UNCOMMITTED (STOP at commit auth)

- **RED (Step-1 contract)**: modules absent → 179 failed / 11 passed in
  359.88s. All 11 passes verified as legitimate negative controls (read each
  rollout test body; e.g. current handler creates order directly from passed
  trade plan → bridge/regression test valid at RED and GREEN).
- **GREEN**: `python -m pytest test_pg_08_10_risk_policy_p2.py
  test_pg_08_10_confirmation_lifecycle_p1.py::TestRiskAssistancePolicyParsing`
  → 37 passed in 0.28s (28 + 9).
- **Revert-fail**: `risk/risk_policy.py` renamed away → 28 failed
  (ModuleNotFoundError); restored → 37 passed.
- **Real-path**: throwaway DB-backed `load_config()` smoke (fx.make_repo) parses
  the new `risk_assistance` section → 1 passed; file deleted after.
- **Deliverables**: `risk/risk_policy.py` (HARD_GATE_CODES 8 mandatory compiled
  floor, ADAPTIVE_GATE_CODES, VALID_MODES, frozen RiskAssistancePolicy with
  full __post_init__ invariant validation, load_risk_assistance_config
  fail-closed); `config/loader.py` `CryptoGuardConfig.risk_assistance`
  property + load_config validation call; `config/trading_mode.yaml`
  `risk_assistance:` section (mode: shadow, design §4 exact yaml).
- implement.md Step 2 all 6 checkboxes marked [x] with evidence.
- Four states unchanged: implementation_complete=false,
  final_seal_complete=false, production_ready=false, production_recovered=false.

## Task 08-10 Step 3 COMPLETE: confirmation event persistence and migration (08-11)

**Task**: 08-10 LLM-assisted risk governance (Step 3 of 11, task #41)
**Branch**: `main`, tree UNCOMMITTED (STOP at commit auth)

- **RED->GREEN->revert-fail**: `TestEventPersistenceContract` (p1) 8 passed +
  p2 migration file 8 passed + `test_pg_migrations.py` 7 passed = **23 passed**
  (GREEN set, 46.02s + 56.61s + 52.23s). Revert-fail: schema DDL block removed
  -> 15 failed / 1 passed (health-gate RuntimeError lists all 12 missing
  columns + missing table + missing index; the 1 pass is the pure fingerprint
  function = legitimate negative control) -> restored -> 16 passed.
- **New table** `entry_confirmation_events` (12 cols, 2 FKs: snapshot_id ->
  market_snapshots, decision_id -> ga_decisions) + EXACT non-partial UNIQUE
  index `idx_entry_confirmation_events_fingerprint` + 3 new schema-health
  checks wired into pre-health. `_EXPECTED_SCHEMA_FINGERPRINT` regenerated to
  `8b1d13c4...aeb7f` (column-order-insensitive catalog SHA-256).
- **Insertion contract** `insert_entry_confirmation_event_after_decision`:
  all 8 fail-closed gates (decision exists, snapshot_id match, canonical
  shape, VALID_CONFIRMATION_SOURCES, close_time<=analysis, side/direction
  consistency, symbol match, entry_trigger_confirmation provenance);
  `ON CONFLICT (event_fingerprint) DO NOTHING RETURNING id` + SELECT fallback
  = concurrent idempotency; decision rollback leaves no orphan event.
- **Migration** (same advisory lock + txn, before schema DDL): missing table
  no-op; partial EMPTY table DROP+recreate exact; partial WITH-ROWS table
  RuntimeError (never auto-delete business rows); identical dup fingerprints
  deduped keep-lowest-id; conflicting dups RuntimeError (tamper signature);
  wrong/missing fingerprint index introspected via pg_index/pg_attribute and
  rebuilt UNIQUE non-partial (CREATE UNIQUE INDEX IF NOT EXISTS is a
  name-only no-op and would hide a wrong index).
- **Marker** `entry_confirmation_lifecycle_contract_v1` registered as the LAST
  write after the health gate; mirrored in `test_pg_migrations.py`
  EXPECTED_MARKERS (Phase H/I mirror = automatic full-suite runs).
- **Marker-absence proof** (new): fail-closed test now starts from a DDL-only
  UNINITIALIZED schema (SCHEMA_PATH applied, no seeds/markers) so it proves
  the marker is NEVER written on a failed init, then repairs the column
  (backfill canonical fingerprint + SET NOT NULL) and re-runs the SAME
  initialize_database() -> ok + marker present (health-gated, not permanently
  absent). This sub-assertion was unblocked by the earlier grep: ga_decisions
  has NO FK to symbols, so _persist_source_event runs on an uninitialized
  schema.
- Note: 16 tests in p1 (`TestCarriedForwardAndExpiry`,
  `TestInvalidationAndFailClosed`) fail with NotImplementedError — those are
  Step 4 resolver RED tests (task #42 pending), NOT Step 3 scope.
- implement.md Step 3 all 8 checkboxes marked [x] with evidence.
- Four states unchanged: implementation_complete=false,
  final_seal_complete=false, production_ready=false, production_recovered=false.

### 08-11 Step 4 — Deterministic lifecycle resolver (task #42) GREEN

- `entry_confirmation_lifecycle.py` grew from the Step-3 stub (fingerprint +
  source allowlist) into the full §5 resolver: pure `_evaluate_candidate`
  (7 checks + fail-closed status priority) + `resolve_trusted_entry_confirmation`
  repo-loading adapter (current wins; else same-symbol/same-direction persisted
  events in the hard-max window; newest-first `(close, tf-priority, fingerprint)`
  deterministic sort; per-source-snapshot cache; first valid wins else first
  candidate's status else absent). Module still MUST NOT import storage.
- Extraction relocated VERBATIM from ga_judge.py into the lifecycle module as
  the single source of truth; ga_judge re-exports it (llm_agent_judge.py:4208
  and _smoke_suite consumers still resolve). Direction derivation is now
  name-first: `_structure_event_direction` treats a `bullish_choch` as bullish
  even with a contradictory `direction` field; explicit field only when the
  name carries none; missing -> reject (no defaulting).
- **Two bugs found & fixed during GREEN:**
  1. `_expected_direction` written INVERTED (`SHORT->bullish`); corrected to
     `bearish if side == "SHORT" else "bullish"` — this single flip explained
     all three failure shapes (current-snapshot data_gap, carried absent,
     opposite data_gap): 12 failures -> 32 passed.
  2. `test_opposite_bullish_choch_invalidates_immediately` got `geometry_mismatch`
     because the fixture reuses `_bearish_event` (`event="bullish_choch"` +
     `direction="bearish"`, contradictory). Name-first direction fixed it: the
     event is a bullish structure -> not a current SHORT confirmation -> carried
     path -> `_later_opposing_structure` finds it -> `invalidated/opposite_structure`.
- **Full p1 file: 33 passed** (146-165s): 16 Step 4 + 8 persistence + 9 policy.
  Extraction consumers green: `test_pg_08_08_timeframe_modules_entry_extraction.py`
  5 passed; smoke consumers (test_a3_*, test_r4_d5_*, test_r9_build_trade_plan_*)
  8 passed.
- **Revert-fail proof (carry/expiry/invalidation):** temporarily renamed
  `resolve_trusted_entry_confirmation` + `_evaluate_candidate` away -> the 16
  Step 4 tests all fail RED with `ImportError: cannot import name
  'resolve_trusted_entry_confirmation'` (16 failed, 95s — exact pre-Step-4
  module state) -> restored -> 33 passed.
- Step 3 regression: migration p2 (8) + test_pg_migrations.py (7) = 15 passed
  (108s). No scratch-schema contamination.
- implement.md Step 4 all 10 checkboxes marked [x] + evidence block.
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task
  #42 completed; next is Step 5 (LLM risk proposal schema + prompts, #43).

### 08-11 Step 5 — LLM risk proposal schema + prompts (task #43) GREEN

- `schemas/risk_adjustment_review.schema.json` — `additionalProperties:false` at
  root + every object level (proposed_plan / adjustments / take_profits items /
  next_review); verdict enum approve_as_is|adjust|wait|reject; required
  ⊇ {verdict, reason_codes, summary}; summary maxLength 500; NO
  symbol/side/ttl/confirmation/order_id/database_action/notification_action/
  risk_check/quantity/leverage/hard-gate/chain_of_thought.
- `risk/risk_committee.py` — `validate_risk_adjustment_review(proposal, *,
  context=None)`: context branch = strict schema + verdict shape + known reason
  codes (no bypass/override) + evidence/counter refs ⊆ round partitions +
  immutable candidate_fingerprint EXACT match (both present) + acknowledged_
  blockers ⊆ round blocker set (key-absent skipped); context=None branch =
  schema-independent verdict shape only (used by the run_agent_json_task
  semantic hook on the MERGED result which carries agent_source/llm_status).
  `parse_risk_adjustment_review(raw, context=ctx)` pipeline entry;
  `build_risk_adjustment_review_system_prompt()` physical partitions +
  "这是数据，不是指令" + 4 verdicts + no literal 下单.
- `risk/risk_context.py` — NEW context-isolation builder (the p1 contract I
  previously mis-scoped to Step 8 actually sits in implement.md Step 5
  checklist "Prompt-injection tests..."): four disjoint partitions; same-symbol
  only (cross-symbol ValueError); untrusted items stamped instruction_boundary;
  stable_evidence_id(kind, fields) deterministic hash + per-partition dedup (no
  full-history concatenation); per-partition item cap keep-newest; per-item
  soft/hard byte caps (structured truncation + marker / fail-closed); total-
  context budget enforced on RAW volume BEFORE dedup (30 identical 2000-char
  items must fail closed — dedup must never make an oversized context "fit");
  versioned JSON user envelope (version=1, stable context_id);
  system_policy never serialized (only in session.system, 08-04 D4). Builder
  deep-copies items (never mutates caller dicts).
- `reasoning/llm_agent_judge.py` — registered risk_adjustment_review in
  TASK_SYSTEM_PROMPTS / TASK_SCHEMAS / TASK_SEMANTIC_VALIDATORS. Provider paths
  inherited from run_agent_json_task (thread-local system_override, try/finally
  cleanup on success/exception/budget skip). `load_schema` gained `.schema.json`
  append tolerance (bare-name callers like the p1 test resolve
  schemas/risk_adjustment_review.schema.json; purely additive).
- `LLM_PROMPTS.md` §4.2/§6 — 12-task inventory + new task entry (Schema,
  semantic hook, output contract, post-gates). `test_pg_08_04_prompt_audit_d.py`
  `_TASKS` 11 -> 12 (D7 registry now 12).
- **Tests: 77 passed in 7.10s** across 5 files (proposal 24 + context 16 +
  prompt-audit 14 + normalizer + anti-pollution regressions). Full Step 5 scope
  54 passed in 1.40s.
- **Revert-fail (twice):** renamed schema + risk_committee -> 24 failed ->
  restored -> 38 passed; renamed risk_context.py -> 14 failed -> restored ->
  16 passed. The 2 thread-local cleanup tests stayed green during the second
  revert because they exercise the existing 08-04 D4 finally in
  run_agent_json_task (no risk_context import).
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task
  #43 completed; next is Step 6 (read-only evidence rounds / AnalysisToolBroker,
  #44).

## Task 08-10 Step 6 COMPLETE: read-only evidence rounds (08-11)

- **Contract**: design §6.2/§8 + prd P1-4. The risk proposal LLM may request ONLY
  enumerated read-only broker methods through a structured tool-request schema;
  results carry source/as-of/age/trust/schema metadata; failed or stale tool
  evidence cannot support approval; budget exhaustion -> `wait` (no further loop).
- **Frozen `AnalysisToolBroker.METHODS` preserved**: exactly 5 (asserted by
  `test_five_readonly_methods_ok`). The two new narrow reads live in a SEPARATE
  `RISK_READ_METHODS` set dispatched through `call()` — never touches `METHODS`.
- **New methods** (`tools/analysis_tool_broker.py`):
  - `confirmation_lifecycle_evidence(symbol, side, analysis_time_utc=None,
    max_age_ms=None)` — reads `repo.confirmation_lifecycle(...)` seam; repo row
    None -> `status:"absent"` (not stale); `age_ms > cap` (explicit or
    `DEFAULT_LIFECYCLE_MAX_AGE_MS=4h`) -> `BrokerStaleError` (propagates via
    `call()` re-raise tuple, never swallowed into read_failed).
  - `adaptive_risk_budget(symbol=None, analysis_time_utc=None)` — compact summary
    via `adaptive_risk_budget_summary(sym, as_of=at)` seam, never raw account rows.
  - Both results self-describe `source/as_of/age_ms/trust/schema_version` and are
    RESULT_SCHEMAS-validated (2 new entries; `test_result_schemas_conform` iterates
    a hardcoded calls list so additions are safe).
- **Structured gate** `validate_tool_request(request) -> (ok, err, normalized)`
  returns `{"method","params"}`; rejects non-dict request/params, non-enumerated
  method, unknown/extra keys, wrong types, bad side enum (`_ALLOWED_SIDES`).
- **Executor** `run_risk_supplement_round(broker, requests, symbol,
  analysis_time_utc, max_requests=MAX_RISK_TOOL_REQUESTS=6)`: over-capacity ->
  `BrokerRoundLimitError`; invalid/unknown -> `BrokerForbiddenError`; execution
  broker errors (stale/param/size/schema/timeout) -> `ok=False`
  `error="evidence_failed"` (round never approvable); round time injected when
  param allowed + absent; every result stamped `env["meta"]`.
- **Tests**: `test_pg_08_10_evidence_rounds_p1.py` 28 passed (4 classes).
  RED start: 26 failed / 2 passed (the 2 = pre-existing write-forbidden +
  offline-source regressions). Network/import source-scan proves no external
  MCP/network dependency.
- **Revert-fail (three probes, each restored -> 28 passed again)**:
  - A: dropped method enumeration -> `test_unknown_method_rejected` 1 failed RED
    (unknown-param backstop kept `web_search` rejected too — defense-in-depth).
  - B: neutralized round-limit cap -> `test_too_many_requests_fails_closed`
    1 failed RED (did not raise).
  - C: renamed both new read methods `_zz_*` -> 11 failed RED (all
    TestBrokerRiskReads + supplement-round tests) -> restored.
- **Regression**: skills_tools_e + reviewer_fixes_f + llm_risk_proposal_p1 +
  risk_context_isolation_p1 + prompt_audit_d + evidence_rounds_p1 =
  **111 passed in 51.89s**.
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task
  #44 completed; next is Step 7 (deterministic adjustment verifier, #45).

## 08-11 Step 7 — Deterministic adjustment verifier (task #45) COMPLETE

- **New module** `plugins/crypto_guard/risk/risk_adjustment_verifier.py`
  (module name matches the RED contract import; created as
  `adjustment_verifier.py` then renamed).
  `verify_risk_adjustment(*, candidate_plan, proposal, confirmation_lifecycle,
  snapshot, account_state, policy, decision_confidence)` -> immutable frozen
  `AdjustmentVerification{ok, adjusted_plan, monetary_risk_delta,
  final_risk_check, errors=(), reason_codes=(), effective_order_allowed}`.
  Pure/read-only: deep-copies the candidate, never mutates inputs, no writes.
- **Fingerprint** `candidate_plan_fingerprint(...)`: SHA-256 sorted-key compact
  JSON over symbol/side/entry/trigger/stop/TPs(price+ratio)/risk_percent/
  confirmation fingerprint/analysis time/`policy.contract_version`. Top-level
  proposal `candidate_fingerprint` must match EXACTLY; `candidate_fingerprint`
  inside `adjustments` is a structural rejection.
- **Allowlist** `ADJUSTABLE_FIELDS = {entry_price, stop_loss, take_profits,
  risk_percent, news_like_event_policy}`; any other key (forged identity,
  symbol/side, quantity) discards the plan. `wait`/`reject` construct no plan;
  `approve_as_is` forbids `adjustments` and deep-copies the candidate.
- **Risk budget**: stop never tightens (structural reject); wider stop scales
  `risk_percent = cand_risk×cand_dist/adj_dist`, capped by min(cand_risk,
  scaled, max_single_trade_risk_pct, max_total−open, proposed) ->
  `monetary_risk_delta ≤ 0` always. Reason code `minimum_stop_distance` emitted
  when the mitigation applies.
- **Hard gates** independently enforced: lifecycle valid (SimpleNamespace/dict
  safe), market data complete, extreme regime (news_like_event adaptive -> only
  explicit dict `news_like_event_policy` with truthy `allow` neutralises),
  account (enabled/not paused/drawdown > −3.0/open_orders < max), geometry,
  entry deviation (pct AND ATR), stop min pct + ATR buffer + max pct + max ATR,
  TP geometry/ratios ∈ (0,1] sum ≈ 1, min RR. Then FULL existing
  `validate_trade_plan` re-run is the last word; engine reasons appended.
- **effective_order_allowed** = `(mode == "paper_bounded") and ok`; shadow/off
  never authorise an order.
- **RED -> GREEN**: 24 failed (module missing) -> 24 passed in 3.27s.
- **Regression**: risk_policy_p2 + confirmation_lifecycle_p1 +
  llm_risk_proposal_p1 = **85 passed in 145.75s**.
- **Revert-fail proofs (both restored -> 24 passed again)**:
  - A (risk increase): inverted scaling -> `test_wider_stop_reduces_risk_percent`
    1 failed RED (risk_percent stayed 0.5 ≠ 0.3214285714285714).
  - B (gate bypass): neutered `_gate_confirmation_lifecycle` ->
    TestConfirmationLifecycleGate 3 failed RED (expired/invalidated/absent all
    ok=True).
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task
  #45 completed; next is Step 8 (pipeline and rollout integration, #46).

## 08-11 Step 8 — Pipeline and rollout integration (task #46) COMPLETE

- **New pure gate** `run_ga_workers.risk_advisory_order_allowed(*,
  proposal_verified, final_risk_check_ok, plan_execution_state,
  account_gate_open, regime_gate_open, once_ever_open, mode)` encodes design §7
  final conjunction. `mode == "off"` -> True (legacy gate decides
  byte-for-byte); `mode != "paper_bounded"` -> False (shadow/unknown fail
  closed); paper_bounded requires EVERY term True.
- **Handler enforcement** in `handle_opportunity_watch_recheck`: the
  `risk_advisory` envelope (system-only, stamped AFTER LLM schema validation,
  never authorable by the LLM) is enforced ONLY when the decision carries it;
  no-envelope decisions keep the legacy path byte-for-byte. The check sits
  between `_recheck_order_gate` and the VETO-only broker verifier, before
  `create_paper_order(trigger_watch_id=...)`. shadow -> rejected, no alert
  outbox row; paper_bounded requires proposal ok + verification_ok +
  final_risk_check_ok else rejected with the ORIGINAL deterministic plan
  retained (failed LLM never reuses a prior adjusted plan). Once-ever/CAS/
  ownership/task-lock idempotency untouched.
- **news_like_event tension resolved**: adaptive gate may honor an LLM proposal
  allowing news, but the final engine rerun (`validate_trade_plan`,
  EXTREME_REGIMES) is the last word -> `final_risk_check_ok=False` ->
  paper_bounded never orders in a news regime (确认 != 下单).
- **Lifecycle geometry defect fixed**: LTC fixture 45.51 entry over 45.34 event
  = 0.375% deviation, above old `max_entry_deviation_pct` 0.30 (default +
  production config) -> carried confirmation invalidated as
  `geometry_mismatch`. Raised default + `config/trading_mode.yaml` to 0.50,
  updated the Step-2 default assertion. Step 7's own gate compares ADJUSTED vs
  CANDIDATE entry (unaffected); Step 4's 2.56% mismatch test still fails.
- **RED -> GREEN**: 16 failed (10 ImportError missing pure gate + 4
  handler-envelope RED + 2 LTC geometry_mismatch) -> 22 passed.
- **Regression**: rollout + risk_policy + confirmation_lifecycle +
  risk_adjustment_verifier = **107 passed in 217.76s**; existing recheck-handler
  suite (08-04 semantics/bridge/reviewer_fixes, 08-06 trigger_once_ever, 08-08
  watch_trigger_e2e + recheck_diagnostics) = **83 passed in 527.05s**.
- **Revert-fail proof**: neutered `risk_advisory_order_allowed` (paper_bounded
  -> always True) -> **9 failed** RED (6 negative gate tests: proposal-not-
  verified / final-risk-not-ok / plan-not-confirmed / account-closed /
  regime-closed / once-ever-closed; + 3 handler refusals: hard blocker even
  when LLM approves, provider/tool/schema failure, failed-proposal-never-
  reuses). Restored -> 22 passed.
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task
  #46 completed; next is Step 9 (persistence, reports, notifications,
  diagnostics, #47).

### 08-11 Step 9 — Persistence, reports, notifications, diagnostics (task #47) COMPLETE

- **Producer (persistence gap)**: research `producer-gap-2026-08-11.md` — the
  committed code had NO production writer for the four audit keys (only read
  sites: envelope gate, hourly report, diagnostics). Added `_attach_risk_governance`
  in `run_ga_workers.py` wired at the end of `_run_recheck_analysis` (watch-recheck
  seam ONLY): `mode=off` returns the SAME dict untouched (legacy byte-for-byte);
  shadow/paper_bounded ALWAYS stamps the system-only envelope
  `{mode, proposal_status, verification_ok, final_risk_check_ok}` AND persists the
  four audit keys + `policy_version` + `llm_latency_ms` + sorted `evidence_ids` via
  new narrow repo UPDATE `update_ga_decision_risk_governance`
  (COALESCE||jsonb merge by ga_decision_id); LLM/schema/provider/verifier/producer
  exception fails closed to `proposal_status="failed"`; no candidate ->
  `proposal_status="no_candidate"` no LLM round. `_MERGED_RESULT_INTERNAL_KEYS`
  stripped before `parse_risk_adjustment_review`; `llm_status != "ok"` never parses
  the deterministic fallback. `KNOWN_REASON_CODES` (10) compiled in `risk_policy.py`.
  RED-first `test_pg_08_10_risk_advisory_producer_p1.py` (9 tests): RED 9 failed ->
  GREEN `9 passed in 65.13s` -> revert-fail `9 failed in 55.69s` -> restore
  `9 passed in 53.64s` (all `-p no:cacheprovider`).
- **Affected regression**: rollout + policy + proposal + diagnostics +
  watch-recheck-diagnostics = **111 passed in 316.39s**.
- **Diagnostics** (`diagnostics/llm_risk_governance.py`): seven marker-gated gates
  (carried-without-provenance, survived-expiry, immutable-change, unknown-evidence,
  accepted-positive-delta, order-without-verifier-success, starvation split
  legitimate-rejection vs system-failure >=3) + `{name}_contract_marker_missing`
  fail-closed + `diagnose_llm_risk_governance` aggregate.
  `test_pg_08_10_llm_risk_diagnostics_p2.py` 23 tests. Per-diagnostic revert-fail:
  neutered all seven gates -> **8 failed** RED (one fires-test per gate +
  no-evidence-ids fail-closed; 15 clean/out-of-scope still passed) -> restore ->
  **23 passed in 142.39s**.
- **Funnel/report/notification**: hourly funnel counters + bounded reason rendering;
  order notification carries original/adjusted prices, effective risk, quantity, TP
  list, confirmation source/age, final verifier result. Observation triggers stay
  silent (`TestObservationTriggersSilent`: send_message spy never fires, no
  alert_outbox row).
- **Migration markers**: `llm_risk_proposal_contract_v1` +
  `risk_adjustment_verifier_contract_v1` + `llm_risk_context_isolation_contract_v1`
  registered after the health gate (LAST); mirrored in `test_pg_migrations.py`
  EXPECTED_MARKERS + Phase H/I scratch-marker sets.
- **Phase H/I**: `_phase_h_fault_inject` **37/37 faults verified** (five 08-10
  markers seeded via `_ensure_all_contract_markers`, no interference; 08-06/08-08
  marker-missing still fire); `_phase_i_fresh_verify` **SUCCESS — fresh DB clean**
  (schema health ok, state consistency 0 issues, report accuracy 0 issues).
- implement.md Step 9 all 11 checkboxes marked [x] with evidence.
- Four states unchanged: implementation_complete=false, final_seal_complete=false,
  production_ready=false, production_recovered=false. Staged area empty. Task #47
  completed; next is Step 10 (efficient verification chain + fresh reviewer, #48).

### 08-12 08-10 task reviewer re-dispatch: P2-1/P2-2/Recommended-1 closures (round 2)
- Fresh reviewer findings ALL closed RED-first with real revert-fail proofs.
- P2-1 (prior session): committee blocker-acknowledgment completeness guard
  (`candidate_adaptive_blockers` + `risk_committee` missing-guard) — revert 3 RED
  -> restore 53 GREEN (`producer_p1` + `llm_risk_proposal_p1`).
- P2-2 (this session): persist-loss false negative. `paper_orders.risk_advisory_mode`
  TEXT column (NULL=legacy, off=governance-off, paper_bounded/shadow=governance-ran);
  producer recheck bridge passes `risk_advisory_mode=ra_mode`; diagnostics flags an
  order whose mode says governance ran but whose owning decision lost the audit row.
  - schema_postgres.sql line 438 + migrations `_apply_08_12_risk_advisory_mode_migration`
    wired into initialize_database (idempotent), _REQUIRED_COLUMNS + __all__ + public wrapper.
  - `_EXPECTED_SCHEMA_FINGERPRINT` regenerated from fresh scratch schema ->
    9a76f65db2c8903a3e091e98c68f408562c7dd9f0aac80e75476e6ed49637465.
  - repository.create_paper_order + risk_advisory_mode kwarg (19-placeholder INSERT).
  - 3 new diagnostics tests (fires / mode-NULL clean / mode-off clean) + order-bridge
    assertion `order["risk_advisory_mode"] == "paper_bounded"`.
  - revert-fail: diagnostic branch `if False:` -> 1 RED; producer pass removed -> 1 RED
    (None == 'paper_bounded'). Restored -> 1+1 GREEN.
- Recommended-1 (this session): direct unit test
  `test_fingerprint_absent_confirmation_never_collides` (determinism / distinctness /
  64-hex shape / dict-path equivalence). Revert to unconditional strict call ->
  1 RED (KeyError 'symbol'); restored -> 1 GREEN.
- Evidence: diagnostics file `29 passed in 156.09s`; producer file `20 passed in
  96.56s`; 4-file union `127 passed in 285.18s`; full 12-file 08-10 union re-run.
- Research: `research/fresh-reviewer-p2-1-p2-2-rec1-closure-2026-08-12.md`.
- Working tree UNCOMMITTED; staged area empty; no git add/commit/push, no production
  migration, no marker writes, no service control. STOP at commit auth.
