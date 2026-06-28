# Fix: Hourly Report Accuracy — 15 Review Issues (P0×7, P1×5, P2×3)

> 终审发现 `3677a6f` 存在 15 个缺陷，旧库迁移必崩为首要阻断项。

---

## P0 — 生产阻断

### P0-1: 旧库迁移顺序必崩

**现状**: `initialize_database()` 先 `executescript(schema.sql)`，后调 `_apply_hourly_report_accuracy_migration()`。`schema.sql:177` 已包含 `CREATE INDEX ... ON ga_decisions(batch_id)` — 但旧库的 `ga_decisions` 尚无 `batch_id` 列（该列在 migration 的 `_add_column` 里添加）。生产库重启直接 `OperationalError: no such column: batch_id`。

**修复**: 将 `_apply_hourly_report_accuracy_migration()` 调用移到 `executescript` **之前**（与 `_apply_stop_loss_adjustment_dedup` 同位），且 migration 内 `_add_column` + `CREATE TABLE IF NOT EXISTS analysis_batches` 都带幂等守卫。`schema.sql` 里的 `CREATE INDEX` 加 `IF NOT EXISTS` 即可。

**验证**: 在无 `batch_id`/`previous_grade`/`rendered_summary` 列的旧库上跑 `initialize_database()` 不报错；`check_schema_health()` 通过。

**文件**: `migrations.py`（调用顺序）、`schema.sql`（索引加 IF NOT EXISTS）

---

### P0-2: 批次完成记录并发丢失

**现状**: `mark_batch_symbol_completed()` (repository.py:380) 读 JSON → Python 修改 → 整列覆写。两个 worker 并发完成不同 symbol 时，后写的覆盖先写的新增 symbol。

**修复**: 改为原子 SQL 更新，不再读-改-写 JSON：

```sql
-- 独立明细表（推荐方案）
CREATE TABLE IF NOT EXISTS batch_symbol_status (
    batch_id  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'pending',  -- 'completed' | 'failed'
    updated_at TEXT,
    PRIMARY KEY (batch_id, symbol)
);
```

- `mark_batch_symbol_completed(batch_id, symbol, failed=False)` → `INSERT OR REPLACE INTO batch_symbol_status ...`
- `_await_batch_completion` 查 `batch_symbol_status` 而非 JSON 列
- 保留 `analysis_batches` 表的顶层状态列但去掉 `completed_symbols_json` / `failed_symbols_json`
- 迁移：从现有 JSON 填充明细表

**验证**: 两个连接并发 mark 不同 symbol → 两个 symbol 都在 `batch_symbol_status` 里。

**文件**: `schema.sql`、`migrations.py`、`repository.py`、`hourly_report.py`

---

### P0-3: 批次永远不会结束

**现状**: `finish_analysis_batch()` 只有定义和测试调用，生产代码无任何地方调用它。所有批次永久 `running`，`finished_at` 永远空。

**修复**: 在 `run_ga_workers.py` 的 `scheduled_market_analysis` handler 里，当该 batch 最后一个 symbol 完成/失败时，调 `finish_analysis_batch(batch_id)`。判定逻辑：查 `batch_symbol_status` 中该 `batch_id` 还有无 `status='pending'` 的行。

**验证**: 单 symbol batch 完成 → status 变为 `success`；多 symbol batch → 全部完成后变 `success`。

**文件**: `run_ga_workers.py`、`repository.py`（可能新增 `is_batch_complete(batch_id)` 辅助方法）

---

### P0-4: 已有 pending job 被提前标记完成

**现状**: `cron_scheduler.py:116` 遇到 pending/running job 就直接 `mark_batch_symbol_completed(batch_id, symbol)`。但该 job 可能尚未开始执行分析，报告会提前放行。

**修复**: 改为 `mark_batch_symbol_completed(batch_id, symbol, status='pending')` — 即在 `batch_symbol_status` 中标记为 `pending`（而非 `completed`），表示"已有 job 在跑但尚未完成"。`_await_batch_completion` 只将 `status='completed'` 的计入完成计数；`pending` 的仍需等待。

