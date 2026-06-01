# 测试计划

## 1. 单元测试

必须覆盖：

- GADecision schema validation
- feishu action builder
- risk gate
- next candle analysis time
- key levels validator
- Redis adapter fallback
- Parquet archive merge dedupe
- DuckDB read parquet
- Skill tools deterministic outputs

## 2. 集成测试

### 用户临时分析 D 级

输入：`分析 AGTUSDT`

期望：

- 写入 `ga_decisions`
- signal_grade = D
- 按钮只有 `加入长期产品池` 和 `忽略`
- 不创建 opportunity_watch
- 不创建 paper_order

### 用户临时分析 B 级

期望：

- 显示 `加入机会监控`
- 用户点击后才创建 opportunity_watch

### A/S trade_plan 完整

期望：

- 风控通过
- 显示 `加入模拟盘`
- 用户点击后创建 paper_order
- paper_order 引用 ga_decision_id

### 每小时播报

期望：

- 摘要格式
- 不展开 D 级长文本
- 包含 Redis / Parquet / DuckDB 状态

### Redis 断开

期望：

- 系统不崩溃
- fallback SQLite
- `/status` 显示 degraded

### Parquet / DuckDB

期望：

- 产生 parquet 文件
- DuckDB 查询成功

## 3. 回归测试

- MVP 原有飞书交互不破坏。
- 原有模拟盘查询不破坏。
- 原有定时任务不破坏，但最终决策必须进入 GA Master Controller。
