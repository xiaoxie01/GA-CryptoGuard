# Codex 实施顺序

不要一次性大改所有模块。严格按以下顺序推进，每一步都要可运行、可回滚、可验收。

## Step 0：仓库审计

Codex 必须先扫描当前仓库，列出：

- 飞书入口文件
- 定时任务入口
- 当前分析流程入口
- 当前策略/信号生成位置
- 当前模拟盘订单创建位置
- 当前数据库访问层
- 是否已有 Redis / DuckDB / Parquet 代码
- 是否有绕过 GA 的最终决策路径

产出：`ARCHITECTURE_AUDIT_REPORT.md`

## Step 1：GA 主控权回收

实现或重构：

- `plugins/crypto_guard/ga_master/controller.py`
- `context_builder.py`
- `skill_orchestrator.py`
- `decision_schema.py`
- `decision_persistence.py`
- `feishu_action_builder.py`

验收重点：

- 飞书用户分析请求进入 GA Master Controller。
- 定时分析请求进入 GA Master Controller。
- 最终输出只能是 `GADecision`。
- 工具层、策略层、定时任务不能直接创建最终 signal / paper_order / opportunity_watch。

## Step 2：GADecision 与状态持久化

新增：

- `ga_decisions`
- `analysis_states`
- `skill_execution_logs`
- `skill_feedback_memory`

验收重点：

- 每次分析都写入 `ga_decisions` 和 `analysis_states`。
- `paper_order` 只能引用 `ga_decision_id`。
- `opportunity_watch` 只能引用 `ga_decision_id` 且必须由用户按钮确认。

## Step 3：动态 Skill 结构化落地

先落地框架，再逐步完善算法：

1. `price_action`
2. `momentum`
3. `trend_stage`
4. `smc_orderflow`
5. `chanlun`

每个 Skill 必须包含：

- `skill.yaml`
- `prompt.md`
- `tools.py`
- `schema.json`
- `feedback_rules.yaml`

验收重点：

- Tool 只返回事实。
- GA 解释 Tool 结果。
- SkillResult 写入 `skill_execution_logs`。

## Step 4：飞书交互与播报重构

实现：

- 用户主动分析卡片
- 按 signal_grade 生成按钮
- 每小时摘要播报
- 详细分析按需展开
- D/C 级默认不推送详细内容

验收重点：

- D 级不显示“加入机会监控”。
- 无完整 trade_plan 不显示“加入模拟盘”。
- 每小时播报不再逐币展示长篇模块明细。

## Step 5：Redis 真接入

Redis 安装目录：`D:\Program Files\Redis`

实现：

- `RedisAdapter`
- 用户高优先级队列
- 后台任务队列
- 最新价格缓存
- 飞书事件去重
- 静默期 key
- 任务锁

验收重点：

- `/status` 显示 Redis connected 或 degraded。
- Redis 里能看到 queue / lock / latest_price / quiet / dedupe key。
- 用户消息优先于后台任务。

## Step 6：Parquet 自主管理归档

实现：

- `ParquetKlineArchive`
- 月度分区
- 自行创建目录
- closed candle 归档
- 本地文件完整性检查

验收重点：

- 生成 `data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet`
- `/status` 显示最近一次 Parquet 写入时间。

## Step 7：DuckDB 统计查询接入

DuckDB 数据库目录：`D:\Program Files\duckdb`

默认数据库文件：`D:\Program Files\duckdb\crypto_guard_analytics.duckdb`

实现：

- `DuckDBAnalytics`
- 查询 Parquet 历史 K 线
- 每小时信号等级分布
- 模拟盘表现摘要
- 每日复盘基础统计
- 策略表现统计

验收重点：

- DuckDB 能读取 Parquet。
- `/status` 显示 DuckDB query ok。
- 每小时播报统计来自 DuckDB 聚合或明确 fallback。

## Step 8：风控与模拟盘重构

实现：

- 只有 GADecision 允许创建模拟盘订单
- RR >= 2
- confidence >= 0.72
- 结构 + 动能共振
- 非极端行情
- 每 3 分钟最新价格更新
- 市价 / 挂单模拟成交规则

## Step 9：每日复盘与自进化闭环

实现：

- UTC 00:05 每日复盘
- 连续 3 次止损触发自进化
- 总回撤 >10% 触发自进化
- Skill memory 更新
- candidate patch
- shadow_testing
- 不自动覆盖 active

## Step 10：完整验收

执行 `13_acceptance/ACCEPTANCE_MATRIX.md` 的所有验收项。