**验证**: skipped-pending symbol 在 `batch_symbol_status` 中为 `pending`，不阻止 batch 完成判定（因为 job 完成后会改为 `completed`）。

**文件**: `cron_scheduler.py`、`repository.py`、`hourly_report.py`

---

### P0-5: 报告没有按精确 batch 取决策

**现状**: `latest_ga_decisions_by_symbol(min_analysis_time=)` 不过滤 `batch_id`，可能混入手动分析、其他批次或同时间重复决策。

**修复**: 新增 `latest_ga_decisions_by_symbol(batch_id: str | None = None)` 参数，当提供 `batch_id` 时，SQL 加 `WHERE batch_id=?`。`hourly_report.py` 传 `_await_batch_completion` 返回的 `batch_id`。

**验证**: 多批次并存时，只返回指定批次的决策。

**文件**: `repository.py`、`hourly_report.py`

---

### P0-6: 不完整批次可能被标成完整

**现状**:
- 只有 `status='running'` 时才计算 `incomplete`；若 status 已变为 `success` 但仍缺 symbol，报告不标记 incomplete
- `_await_batch_completion` 返回值漏了 `completed_symbols`，报表完成数量一直显示 0

**修复**:
- `_await_batch_completion` 改用 `batch_symbol_status` 做精确判定：统计 `pending/completed/failed` 数量与 `enabled_symbols` 对比
- 返回值增加 `completed_count`、`total_count`、`pending_symbols`
- 不管 batch status 是什么，只要有 pending symbol 就标记 `incomplete=True`

**验证**: batch status=success 但仍有 pending symbol → incomplete=True

**文件**: `hourly_report.py`、`repository.py`

---

### P0-7: 评级迟滞使用了错误输入

**现状**: `controller.py:404` 把 `confidence`（0-1 置信度）当策略评分传给 `grade_with_hysteresis(current_score=confidence)`，会在 `grade_from_score` 中重新计算 grade。confidence=0.75 → grade_from_score → A（0.72），但如果原始信号评分算出的是 B（0.65），则迟滞逻辑用错误输入。`emergency_down` 从未接入。

**修复**:
- 传 `grade_with_hysteresis` 当前实际 grade（`legacy.get("signal_grade")`）而非 confidence score
- 修改 `grade_with_hysteresis` 签名：`grade_with_hysteresis(current_grade: str, previous_grade: str | None, *, emergency_down: bool = False) -> tuple[str, str]`
- 在 controller 中：当 `risk_check.hard_risk_off` 或 `risk_check.daily_loss_pause` 为 True 时，传 `emergency_down=True`
- 去掉 `current_score` 参数和 `grade_from_score` 内部调用

**验证**: confidence=0.65 + signal_grade=B → 迟滞基于 B 而非重新计算为 A

**文件**: `controller.py`、`grade_config.py`、`tests/test_smoke.py`

---

## P1 — 功能缺陷

### P1-8: 4H range/transition 仍允许 S 级

**现状**: `controller.py:433-436` 把 `transition`、`range`、`unknown` 都视为与 LONG/SHORT 不冲突，4H=range 时 S 级不受阻。

**修复**: 当 4H bias 为 `range` 或 `transition` 时，`htf_conflict` 应为 True（"高周期未确认方向"），除非 `independent_trend` 为 True。修改 controller 中 `htf_conflict` 计算逻辑：4H 在 `{bullish, bearish}` 之外时一律视为 conflict（`range`/`transition`/`unknown`/空值 → `htf_conflict=True`）。

**验证**: 4H=range + LONG side + no independent_trend → clamp_grade 降级

**文件**: `controller.py`

---

### P1-9: 确定性文案没有真正用于报表

