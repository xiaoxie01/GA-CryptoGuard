---

# FILE: README.md

# GA CryptoGuard Codex 完整实施与验收方案

本包用于指导 Codex 对现有 GA CryptoGuard 项目进行架构纠偏、功能完善与验收。目标是把当前实现纠正为：

> **由 GA 绝对主控、通过飞书交互、面向 Binance 合约市场的自主分析与模拟交易研究系统。**

## 关键原则

1. **GA Master Controller 是唯一最终决策出口。**
2. **任何交易判断、机会监控建议、模拟盘动作、飞书按钮都必须来自 `GADecision`。**
3. **Skill 是 Prompt + Tool + Feedback Memory + Evolution Rule 的动态闭环，不是单纯 Python 函数。**
4. **Tool 只计算客观事实，不直接产生最终交易决策。**
5. **飞书是交互层和预警出口，不是内部分析状态的全文展示窗口。**
6. **SQLite 保存业务状态；Redis 承担队列/缓存/锁/静默期；Parquet 自主管理长期 K 线归档；DuckDB 查询 Parquet 和生成统计。**
7. **不接实盘，不调用真实下单接口，不保存交易权限或提现权限 API Key。**

## 用户指定 Windows 路径

- DuckDB 数据库目录：`D:\Program Files\duckdb`
- Redis 安装目录：`D:\Program Files\Redis`
- Parquet：由项目自行处理，默认放在项目目录下 `data/parquet/klines/binance_um/`

## 建议阅读顺序

1. `CODEX_MASTER_PROMPT.md`：直接复制给 Codex 的总提示词。
2. `IMPLEMENTATION_ORDER.md`：严格实施顺序。
3. `01_architecture/GA_MASTER_CONTROL.md`：GA 主控架构。
4. `06_skills/SKILL_CONTRACT.md`：动态 Skill 规范。
5. `03_storage/STORAGE_REDIS_DUCKDB_PARQUET.md`：Redis / Parquet / DuckDB 接入规范。
6. `13_acceptance/ACCEPTANCE_MATRIX.md`：最终验收矩阵。

## 输出要求

Codex 每完成一个实施阶段，必须输出：

- 修改文件列表
- 新增/修改配置
- 数据库迁移说明
- Redis 接入点
- Parquet 写入示例
- DuckDB 查询示例
- 飞书播报新旧对比
- 临时分析按钮规则测试结果
- `/status` 输出示例
- 验收标准逐条对照


---

# FILE: IMPLEMENTATION_ORDER.md

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


---

# FILE: 01_architecture/GA_MASTER_CONTROL.md

# GA Master Control 架构规范

## 1. 正确的数据流

```text
Feishu / Scheduler
    ↓
GA Master Controller
    ↓
Context Builder
    - previous analysis_state
    - active opportunity_watch
    - open paper_position
    - symbol profile
    - skill_feedback_memory
    - Redis latest price
    ↓
Skill Orchestrator
    - chanlun
    - price_action
    - smc_orderflow
    - momentum
    - trend_stage
    ↓
SkillResult[]
    ↓
GA Multi-Timeframe Reasoning
    ↓
Risk Check
    ↓
GADecision
    ↓
Persistence / Feishu Actions / Paper Trading / Opportunity Watch
```

## 2. 绝对禁止的旧流程

Codex 必须搜索并消除以下模式：

```text
scheduler -> strategy_engine -> final signal -> feishu
analysis_worker -> LLM summary -> paper_order
tool -> direct signal
strategy_evaluator -> direct paper_order
D grade -> auto opportunity_watch
user message -> direct paper_order without GA risk check
```

这些都必须改成：

```text
任何最终动作都必须由 GADecision 驱动。
```

## 3. GA 负责什么

GA Master Controller 负责：

1. 识别用户意图。
2. 决定是否进行临时分析、定时分析、复盘、模拟盘动作。
3. 读取上下文和历史状态。
4. 决定调用哪些 Skill。
5. 解释 SkillResult。
6. 执行多周期共振。
7. 执行反向证据检查。
8. 执行风险门控。
9. 输出唯一 `GADecision`。
10. 生成飞书按钮。
11. 触发模拟盘或机会监控后续流程。
12. 触发复盘和自进化。

