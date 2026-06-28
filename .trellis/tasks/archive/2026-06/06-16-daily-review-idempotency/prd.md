# brainstorm: daily review idempotency

## Goal

消除每日模拟盘复盘的重复执行问题：同一个 review_date 的 daily_review 被反复入队、反复推送飞书、反复写入 skill_feedback_memory（今天已产生 1540 条重复记忆），导致自进化闭环吃进大量低质量噪声。

## What I already know

### 5 个根因（用户诊断 + 代码验证）

1. **`paper_position_updater.py:123` fallback 时间窗口错误**
   - `_ensure_daily_review()` 检查 `agent_jobs.created_at >= yesterday AND < today`
   - fallback job 的 `created_at` 是今天（入队时间），不在昨天窗口内
   - 结果：每次都查不到已有 job，每次 `update_paper_positions` 都入队新的 fallback job
   - 实际影响：6/15 当天产生了 308 个成功的 fallback job

2. **`repository.py:654` `enqueue_job()` 无 job 级幂等性**
   - 没有 UNIQUE 约束或 `INSERT OR IGNORE`
   - 相同 `session_id` 可以无限次插入
   - scheduler 的 `daily_review` job (`run_scheduler.py:76`) 也使用相同的 `system:scheduled:daily` session_id，会被重复入队

3. **`run_ga_workers.py:198` `process_job` 对 daily_review 总是推送飞书**
   - 不检查 `daily_review_reports.pushed_to_feishu`
   - 每次 worker 处理 daily_review job 都会执行 `send_markdown_alert`
   - 77 条 daily_review outbox 消息今天被发送

4. **`alert_delivery.py:64` dedupe_key 过粗**
   - 当前: `f"{symbol or '-'}:{alert_type}"` → `-:daily_review`
   - 应该: `daily_review:{review_date}` → 用 review_date 区分不同天的推送
   - 目前所有天的 daily_review 共享同一个 dedupe_key，静默期内不同天的推送会被错误去重

5. **`daily_reviewer.py:12` `run_daily_review()` 不幂等**
   - 每次都重写 `skill_feedback_memory`（每个 skill 一条，5 个 skill = 5 条/次）
   - 每次都 `save_daily_review_report()`（虽有 `ON CONFLICT(review_date) DO UPDATE`，但每次覆盖）
   - 没有 `force=False` 参数来跳过已完成的 review
   - 实际影响：1540 条 "无平仓样本" skill_feedback_memory 今天被写入

### 补充发现

6. **`run_scheduler.py:76` scheduler daily_review 也缺幂等**
   - `session_id = "system:scheduled:daily"` 固定值
   - scheduler 可能因重试或重启多次入队同一 job
   - 应该用 `system:scheduled:daily:{date}` 来防止同一天多次调度

7. **`paper_position_updater.py:155` `_check_daily_loss_trigger()` 的 job 也有同样问题**
   - `session_id = f"system:paper:daily_loss:{today}"` — 这个倒是带日期了，但 `enqueue_job` 本身没有去重

## Assumptions (temporary)

- `daily_review_reports` 表已有 `UNIQUE(review_date)` 约束和 `pushed_to_feishu` 字段
- `save_daily_review_report()` 已有 `ON CONFLICT(review_date) DO UPDATE`
- fallback job 和 scheduler job 的修复不会影响正常流程
- 命名修改（P1）是简单的重命名，不影响核心逻辑

## Decision (ADR-lite)

### D1: job 级幂等 — DB 层 UNIQUE 约束

**Context**: `enqueue_job()` 无去重，同 session_id 可无限插入。
**Decision**: `ALTER TABLE agent_jobs ADD UNIQUE INDEX idx_agent_jobs_dedupe (job_type, session_id)` + `INSERT OR IGNORE`
**Consequences**: session_id 设计上已是业务唯一标识（如 `system:paper:daily_fallback:{date}`），失败重试不靠重新 insert 同 session_id，而是 update status 回 pending。所有 scheduler 固定 session_id 改为包含日期。

### D2: `run_daily_review(force=False)` — 已存在则直接返回