**现状**: `_decision_row()` 和报表文本仍使用 `final_summary`；`rendered_summary` 已保存但从未展示。黑名单过窄，删除"风控全部满足"后仍可能留下"建议设置 limit short"等执行措辞。

**修复**:
- `_decision_row()` 优先使用 `rendered_summary`，fallback `final_summary`
- 扩展 `FORBIDDEN_EXECUTABLE_PHRASES` 加入高频执行措辞：`"建议设置 limit"`, `"建议设置 trigger"`, `"建议做多"`, `"建议做空"`, `"可开仓"`, `"可入场"`
- 报表渲染使用 `_decision_row` 返回的 `rendered_summary`

**验证**: risk_check=false 的决策在报表中显示 `rendered_summary`（含门禁覆盖条款）

**文件**: `hourly_report.py`、`report_consistency.py`

---

### P1-10: trade plan 只判断非空

**现状**: `execution_eligible()` 和 `_opportunity_classifier()` 用 `bool(has_trade_plan) and isinstance(plan, dict)` 判断，任意非空 dict（如 `{"note": "placeholder"}`）都通过。

**修复**: 新增 `is_valid_trade_plan(plan: dict | None) -> bool` 检查必要字段 `side`, `entry_type`, `entry_price`（或 `trigger_price`）, `stop_loss`, `take_profit`, `risk_reward_ratio`。在 `execution_eligible` 和 `_opportunity_classifier` 中复用。

**验证**: `{"note": "placeholder"}` → `is_valid_trade_plan` 返回 False

**文件**: `report_consistency.py`、`hourly_report.py`

---

### P1-11: 诊断系统性假阳性（5 项）

**11a**: stale 检查扫描最近 120 条历史记录而非本次报告行 → 大量无关 stale 标记
- 修复：仅扫描 `batch_id` 匹配当前报告 batch 的记录，或 `analysis_time` 在当前 batch 窗口内的

**11b**: 观察候选缺 plan/风控失败也被当作 executable 错误（`_check_executable_opportunity_without_trade_plan` 和 `_check_executable_opportunity_risk_rejected`）
- 修复：仅检查 `decision` 字段为 `create_paper_order` 或 `trade_plan_available` 的行（即自称可执行但实际缺门禁的）

**11c**: 方向翻转只在 `counter_evidence` 中寻找确认
- 修复：也检查 `market_bias` 变化、`smc_events` 中的 `BOS`/`CHoCH`、`trade_plan_json.side` 变化

**11d**: sell-side 向下扫、buy-side 向上扫被错误诊断为异常
- 修复：`_check_invalid_liquidity_sweep_semantics` 只检查显式方向矛盾词（"看空"+"sell_side"组合），不检查中性描述词（"向下"= 价格向下扫低点 = sell_side 的正常行为）

**11e**: `run_for_report()` 异常时返回 `ok=True`，属 fail-open
- 修复：异常时返回 `ok=False, error=str(exc)`

**文件**: `report_diagnostics.py`

---

### P1-12: 同步等待导致 worker 和测试卡住

**现状**: `_await_batch_completion` 默认 timeout 300s，测试未 mock 时间导致 >3min 超时。

**修复**:
- `_await_batch_completion` 在非调度模式下（`mode != "scheduled"` 或被测试直接调用时）使用短超时（30s）
- 新增环境变量 / 配置项 `HOURLY_REPORT_BATCH_GATE_TIMEOUT` 可覆盖
- 测试中 mock `time.sleep` 或直接调用 repo 方法构造 batch_state，不走 await

**验证**: 测试全部在 10 分钟内完成；batch gate 超时时报告标记 incomplete 并继续渲染

**文件**: `hourly_report.py`、`tests/test_smoke.py`

---

## P2 — 改进项

### P2-13: opportunity 的 age 使用 created_at 而非 analysis_time

**修复**: opportunity 行显示的 age 基于决策的 `analysis_time`（行情时间），不是 `created_at`（入库时间）。

**文件**: `hourly_report.py`

---