## 4. Tool / Worker 负责什么

Tool 和 Worker 只负责执行 GA 授权的任务：

- 拉取行情
- 计算指标或结构事实
- 读写数据库
- 写 Redis 缓存
- 写 Parquet
- DuckDB 查询统计
- 推送 GA 生成的飞书消息
- 执行已批准的模拟盘动作

## 5. GADecision 是唯一最终出口

强制规则：

1. `paper_orders.ga_decision_id` 必须存在。
2. `opportunity_watches.ga_decision_id` 必须存在，且 `created_by_user_action = 1`。
3. 每小时播报只能引用 `ga_decisions` 的摘要，不直接读取 Skill 长文本。
4. 风控结果必须写入 `ga_decisions.risk_check_json`。
5. 飞书按钮必须来自 `ga_decisions.feishu_actions_json`。


---

# FILE: 03_storage/STORAGE_REDIS_DUCKDB_PARQUET.md

# Redis / Parquet / DuckDB 接入规范

## 1. 存储分工

| 组件 | 职责 |
|---|---|
| SQLite | 业务状态、GA 决策、模拟盘、复盘、策略版本、审计 |
| Redis | 队列、缓存、任务锁、飞书去重、静默期、最新价格 |
| Parquet | 长期 K 线归档，由项目自行管理 |
| DuckDB | 查询 Parquet、报表聚合、回测统计、策略表现分析 |

## 2. Redis key 规范

```text
queue:user:feishu
queue:ga:background
queue:market:data
latest_price:{symbol}
mark_price:{symbol}
lock:job:{job_name}
quiet:{symbol}:{alert_type}
dedupe:feishu_event:{event_id}
health:redis:last_ping
```

## 3. Redis Adapter 接口

Codex 应实现：

```python
class RedisAdapter:
    def is_available(self) -> bool: ...
    def enqueue_user_job(self, payload: dict) -> str: ...
    def enqueue_background_job(self, payload: dict) -> str: ...
    def pop_user_job(self) -> dict | None: ...
    def pop_background_job(self) -> dict | None: ...
    def set_latest_price(self, symbol: str, price: float, ttl_seconds: int = 600): ...
    def get_latest_price(self, symbol: str) -> float | None: ...
    def acquire_lock(self, name: str, ttl_seconds: int) -> bool: ...
    def release_lock(self, name: str): ...
    def is_quiet(self, symbol: str, alert_type: str) -> bool: ...
    def set_quiet(self, symbol: str, alert_type: str, ttl_seconds: int): ...
    def dedupe_event(self, event_id: str, ttl_seconds: int = 3600) -> bool: ...
```

如果 Redis 不可用，必须 fallback SQLite，但 `/status` 要显示 degraded。

## 4. Parquet K 线归档

路径规范：

```text
data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet
```

字段规范：

```text
exchange
market_type
symbol
interval
open_time
open_time_utc
open
high
low
close
volume
close_time
close_time_utc
quote_volume
trade_count
taker_buy_base_volume
taker_buy_quote_volume
ingested_at_utc
```

归档规则：

1. 只归档已收盘 K 线。
2. 每批写入前按 `symbol + interval + open_time` 去重。
3. 若目标文件已存在，读取旧文件，合并去重后重写。
4. 写入完成后记录 `parquet_archive_runs`。
5. 失败不影响 SQLite 热数据，但必须记录错误。

## 5. DuckDB Analytics

DuckDB 数据库路径：

```text
D:/Program Files/duckdb/crypto_guard_analytics.duckdb
```

Codex 应实现：

```python
class DuckDBAnalytics:
    def health_check(self) -> dict: ...
    def query_klines(self, symbol: str, interval: str, start: str, end: str): ...
    def hourly_signal_distribution(self, start: str, end: str): ...
    def paper_account_summary(self, date_utc: str): ...
    def daily_review_stats(self, date_utc: str): ...
    def strategy_performance(self, strategy_name: str, days: int = 30): ...
```

DuckDB 查询 Parquet 示例：

