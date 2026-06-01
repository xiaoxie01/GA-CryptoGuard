# Codex 总提示词：GA CryptoGuard 架构纠偏与完整实现

你是本项目的资深后端工程师、量化系统架构师和代码审查员。请基于当前仓库和本方案文档，对 GA CryptoGuard 进行架构纠偏、功能完善和验收。

## 文档包位置

请先阅读本目录下所有文件，重点阅读：
目录位置：E:\GenericAgent_crypto\ga_crypto_guard_codex_complete_plan
1. `README.md`
2. `IMPLEMENTATION_ORDER.md`
3. `01_architecture/GA_MASTER_CONTROL.md`
4. `01_architecture/ANTI_PATTERNS_AND_REFACTOR.md`
5. `03_storage/STORAGE_REDIS_DUCKDB_PARQUET.md`
6. `06_skills/SKILL_CONTRACT.md`
7. `13_acceptance/ACCEPTANCE_MATRIX.md`

## 用户指定环境
DuckDB、Redis已安装
- DuckDB 数据库目录：`D:\Program Files\duckdb`
- Redis 安装目录：`D:\Program Files\Redis`
- Redis URL：`redis://localhost:6379/0`
- Parquet 由项目自行处理，默认目录：`data/parquet/klines/binance_um/`

## 项目目标

构建一个由 GA 绝对主控、通过飞书交互的 Binance 加密货币合约自主分析与模拟交易系统。

必须满足：

1. GA 是核心决策中枢，负责逻辑推理、状态记忆、自进化。
2. 飞书是用户交互入口和预警出口。
3. 所有交易行为只进入模拟盘，不接实盘。
4. 工具层只计算客观事实，不直接生成最终交易决策。
5. 所有核心分析能力必须封装为 GA 动态 Skill。
6. Skill 不是单纯代码函数，而是 Prompt + Tool + Feedback Memory + Evolution Rule 闭环。
7. 所有最终动作必须来自 `GADecision`。

## 最高优先级规则

任何最终交易判断、机会监控建议、模拟盘动作、飞书按钮，都必须来自 GA Master Controller 输出的 `GADecision`。

禁止：

- Scheduler 直接生成交易结论。
- Strategy engine 直接创建 paper_order。
- Tool 直接输出 final decision。
- LLM 只作为文案总结器。
- D 级 / no_edge 自动建议机会监控。
- 用户未点击按钮就自动加入观察列表。
- 用户手动开仓绕过风控。

## 必须实现的模块

请新增或重构：

```text
plugins/crypto_guard/ga_master/
  controller.py
  context_builder.py
  skill_orchestrator.py
  decision_schema.py
  decision_persistence.py
  feishu_action_builder.py
  risk_gate.py
  report_adapter.py
```

动态 Skill：

```text
plugins/crypto_guard/skills/chanlun/
plugins/crypto_guard/skills/price_action/
plugins/crypto_guard/skills/smc_orderflow/
plugins/crypto_guard/skills/momentum/
plugins/crypto_guard/skills/trend_stage/
```

每个 Skill 必须包含：

- `skill.yaml`
- `prompt.md`
- `tools.py`
- `schema.json`
- `feedback_rules.yaml`

存储与基础设施：

- RedisAdapter
- ParquetKlineArchive
- DuckDBAnalytics
- SQLite migrations
- `/status` 健康检查

## 必须实现的 GADecision

新增 `ga_decisions` 表。

`GADecision` 至少包含：

- symbol
- analysis_time
- decision_type
- signal_grade
- confidence
- market_bias
- trend_stage
- decision
- skill_result_refs_json
- evidence_json
- counter_evidence_json
- risk_check_json
- trade_plan_json
- opportunity_watch_json
- feishu_actions_json
- final_summary
- created_by = ga_master_controller

强制规则：

1. 只有 GADecision 可以创建 paper_order。
2. 只有 GADecision + 用户按钮确认可以创建 opportunity_watch。
3. 只有 GADecision 可以进入 hourly_report。
4. 工具层和策略层不能直接创建最终 signal。
5. 所有飞书按钮必须来自 feishu_actions_json。

## 飞书按钮规则

