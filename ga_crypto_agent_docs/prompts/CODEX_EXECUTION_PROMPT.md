# Codex 执行提示词

你是 Codex，需要在现有 GenericAgent 项目中实现 `plugins/crypto_guard`。请先阅读本压缩包全部文档，然后按 Phase 0 → Phase 9 小步实现。

基于https://github.com/lsdefine/GenericAgent项目进行改造。需要拉取项目至"E:\GenericAgent_crypto"目录下。可以先深度理解GenericAgent(简称GA)项目。
"E:\GenericAgent_crypto\ga_crypto_agent_docs"目录下包含了以下内容。
* `README.md`：项目总说明和 Codex 执行原则
* `FULL_SPEC.md`：合并版完整规格文档
* `docs/`：产品需求、系统架构、模块设计、调度队列、SOP、自进化、飞书交互、存储缓存、实施路线、验收标准
* `sql/schema.sql`：SQLite 完整表结构
* `configs/`：交易模式、产品池、定时任务、策略模板配置
* `schemas/`：MarketStateSnapshot、GA Decision、Trade Review 的 JSON Schema
* `prompts/CODEX_EXECUTION_PROMPT.md`：给 Codex 的执行提示词

**按照计划完成，然后对其进行审查以及验收。中途不允许使用Python虚拟环境。**

## 最高优先级约束

1. 不接实盘。
   - 禁止真实交易下单。
   - 禁止引入交易权限 API Key。
   - 所有交易行为只能是 paper trading。

2. 不阻塞飞书用户消息。
   - 用户消息必须走 high priority queue。
   - 定时任务必须走 scheduler/background queue。
   - 用户 session 与后台 session 隔离。

3. 不要大改 GenericAgent 核心。
   - 优先新增 `plugins/crypto_guard`。
   - 只在 Feishu frontend 和 tool registry 做必要接入。

4. 所有时间使用 UTC。
   - K 线分析只使用已收盘数据。
   - 所有分析函数必须接收 `analysis_time_utc`。

5. GA 输出必须是可校验 JSON。
   - 使用 `schemas/ga_decision.schema.json`。
   - 不符合 schema 时必须重试或降级为 no_edge。

## 实施顺序

### Step 1
创建目录结构、配置加载、SQLite 初始化、schema 执行。

### Step 2
实现 symbols 管理工具：add/remove/pause/resume/list。

### Step 3
实现 agent_jobs SQLite queue，支持 priority 和 session_id。

### Step 4
实现 scheduler worker 和 scheduler_runs 幂等。

### Step 5
实现 Binance K 线 fetch 与 candles upsert。

### Step 6
实现 price_action_engine、momentum_engine、trend_stage_engine 初版。

### Step 7
实现 MarketStateSnapshot builder。

### Step 8
实现 GA SOP decision，输出标准 JSON。

### Step 9
实现 Feishu 临时分析卡片与按钮回调。

### Step 10
实现 paper trading 和 review job。

## 编码风格

- Python 类型标注。
- 每个模块有清晰接口。
- Repository 层隔离 SQL。
- 配置使用 YAML。
- JSON 输出用 schema 校验。
- 对外工具函数返回 `{ok: bool, ...}`。
- 异常必须写入 scheduler_runs / agent_jobs error_message。
- 使用中文进行注释。
- 飞书消息尽量使用中文以免用户不理解。

## 工具说明
- duckdb数据库：D:\Program Files\duckdb
- redis：D:\Program Files\Redis
- sqlite、Parquet都需要自行处理。

## 关键验收

- 用户发送消息时，即使后台 daily_review 正在运行，也必须能优先回应。
- 重复点击“加入模拟盘”不会重复创建订单。
- 15m 分析不会使用未收盘 15m K 线。
- 所有模拟盘平仓都会触发复盘。
- 策略补丁永远先进入 candidate，不得直接 active。