```sql
SELECT symbol, interval, COUNT(*) AS n, MIN(open_time_utc), MAX(close_time_utc)
FROM read_parquet('data/parquet/klines/binance_um/BTCUSDT/15m/*.parquet')
GROUP BY symbol, interval;
```

## 6. /status 必须显示

```json
{
  "redis": {"status": "ok", "url": "redis://localhost:6379/0"},
  "sqlite": {"status": "ok"},
  "parquet": {"status": "ok", "last_write": "2026-05-26T06:00:00Z"},
  "duckdb": {"status": "ok", "database": "D:/Program Files/duckdb/crypto_guard_analytics.duckdb"}
}
```


---

# FILE: 06_skills/SKILL_CONTRACT.md

# GA 动态 Skill 合同

## 1. Skill 不是单纯代码

每个 Skill 都必须是：

```text
Prompt SOP + Deterministic Tools + Output Schema + Feedback Memory + Evolution Rule
```

## 2. Skill 标准目录

```text
plugins/crypto_guard/skills/{skill_name}/
  skill.yaml
  prompt.md
  tools.py
  schema.json
  feedback_rules.yaml
```

## 3. Skill 执行顺序

```text
GA Master Controller
  ↓
SkillOrchestrator loads skill.yaml
  ↓
读取 skill memory
  ↓
调用 tools.py 计算客观事实
  ↓
GA 根据 prompt.md 解释事实
  ↓
输出 SkillResult
  ↓
schema 校验
  ↓
写 skill_execution_logs
  ↓
返回 GA Master Controller
```

## 4. 工具层禁止事项

`tools.py` 禁止输出：

- `signal_grade`
- `create_paper_order`
- `final_decision`
- `feishu_buttons`
- `opportunity_watch_recommended`

工具层只输出事实，例如：

- swing points
- BOS / CHoCH candidates
- FVG ranges
- order blocks
- CVD slope
- RSI slope
- Zhongshu ranges
- divergence metrics

## 5. GA Skill 可以判断

GA Skill 可以基于工具事实判断：

- 结构含义
- 信号可靠性
- 风险
- 是否需要等待确认
- 是否 degraded
- confidence

但最终交易结论仍然只能由 GA Master Controller 输出。

## 6. Skill 自进化

每日复盘和交易复盘可以写入 `skill_feedback_memory`。

Skill patch 可以修改：

- prompt
- tool 参数
- confidence 规则
- 过滤条件

但必须：

```text
candidate -> shadow_testing -> 用户确认 -> active
```

不能自动覆盖 active。


---

# FILE: 13_acceptance/ACCEPTANCE_MATRIX.md

# 最终验收矩阵

## A. GA 主控验收

- [ ] 飞书用户分析请求进入 GA Master Controller。
- [ ] 定时分析请求进入 GA Master Controller。
- [ ] 每次最终分析写入 `ga_decisions`。
- [ ] `paper_order` 只能由 `ga_decision_id` 创建。
- [ ] `opportunity_watch` 只能由 `ga_decision_id` + 用户按钮确认创建。
- [ ] 工具层不直接输出最终交易决策。
- [ ] 策略层不绕过 GA 创建交易动作。

## B. Skill 验收

- [ ] 五大 Skill 均有 `skill.yaml`。
- [ ] 五大 Skill 均有 `prompt.md`。
- [ ] 五大 Skill 均有 `tools.py`。
- [ ] 五大 Skill 均有 `schema.json`。
- [ ] 五大 Skill 均有 `feedback_rules.yaml`。
- [ ] Skill 执行写入 `skill_execution_logs`。
- [ ] Tool 只输出事实，不输出交易动作。
- [ ] 每日复盘可写入 `skill_feedback_memory`。

## C. 飞书交互验收

- [ ] D/C 级只显示 `[加入长期产品池] [忽略]`。
- [ ] B 级显示 `[加入机会监控] [加入长期产品池] [忽略]`。
- [ ] A/S 且 trade_plan 完整并风控通过，显示 `[加入模拟盘]`。
- [ ] 无 trade_plan 不显示 `[加入模拟盘]`。
- [ ] D 级不建议机会监控。
- [ ] 用户未点击按钮不会自动加入观察列表。
- [ ] 每小时播报为摘要，不展示逐币长篇模块明细。
- [ ] `详细分析 XXX` 才展开模块明细。

