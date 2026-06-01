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
