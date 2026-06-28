# Fix: Hourly Report Accuracy — Round 3 (P0×3, P1×6, P2×3)

> 终审 R2 仍发现 12 个缺陷，worker 自阻塞为首要阻断项。

---

## P0

### P0-1: 小时报告阻塞唯一后台 Worker

**现状**: `_await_batch_completion` 在 `build_hourly_report` 内同步轮询等待最多 300s。`hourly_feishu_report` 调度优先级高于行情分析，但共用唯一后台 Worker。等待期间该 Worker 无法执行任何其他任务，形成自阻塞。

**修复**: 改为"批次未结束则重新入队"模式：
- `_await_batch_completion` 不再轮询等待，只查一次状态
- 若 batch 未完成 → 返回 `incomplete=True`，`build_hourly_report` 调用 `repo.enqueue_job("hourly_feishu_report", ..., delay_seconds=poll_interval)` 重新入队
- 设置最大重试次数（默认 12 次 × 30s = 6 分钟），超过后强制渲染（标记 incomplete）
- Worker 内不调 `_short_sleep`

**文件**: `hourly_report.py`、`run_ga_workers.py`（hourly_feishu_report handler 适配重入）

---

### P0-2: 精确批次查询串批/重复

**现状**: `latest_ga_decisions_by_symbol` 的子查询用 `batch_id` 过滤，但外层 JOIN 只用 `symbol + MAX(analysis_time)`。同时间有其他批次或重复决策时可能混入。

**修复**: 使用 `ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY analysis_time DESC, id DESC)` 窗口查询，所有过滤条件（`batch_id`、`min_analysis_time`）放在窗口查询内部，只取 `rn=1`。

```sql
SELECT * FROM (
    SELECT gd.*, ROW_NUMBER() OVER (
        PARTITION BY gd.symbol ORDER BY gd.analysis_time DESC, gd.id DESC
    ) AS rn
    FROM ga_decisions gd
    WHERE batch_id = ? AND analysis_time >= ?
) WHERE rn = 1
ORDER BY analysis_time DESC, id DESC
LIMIT ?
```

**文件**: `repository.py`

---

### P0-3: emergency_down 在 risk_gate.check 之前计算，始终为假

**现状**: `controller.py:405-416` 从 `legacy.get("hard_risk_off")` 读值，但 `hard_risk_off` 是 `risk_gate.check()` 的输出，此时 risk gate 还没跑。LLM agent 输出的 `legacy` 里不会有 `hard_risk_off` 字段，所以 `emergency_down` 始终为 False。

**修复**: 重排执行顺序：先 `risk_gate.check()` → 再 `grade_with_hysteresis(emergency_down=...)`。从 risk gate 返回的 `risk["account_risk"]["hard_risk_off"]` / `risk["account_risk"]["daily_loss_pause"]` 计算 `emergency_down`。

**文件**: `controller.py`

---

## P1

### P1-4: 包含失败品种的批次标记为 success

**现状**: `run_ga_workers.py` 的成功/异常路径都传 `status="success"` 给 `finish_analysis_batch`。

**修复**: 检查 `batch_symbol_status` 中是否有 `status='failed'` 行：
- 全部 completed → `status="success"`
- 有 completed + failed → `status="partial_failed"`
- 全部 failed → `status="failed"`

**文件**: `run_ga_workers.py`、`repository.py`（可能新增 `batch_has_failures(batch_id)` 辅助方法）

---

### P1-5: 默认测试真实等待 300 秒

**现状**: 测试未 mock `_short_sleep`，直接跑会卡 5 分钟。

**修复**: 在 `HourlyReportAccuracyTest` 中 mock `_short_sleep` 为 no-op；或使用状态机测试（直接构造 batch_state dict，不走 await）。确保 `HOURLY_REPORT_BATCH_GATE_TIMEOUT` 不设时测试也能在合理时间内完成。

**文件**: `tests/test_smoke.py`

---

### P1-6: 方向翻转诊断被自身条件抵消

**现状**: bullish↔bearish market_bias 翻转被当成"已确认"，导致真正未经收盘确认的翻转不报警。

**修复**: `market_bias` 翻转不作为独立确认源。只有以下证据算确认：
- `counter_evidence`/`evidence` 中的收盘 K 线突破词
- SMC BOS/CHoCH 事件

`market_bias` 翻转本身只是"翻转发生了"的证据，不是"翻转有结构性确认"的证据。