### P2-14: risk_off 状态行仍显示负号回撤

**修复**: 报表风险状态行的回撤显示统一为非负幅度（"回撤 2.8%"），内部符号约定不变。

**文件**: `hourly_report.py`

---

### P2-15: SMC 注释术语错误

**现状**: `smc_engine.py:13` 注释 "resting buy-stops below the prior low" — 低点下方挂的是 sell stops（多单止损），不是 buy-stops。

**修复**: 改为 "resting sell-stops (long stop-loss orders) below the prior low"。

**文件**: `analysis/smc_engine.py`

---

## 测试计划

| # | 测试 | 覆盖 |
|---|------|------|
| 1 | `test_migration_adds_columns_before_schema_index` | P0-1: 旧库迁移不崩 |
| 2 | `test_concurrent_batch_symbol_completion` | P0-2: 并发不丢 |
| 3 | `test_batch_finishes_when_all_symbols_done` | P0-3: 批次自动结束 |
| 4 | `test_skipped_pending_marks_pending_not_completed` | P0-4: 不提前标记完成 |
| 5 | `test_decisions_filtered_by_batch_id` | P0-5: 精确 batch 过滤 |
| 6 | `test_incomplete_batch_even_if_status_success` | P0-6: status=success 但缺 symbol 仍 incomplete |
| 7 | `test_grade_hysteresis_uses_actual_grade` | P0-7: 迟滞基于 grade 而非 confidence |
| 8 | `test_emergency_down_bypasses_hysteresis` | P0-7: hard_risk_off 触发 emergency_down |
| 9 | `test_4h_range_clamps_s_grade` | P1-8: 4H=range 时 S 被降级 |
| 10 | `test_report_uses_rendered_summary` | P1-9: 报表用 rendered_summary |
| 11 | `test_invalid_trade_plan_not_execution_eligible` | P1-10: 不完整 plan 不通过 |
| 12 | `test_stale_check_only_current_batch` | P1-11a: stale 只查当前 batch |
| 13 | `test_run_for_report_returns_ok_false_on_error` | P1-11e: 异常时 fail-closed |
| 14 | `test_batch_gate_timeout_configurable` | P1-12: 超时可配置 |

---

## 修改文件清单

| 文件 | 改动项 |
|------|--------|
| `storage/migrations.py` | P0-1: migration 调用前移；P0-2: 新增 batch_symbol_status 表迁移 |
| `storage/schema.sql` | P0-1: 索引加 IF NOT EXISTS；P0-2: batch_symbol_status 表声明 |
| `storage/repository.py` | P0-2/3/4/5/6: batch_symbol_status 原子操作 + batch_id 过滤 + is_batch_complete |
| `scheduler/cron_scheduler.py` | P0-4: skipped-pending 标记 pending 而非 completed |
| `ga_master/controller.py` | P0-7: grade_with_hysteresis 传 grade + emergency_down；P1-8: 4H range → conflict |
| `strategy/grade_config.py` | P0-7: grade_with_hysteresis 签名改为接收 current_grade |
| `notify/hourly_report.py` | P0-5/6: batch_id 过滤 + 精确 incomplete 判定；P1-9: 用 rendered_summary；P1-12: 可配置超时；P2-13/14 |
| `notify/report_consistency.py` | P1-9: 扩展 FORBIDDEN_EXECUTABLE_PHRASES；P1-10: is_valid_trade_plan |
| `diagnostics/report_diagnostics.py` | P1-11: 5 项假阳性修复 |
| `run_ga_workers.py` | P0-3: batch 完成时调 finish_analysis_batch |
| `analysis/smc_engine.py` | P2-15: 术语修正 |
| `tests/test_smoke.py` | 全部测试新增/修改 |

---

## 约束

- 不触碰真实订单路径
- 迁移必须幂等（旧库、新库、已迁移库都能跑）
- 491 测试基线 + 新增测试全部通过
- `check_schema_health()` 生产库通过
