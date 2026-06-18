# GA CryptoGuard 架构纠偏与完整验收报告

生成时间：2026-05-26（UTC+8）

## 1. 架构审计结论

- 已完成 Step 0 审计，报告见 `ARCHITECTURE_AUDIT_REPORT.md`。
- 已发现并修正的旧路径：Scheduler/工具层直接产出 signal、按钮直接生成机会监控、旧 signal 直接进入模拟盘、每小时播报直接读取 signals 展开长文。
- 当前最终动作统一通过 `GADecision` 持久化结果承接；旧 signal 兼容路径会先补建 `GADecision`，再创建模拟盘订单或机会监控。

## 2. 修改文件列表

- `plugins/crypto_guard/ga_master/*`
- `plugins/crypto_guard/skills/{price_action,momentum,trend_stage,smc_orderflow,chanlun}/*`
- `plugins/crypto_guard/storage/{schema.sql,migrations.py,repository.py,redis_adapter.py,parquet_archive.py,duckdb_analytics.py}`
- `plugins/crypto_guard/tools/{ga_crypto_tools.py,status_tools.py}`
- `plugins/crypto_guard/run_ga_workers.py`
- `plugins/crypto_guard/notify/{feishu_cards.py,feishu_integration.py,alert_delivery.py,hourly_report.py}`
- `plugins/crypto_guard/risk/risk_engine.py`
- `plugins/crypto_guard/reasoning/analysis_state.py`
- `plugins/crypto_guard/paper/{paper_broker.py,paper_position_updater.py}`
- `plugins/crypto_guard/review/{daily_reviewer.py,evolution_triggers.py}`

## 3. 新增模块列表

- `ga_master/controller.py`
- `ga_master/context_builder.py`
- `ga_master/skill_orchestrator.py`
- `ga_master/decision_schema.py`
- `ga_master/decision_persistence.py`
- `ga_master/feishu_action_builder.py`
- `ga_master/risk_gate.py`
- `ga_master/report_adapter.py`
- `storage/redis_adapter.py`
- `storage/duckdb_analytics.py`
- 动态 Skill：`price_action`、`momentum`、`trend_stage`、`smc_orderflow`、`chanlun`

## 4. 被禁止的旧流程清单

- 工具层直接创建机会监控：已拒绝，必须由 GA decision + 用户按钮确认。
- 旧 signal 直接创建模拟盘订单：已改为先补建 `GADecision`，订单写入 `ga_decision_id`。
- D/C 级显示“加入机会监控”：已禁止。
- 无完整且通过风控的 trade_plan 显示“加入模拟盘”：已禁止。
- 定时任务直接推送逐币长文：已改为小时摘要，详细分析按需展开。

## 5. GA Master Controller 调用链

用户主动分析：

`crypto_analyze_symbol_once -> build_market_state_snapshot -> GAMasterController.analyze_symbol -> ContextBuilder -> run_agent_sop_decision -> RiskGate -> FeishuActionBuilder -> analysis_state -> ga_decisions -> compatibility signal -> Feishu card`

定时分析：

`scheduled_market_analysis job -> GAMasterController.analyze_symbol -> ga_decisions/analysis_states -> hourly_report`

按钮动作：

`Feishu button -> handle_button_callback -> ga_decision_id -> risk recheck -> paper_order/opportunity_watch`

## 6. Skill 执行示例

- Skill 标准名：`price_action`、`momentum`、`trend_stage`、`smc_orderflow`、`chanlun`
- 每个 Skill 均包含 `skill.yaml`、`prompt.md`、`tools.py`、`schema.json`、`feedback_rules.yaml`
- Tool 输出确定性事实；GA 解释写入 `ga_interpretation_json`
- 执行日志表：`skill_execution_logs`

## 7. GADecision 示例字段

`symbol, analysis_time, analysis_time_utc, decision_type, signal_grade, confidence, market_bias, trend_stage, decision, skill_result_refs_json, evidence_json, counter_evidence_json, risk_check_json, trade_plan_json, opportunity_watch_json, feishu_actions_json, final_summary, created_by='ga_master_controller'`

## 8. 飞书按钮生成示例

- D/C：`add_to_watchlist`, `ignore`
- B：`create_opportunity_watch`, `add_to_watchlist`, `ignore`
- A/S 且 trade_plan 完整并风控通过：`create_paper_order`, `create_opportunity_watch`, `add_to_watchlist`, `ignore`
- A/S 但 trade_plan 不完整或风控失败：不显示 `create_paper_order`