用户主动分析后：

D/C：

- `[加入长期产品池]`
- `[忽略]`

B：

- `[加入机会监控]`
- `[加入长期产品池]`
- `[忽略]`

A/S 且 trade_plan 完整并通过风控：

- `[加入模拟盘]`
- `[加入机会监控]`
- `[加入长期产品池]`
- `[忽略]`

A/S 但 trade_plan 不完整：

- `[加入机会监控]`
- `[加入长期产品池]`
- `[忽略]`

禁止 D 级显示“加入机会监控”。禁止无 trade_plan 显示“加入模拟盘”。

## 每小时播报规则

每小时播报必须是摘要，禁止逐币输出完整模块长文。

必须包含：

1. 系统状态：scheduler、market data、Redis、SQLite、Parquet、DuckDB、Feishu queue。
2. 模拟盘摘要：equity、daily pnl、drawdown、open positions、pending orders。
3. 高等级机会：只列 S/A/B。
4. 当前机会监控。
5. C/D 无优势品种汇总，不展开长文。
6. 风险事件。

只有用户发送“详细分析 XXX”时，才允许展示完整模块明细。

## Redis 要求

Redis 安装目录：`D:\Program Files\Redis`

必须真正接入：

- 用户消息高优先级队列
- 后台任务队列
- 最新价格缓存
- 飞书事件去重
- 静默期 key
- 任务锁

必须有 SQLite fallback。

`/status` 必须显示 Redis ok/degraded。

## Parquet 要求

Parquet 必须由项目自行处理。

路径：

```text
data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet
```

要求：

- 只归档已收盘 K 线。
- 写入前合并去重。
- 记录 parquet_archive_runs。
- `/status` 显示最近写入时间。

## DuckDB 要求

DuckDB 数据库文件：

```text
D:/Program Files/duckdb/crypto_guard_analytics.duckdb
```

必须真正接入：

- 查询 Parquet 历史 K 线
- 每小时信号等级分布
- 模拟盘表现摘要
- 每日复盘基础统计
- 策略表现统计

`/status` 必须执行轻量 DuckDB 查询。

## 多周期分析要求

默认日内链路：

- 4H 找方向
- 1H / 15M 找趋势与结构
- 5M / 1M 找入场或反转

必须执行：

- 顺大逆小
- 反向证据检查
- 趋势演化监控
- 未收盘大周期 K 线不能作为确认依据
- 5M 不能单独推翻 4H

## 风控与模拟盘

开仓必须满足：

- RR >= 2
- confidence >= 0.72
- 结构 + 动能共振
- 高周期方向不冲突
- 非极端行情
- trade_plan 完整

每 3 分钟更新最新价格。

市价模拟成交：下一根 K 线开盘价 ±0.1% 滑点。

挂单模拟成交：`low <= entry_price <= high`。

## 自进化

触发条件：

- 连续 3 次模拟盘止损
- 总资金回撤 > 10%

触发后：

1. 执行归因分析。
2. 更新 skill_feedback_memory。
3. 生成 candidate skill/strategy patch。
4. 进入 shadow_testing。
5. 不自动覆盖 active。
6. 用户确认后才能升级。

## 执行顺序

严格按 `IMPLEMENTATION_ORDER.md` 执行。

每完成一步都要运行测试并输出验收结果。

## 输出格式

完成后输出：

1. 架构审计报告。
2. 修改文件列表。
3. 新增模块列表。
4. 被禁止的旧流程清单。
5. GA Master Controller 调用链。
6. Skill 执行示例。
7. GADecision 示例。
8. 飞书按钮生成示例。
9. Redis 接入点。
10. Parquet 写入路径示例。
11. DuckDB 查询示例。
12. `/status` 输出示例。
13. 测试命令和结果。
14. 按 `13_acceptance/ACCEPTANCE_MATRIX.md` 逐条对照验收。

## 当前任务

请先执行 Step 0：仓库审计。

扫描当前代码，找出所有绕过 GA Master Controller 的最终决策路径，并输出审计报告。不要先写大量新功能。审计完成后，再按 `IMPLEMENTATION_ORDER.md` 逐步实施。