**Context**: 每日复盘语义是"每天一次的正式结算"，不是滚动更新。允许同一天反复重算会重新打开重复推送、重复记忆、重复触发自进化。
**Decision**: `force=False`（默认）时，如果 `daily_review_reports.review_date` 已存在：直接返回已有报告，不调 LLM，不写 skill memory，不跑 evolution trigger，不更新 report。返回字段加 `idempotent: true, existing: true`。`force=True` 仅用于手动 `manual_rebuild_daily_review(day_utc, reason)`。
**Consequences**: 如果后来补了 trade review，不会自动更新 daily review，需要显式 rebuild。

### D3: 已有污染清理 — 软清理 + 幂等 + 可审计

**Context**: bug 已产生大量重复数据（308 agent_jobs, 1540 skill_feedback_memory, 77 alert_outbox），修复代码但不清理会让污染继续误导报表和自进化输入。
**Decision**: 在 migration 中新增 `_cleanup_daily_review_duplicates()` 函数，自动执行软清理（标记 status，不物理删除）。
**Consequences**: 
- agent_jobs: 同 `job_type='daily_review'` + 同 `payload.day_utc` + 同 `session_id`，保留最早 success 或最新 pending，其余标记 `status='duplicate'`
- skill_feedback_memory: 仅清理 `source_type='daily_review'` 且 finding 为 "无平仓样本"/"无显著亏损" 的重复条目，每个 `review_date + skill_name + finding` 保留一条，其余 `status='archived'`
- alert_outbox: `alert_type='daily_review'`，保留最早 sent 或最新 pending，其余标记 `status='duplicate'`
- daily_review_reports: 不动（已有 UNIQUE(review_date)）
- cleanup 结果写入 migration result JSON，可审计

## Open Questions

* [ ] P1 重命名 `_check_daily_loss_trigger` → intraday loss review 的 scope

## Requirements (evolving)

### P0: 核心幂等性修复

1. **修复 fallback 时间窗口** (`_ensure_daily_review`)
   - 检查 `daily_review_reports.review_date` 是否存在（按日期直接查，不依赖 `created_at` 时间窗口）
   - 或至少修正时间窗口逻辑为检查 `created_at >= yesterday`（不设上限）

2. **job 级幂等** (`enqueue_job_once()` + schema)
   - `ALTER TABLE agent_jobs ADD UNIQUE INDEX idx_agent_jobs_dedupe (job_type, session_id)`
   - 新增 `enqueue_job_once()` 方法：
     - 已有 `pending/running/success`：返回 existing id，不新增
     - 已有 `failed/cancelled/duplicate`：允许重置为 `pending`
   - 普通 `enqueue_job()` 保留给确实允许重复的任务
   - daily/hourly/scheduler/fallback 必须改用 `enqueue_job_once()`
   - scheduler 固定 session_id 改为包含日期: `system:scheduled:daily:{date}`
   - fallback session_id 已包含日期: `system:paper:daily_fallback:{date}` ✓

3. **daily_review worker 三层推送防线**
   - 第一层: `run_daily_review(force=False)` — 已有报告直接返回，不写 memory
   - 第二层: `process_job` 处理 `daily_review` 时，检查 `daily_review_reports.pushed_to_feishu`，已推送则 skip
   - 第三层: alert `dedupe_key=daily_review:{review_date}` — 发送层兜底

4. **alert dedupe_key 包含日期**
   - 改为 `daily_review:{review_date}` 格式
   - 需要从 job payload 中提取 `day_utc` 或从 result 中提取 `day_start_utc`

5. **`run_daily_review()` 幂等**
   - 新增 `force=False` 参数
   - `force=False` 时：如果 `daily_review_reports.review_date` 已存在，直接返回已有报告
     - 不调 LLM，不写 skill memory，不跑 evolution trigger，不更新 report
     - 返回字段加 `idempotent: true, existing: true, daily_review_report_id`
   - `force=True` 时（仅用于 `manual_rebuild_daily_review(day_utc, reason)`）：重新执行完整流程
     - 重建时先归档旧 skill_feedback_memory（标记 status='archived'）再写新的