**文件**: `report_diagnostics.py`

---

### P1-7: 摘要与执行状态冲突诊断使用弱校验

**现状**: `_check_summary_execution_state_conflict` 用 `isinstance(plan, dict) and bool(plan)` 判断 plan_ok。

**修复**: 导入并使用 `is_valid_trade_plan(plan)` 代替简单非空检查。

**文件**: `report_diagnostics.py`

---

### P1-8: 不可交易摘要仍依赖关键词替换

**现状**: `rewrite_inconsistent_summary` 用黑名单替换。LLM 可能产出"满足创建条件"、"可创建模拟盘空单"、"提供入场窗口"等未收录措辞，黑名单永远追不全。

**修复**: 当 `execution_eligible` 为 False 时，不依赖黑名单替换，而是直接生成确定性观察摘要：
- 保留原始 `final_summary` 作为 `raw_summary` 存入 `rendered_summary`
- 生成新的确定性摘要：`"[观察] {symbol} {grade}级 {decision}；{gate_blockers}"` 写入 `rendered_summary`
- 报表一律使用 `rendered_summary`

**文件**: `report_consistency.py`、`hourly_report.py`

---

### P1-9: 回退批次时间元数据失真

**现状**: 当前 batch 不存在时回退到上一批数据，但仍用当前时段的 `analysis_time`，且 `min_analysis_time` 可能查不到上一批决策。

**修复**: 回退时使用上一批的 `analysis_time` 和 `batch_id`，而非当前时段的。`min_analysis_time` 基于回退批次的 `analysis_time` 计算。

**文件**: `hourly_report.py`

---

## P2

### P2-10: batch_symbol_status 缺少状态 CHECK 约束

**修复**: 添加 `CHECK(status IN ('pending', 'completed', 'failed'))` 到表定义和迁移。

**文件**: `schema.sql`、`migrations.py`

---

### P2-11: 部分外部通知仍显示负数回撤

**修复**: 全部回撤显示路径统一使用 `abs()`：风险状态行、drawdown 告警、hourly report。

**文件**: `hourly_report.py`、`run_ga_workers.py`（drawdown alert handler）

---

### P2-12: 生产库尚未迁移

这是操作项，不是代码修复。部署时需：
1. 备份 DB
2. 运行 `initialize_database()`
3. 验证 `check_schema_health()` 通过
4. Dry-run 小时报告

---

## 测试计划

| # | 测试 | 覆盖 |
|---|------|------|
| 1 | `test_hourly_report_requeues_on_incomplete_batch` | P0-1: 不等待，重新入队 |
| 2 | `test_latest_decisions_row_number_no_cross_batch` | P0-2: 窗口查询不串批 |
| 3 | `test_emergency_down_from_risk_gate` | P0-3: risk gate 后 hysteresis 生效 |
| 4 | `test_batch_status_partial_failed` | P1-4: 有 failed symbol 时 status=partial_failed |
| 5 | `test_await_batch_no_real_sleep` | P1-5: 测试不真实睡眠 |
| 6 | `test_direction_flip_bias_not_confirmation` | P1-6: bias 翻转不算确认 |
| 7 | `test_diagnostics_uses_is_valid_trade_plan` | P1-7: 冲突诊断用完整校验 |
| 8 | `test_non_executable_gets_deterministic_summary` | P1-8: 非执行获得确定性摘要 |
| 9 | `test_fallback_batch_uses_own_time` | P1-9: 回退用上一批时间 |
| 10 | `test_batch_symbol_status_check_constraint` | P2-10: CHECK 约束 |

---

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `notify/hourly_report.py` | P0-1: 重入队替代轮询; P1-9: 回退批时间; P2-11: abs() drawdown |
| `run_ga_workers.py` | P0-1: hourly_feishu_report 重入适配; P1-4: batch status; P2-11: drawdown alert |
| `storage/repository.py` | P0-2: ROW_NUMBER 窗口查询; P1-4: batch_has_failures |
| `ga_master/controller.py` | P0-3: risk_gate 先于 hysteresis |
| `notify/report_consistency.py` | P1-8: 确定性摘要生成 |
| `diagnostics/report_diagnostics.py` | P1-6: bias 非确认; P1-7: is_valid_trade_plan |
| `storage/schema.sql` | P2-10: CHECK 约束 |
| `storage/migrations.py` | P2-10: CHECK 约束迁移 |
| `tests/test_smoke.py` | 全部测试 |