## 9. Redis 接入点

- 用户消息队列：`queue:user:feishu`
- 后台任务队列：`queue:ga:background`
- 最新价格：`latest_price:{symbol}`
- 飞书事件去重：`dedupe:feishu_event:{event_id}`
- 静默期：`quiet:{symbol}:{alert_type}`
- 任务锁：`lock:{name}`
- 临时测试库自动 SQLite fallback，避免本机 Redis 状态污染测试。

## 10. Parquet 写入路径示例

已验证路径：

`E:\GenericAgent_crypto\data\parquet\klines\binance_um\VALIDATIONUSDT\5m\2026-05.parquet`

重复归档同一根 K 线后保留 1 行，最新值覆盖旧值；`parquet_archive_runs` 已记录写入状态。

## 11. DuckDB 查询示例

DuckDB 安装路径：

`D:/Program Files/duckdb/duckdb.exe`

DuckDB 数据文件默认路径已按实际运行权限调整为项目可写目录：

`E:\GenericAgent_crypto\data\duckdb\crypto_guard_analytics.duckdb`

也可用 `CRYPTO_GUARD_DUCKDB_PATH` 覆盖。当前环境 Python 未安装 `duckdb` 模块，但已通过 DuckDB CLI fallback 读取 Parquet 并返回 `VALIDATIONUSDT` 5m K 线。

## 12. `/status` 输出示例

- Redis：`ok`
- Parquet：`ok`，最近写入为 `VALIDATIONUSDT/5m/2026-05.parquet`
- DuckDB：默认项目数据路径 `E:\GenericAgent_crypto\data\duckdb\crypto_guard_analytics.duckdb`，状态 `ok`；当前使用 DuckDB CLI fallback。

## 13. 测试命令和结果

- `python -m compileall -q plugins\crypto_guard`：通过
- `python -m unittest plugins.crypto_guard.tests.test_ga_master_acceptance plugins.crypto_guard.tests.test_smoke`：36 tests，OK
- 新增 GA Master acceptance 测试覆盖：飞书按钮规则、`ga_decisions` 持久化、旧 signal 兼容路径补建 `GADecision`、Parquet 合并去重、DuckDB 读取 Parquet、临时库 Redis fallback。
- Parquet/DuckDB 验证脚本：Parquet 写入 OK；DuckDB CLI 在默认项目数据路径读取 OK
- `/status` 验证：Redis OK，Parquet OK，DuckDB OK

## 14. ACCEPTANCE_MATRIX 对照

### A. GA 主控

- 用户分析与定时分析均进入 `GAMasterController`。
- 每次分析写入 `ga_decisions` 与 `analysis_states`。
- 模拟盘订单写入 `ga_decision_id`。
- 机会监控必须由按钮确认创建，并写入 `ga_decision_id` 与 `created_by_user_action=1`。

### B. Skill

- 五大 Skill 合约文件齐全。
- Skill 执行写入 `skill_execution_logs`。
- 每日复盘与自进化写入 `skill_feedback_memory`。

### C. 飞书

- 按等级生成按钮。
- 小时播报改为摘要。
- 静默期、失败 outbox、重试保留。

### D. Redis

- Redis 队列、去重、静默、锁、价格缓存均接入。
- Redis 不可用或临时测试库时 fallback SQLite。

### E. Parquet

- closed candle 月度分区写入。
- 合并去重已验证。
- `/status` 显示最近写入。

### F. DuckDB

- DuckDB CLI fallback 已实现。
- 默认项目数据路径读取 Parquet 已验证。
- `D:\Program Files\duckdb` 仅作为安装目录和 `duckdb.exe` 所在目录，不再作为数据库写入目录。

### G. 分析流程

- 每次分析读取 previous analysis_state。
- 默认日内链路仍为 4H 方向、1H/15M 趋势结构、5M 入场触发。
- 已收盘 K 线策略不变。

### H. 风控与模拟盘

- RR、confidence、结构动能、高周期方向、极端行情、trade_plan 完整性均由 `RiskGate`/`validate_trade_plan` 强制。
- 人工按钮不能绕过风控。
- 市价滑点与挂单 high/low 成交规则保留。

### I. 自进化

- 连续止损/回撤触发 `evolution_triggers`。
- patch 进入 candidate / shadow_testing。
- 不自动覆盖 active。

### J. 安全

- 未引入实盘交易能力。
- 未调用 Binance 下单接口。
- 未保存交易权限或提现权限 API Key。
- 飞书文案保持模拟盘与策略研究边界。