### P1: 命名修正 — `intraday_loss_review` 与 `daily_review` 分离

6. **新增 `intraday_loss_review` job_type**
   - `_check_daily_loss_trigger()` 入队的 job_type 从 `daily_review` 改为 `intraday_loss_review`
   - 语义: 盘中止血告警，不是日终复盘
   - 行为:
     - 推送风险告警/止损摘要到飞书
     - 调用 `evaluate_evolution_triggers()` 创建/更新 trigger
     - **不写** `daily_review_reports`
     - **不写** `skill_feedback_memory`
     - **不跑** `run_daily_review()`
   - 去重: `dedupe_key=f"intraday_loss_review:{day_utc}:{loss_count_bucket}"`（3_loss / 5_loss）
   - 每个自然日每个 bucket 最多一次
   - `session_id = f"system:paper:intraday_loss:{today}:{loss_count}"`（含日期+loss count，配合 UNIQUE 约束）

## Acceptance Criteria (evolving)

* [ ] 同一天的 `daily_review` fallback job 不会被重复入队（`_ensure_daily_review` 正确检查）
* [ ] scheduler 的 `daily_review` job 同一天不会被重复入队（session_id 包含日期）
* [ ] `run_daily_review(force=False)` 对已有报告的日期直接返回，不写 skill memory
* [ ] daily_review 飞书推送同一 review_date 不会被重复发送（dedupe_key 包含日期）
* [ ] worker 处理 daily_review 时检查 `pushed_to_feishu`，避免重复推送
* [ ] 已有污染可清理（SQL 脚本或 cleanup 函数）
* [ ] 现有 daily_review 正常流程不受影响
* [ ] 测试覆盖

## Definition of Done (team quality bar)

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes
* Rollback considered if risky

## Out of Scope (explicit)

* 不改变 daily_review 的分析逻辑本身
* 不改变调度频率或触发条件
* 不涉及 evolution/algo 逻辑变更

## Technical Notes

### Files inspected

| 文件 | 关键行 | 问题 |
|------|--------|------|
| `paper_position_updater.py` | 123-152 | `_ensure_daily_review()` 时间窗口错误 |
| `paper_position_updater.py` | 155-192 | `_check_daily_loss_trigger()` session_id 带日期但 enqueue_job 无去重 |
| `repository.py` | 654-686 | `enqueue_job()` 无幂等保护 |
| `run_ga_workers.py` | 198-214 | daily_review handler 不检查 pushed_to_feishu |
| `alert_delivery.py` | 64-69 | dedupe_key = `-:daily_review` 过粗 |
| `daily_reviewer.py` | 12-82 | `run_daily_review()` 每次都写 skill memory |
| `daily_reviewer.py` | 171-260 | `_write_skill_memory_updates()` 每个 pattern 写一条，无日期去重 |
| `run_scheduler.py` | 75-78 | scheduler daily_review session_id = `system:scheduled:daily` 固定值 |
| `schema.sql` | 473-491 | `daily_review_reports` 已有 UNIQUE(review_date) |
| `schema.sql` | 497-512 | `agent_jobs` 无 UNIQUE(job_type, session_id) |
| `schema.sql` | 529-544 | `alert_outbox` 有 dedupe_key 列和索引 |

### 数据流追踪

```
scheduler (daily_review)
  → enqueue_job("daily_review", session_id="system:scheduled:daily")
  → agent_jobs 表

paper_position_updater (每次更新都调用)
  → _ensure_daily_review()
  → 检查 agent_jobs.created_at 时间窗口（BUG: 永远查不到）
  → enqueue_job("daily_review", session_id="system:paper:daily_fallback:{yesterday}")
  → 308 个重复 job

worker (process_job)
  → daily_review handler
  → run_daily_review() → 写 skill_feedback_memory (5 skills × N 次 = 大量重复)
  → send_markdown_alert(alert_type="daily_review") 
  → dedupe_key="-:daily_review" → 不同天的推送共享同一个 key
```