## D. Redis 验收

- [ ] `/status` 显示 Redis 状态。
- [ ] Redis 可见 `queue:user:feishu` 或对应队列 key。
- [ ] Redis 可见 `latest_price:{symbol}`。
- [ ] Redis 可见 `lock:job:*`。
- [ ] Redis 可见 `quiet:{symbol}:{alert_type}`。
- [ ] Redis 可见 `dedupe:feishu_event:*`。
- [ ] Redis 断开时系统 fallback SQLite，并显示 degraded。

## E. Parquet 验收

- [ ] closed candle 写入后生成 Parquet 文件。
- [ ] 文件路径符合 `data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet`。
- [ ] 重复归档不会产生重复 K 线。
- [ ] `parquet_archive_runs` 有记录。
- [ ] `/status` 显示最近 Parquet 写入时间。

## F. DuckDB 验收

- [ ] DuckDB 数据库位于 `D:/Program Files/duckdb/crypto_guard_analytics.duckdb`。
- [ ] `/status` 执行轻量 DuckDB 查询并返回 ok/degraded。
- [ ] DuckDB 可以读取 Parquet。
- [ ] 每小时播报中的等级分布来自 DuckDB 聚合或明确 fallback。
- [ ] 每日复盘基础统计可由 DuckDB 查询。

## G. 分析流程验收

- [ ] 每次分析写入 `analysis_states`。
- [ ] 下一次分析读取 previous analysis_state。
- [ ] 4H 找方向，1H/15M 找趋势结构，5M/1M 找入场。
- [ ] 5M 不能单独推翻 4H。
- [ ] 未收盘大周期 K 线不能作为确认依据。
- [ ] next_analysis_time 对齐 K 线收盘后。

## H. 风控与模拟盘验收

- [ ] RR < 2 拒绝模拟盘。
- [ ] confidence < 0.72 拒绝模拟盘。
- [ ] 手动飞书开仓不能绕过风控。
- [ ] 每 3 分钟更新最新价格。
- [ ] 市价单使用下一根 K 线开盘价 ±0.1% 滑点。
- [ ] 挂单使用 high/low 区间成交判断。
- [ ] 开仓/平仓/止损/止盈/调整止损必须推送飞书。

## I. 自进化验收

- [ ] 连续 3 次止损创建 `evolution_triggers`。
- [ ] 总回撤 >10% 创建 `evolution_triggers`。
- [ ] 极端行情暂停自动生成策略补丁。
- [ ] patch 进入 candidate / shadow_testing。
- [ ] 不自动覆盖 active。
- [ ] 用户确认后才能升级。

## J. 安全验收

- [ ] 不接实盘。
- [ ] 不调用 Binance 下单接口。
- [ ] 不保存交易权限或提现权限 API Key。
- [ ] 飞书文案不出现“稳赚”“必涨”“确定盈利”等承诺。


---

# FILE: CODEX_MASTER_PROMPT.md

# Codex 总提示词：GA CryptoGuard 架构纠偏与完整实现

你是本项目的资深后端工程师、量化系统架构师和代码审查员。请基于当前仓库和本方案文档，对 GA CryptoGuard 进行架构纠偏、功能完善和验收。

## 文档包位置

请先阅读本目录下所有文件，重点阅读：

1. `README.md`
2. `IMPLEMENTATION_ORDER.md`
3. `01_architecture/GA_MASTER_CONTROL.md`
4. `01_architecture/ANTI_PATTERNS_AND_REFACTOR.md`
5. `03_storage/STORAGE_REDIS_DUCKDB_PARQUET.md`
6. `06_skills/SKILL_CONTRACT.md`
7. `13_acceptance/ACCEPTANCE_MATRIX.md`

## 用户指定环境

- DuckDB 数据库目录：`D:\Program Files\duckdb`
- DuckDB 数据库文件：`D:\Program Files\duckdb\crypto_guard_analytics.duckdb`
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
