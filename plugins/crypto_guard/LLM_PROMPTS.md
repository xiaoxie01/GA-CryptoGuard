# CryptoGuard LLM 提示词目录

本文档整理 CryptoGuard 生产代码中与 LLM 直接交互的提示词、动态上下文、输出契约和调用位置。内容以当前源码为准；提示词的唯一运行时事实来源仍是对应 Python 构造器和 JSON Schema。

## 1. 交互链路总览

项目有两类 LLM 交互：

1. **市场决策专用链路**：每个产品的多周期行情快照、确定性 SOP 结论和上一轮分析连续性进入 LLM，输出 `GADecision`。
2. **通用 JSON 任务链路**：回测、复盘、日报、机会监控和策略管理等任务通过 `run_agent_json_task` 复用一个通用提示词模板。

所有生产 LLM 调用最终集中到：

- 构造与编排：`reasoning/llm_agent_judge.py`
- Provider 调用：`reasoning/llm_agent_judge.py::_call_ga_llm`
- 市场决策 Schema：`schemas/ga_decision.schema.json`
- 单笔复盘 Schema：`schemas/trade_review.schema.json`
- 上一轮分析上下文：`reasoning/decision_context.py::build_analysis_continuity`
- 公平调度：`reasoning/llm_fair_scheduler.py`
- 运行参数：`config/trading_mode.yaml::llm`

生产代码中没有第二套直接 `llmcore` 调用；会话解析和 `raw_ask` 均由 `_call_ga_llm` 统一处理。

## 2. 公共系统提示词

### 2.1 正常模式

源码：`reasoning/llm_agent_judge.py::SYSTEM_PROMPT`

```text
你是 GA CryptoGuard 的市场研究 Agent。
你必须基于结构化模块证据做多周期 SOP 研判，而不是凭空预测。
边界：禁止实盘交易建议，禁止真实下单，只允许输出模拟盘/机会监控/观察/忽略相关决策。
只输出一个符合 GADecision schema 的 JSON 对象，不要 Markdown，不要额外解释。
```

### 2.2 严格 JSON 模式

源码：`reasoning/llm_agent_judge.py::SYSTEM_PROMPT_STRICT_JSON`

```text
你是 GA CryptoGuard 的市场研究 Agent。
只输出一个符合 GADecision schema 的 JSON 对象。
禁止 Markdown。禁止代码块。禁止自然语言解释。禁止前导文字。
第一个字符必须是 {。最后一个字符必须是 }。
```

严格模式用于市场决策的后续重试。它复用正常提示词的 JSON payload，只替换系统前缀。

## 3. 市场决策提示词

入口：`run_agent_sop_decision`

构造器：`build_llm_decision_prompt`

输出：`GADecision` JSON object

### 3.1 动态模板

实际发送内容为：

```text
{SYSTEM_PROMPT}

输入：
{
  "schema_contract": {...},
  "task": "按 SOP_MULTI_TIMEFRAME_MARKET_ANALYSIS 输出最终 GADecision JSON。",
  "sop": [...],
  "hard_rules": [...],
  "market_snapshot": {...},
  "pre_score": {...},
  "deterministic_reference": {...},
  "output_requirements": {...},
  "historical_memory": {...},
  "open_positions": [...],
  "active_watches": [...]
}
```

`historical_memory`、`open_positions` 和 `active_watches` 仅在有数据时加入。

### 3.2 SOP 原文

```text
检查数据完整性和未来函数风险
判断 4H 已收盘方向过滤器
判断 1H/15M 已收盘趋势与结构
检查 5M 入场、反转和触发机会
主动寻找反向证据
匹配策略评分和动作决策
解释为什么有机会或为什么没有机会
```

### 3.3 硬规则原文

风险阈值中的 `{min_rr}` 和 `{min_conf}` 来自 `trading_mode.yaml::risk`。

```text
不得输出实盘交易或真实下单能力
LLM 不负责几何计算；Swing/FVG/OB/中枢/指标数值必须以 deterministic_preprocessing 输出为准
5M 只能触发入场，不能单独推翻 4H 方向；未收盘 4H/1H/15M 不得作为确认依据
当 signal_grade 为 S 或 A 时，必须生成 trade_plan（包含 side/entry_price/stop_loss/take_profits/invalid_condition）
trade_plan 的止损必须基于结构失效位（swing low/high、FVG 边界、order block 边界）
创建模拟盘必须经过风控：RR>={min_rr}、confidence>={min_conf}、高周期方向支持、非极端行情
B 级可输出 opportunity_watch 但不强制 trade_plan
C/D 级不得 create_paper_order，decision 应为 monitor_only 或 no_edge
反向证据存在不等于不能交易；只要 RR>={min_rr} 且止损明确，A/S 级仍应给出 trade_plan
counter_evidence 至少 1 条
entry_trigger_confirmation 必须是结构化对象（type/timeframe/event_type/direction/candle_close_time/price/source/symbol），不得使用裸字符串
entry_trigger_confirmation 必须与 schema 完全匹配，字段不可省略；无法提供时设为 null
entry_trigger_confirmation.symbol 必须等于顶层 decision.symbol — 禁止跨 symbol 匹配
entry_trigger_confirmation.type 必须恒等于 "closed_candle_confirmation"；禁止使用 price_rejection / pullback_rejection / breakout_retest / reclaim_confirmation 等别名
若无法提供完整 closed-candle 确认对象，请将 entry_trigger_confirmation 设为 null，不要发明 type 值
触发风格（price_rejection/pullback/breakout_retest/reclaim）请写入 event_type、reason、evidence 或 risk_notes，不要写入 type
suggested_actions 必须是扁平字符串数组，仅取以下 5 个值之一或多个：create_paper_order、create_opportunity_watch、add_to_watchlist、ignore、monitor_only。合法示例：["monitor_only"]、["create_paper_order"]。非法示例：["monitor_only","wait_for_breakout","avoid_chop"]（wait_for_breakout/avoid_chop 属于 decision 字段，不得放入 suggested_actions）
```

### 3.4 输入上下文

`market_snapshot` 是经过压缩和预算控制的结构化数据，不发送原始 K 线数组。主要字段如下：

| 字段 | 内容 |
|---|---|
| `symbol`, `analysis_time_utc`, `mode` | 产品、分析时间、模式 |
| `profiles` | 多周期画像 |
| `modules` | 主周期 price action、momentum、trend stage、SMC、order flow、缠论 |
| `multi_timeframe_feature_pack` | 1d/4h/1h/15m/5m 的紧凑结构、动能、关键位、健康状态等，预算 24 KiB |
| `analysis_continuity` | 上一轮分析及当前变化，预算 12 KiB |
| `counter_evidence` | 反向证据 |
| `data_quality` | 数据完整性和健康状态 |
| `global_context` | 全局市场上下文 |

`deterministic_reference` 是确定性 SOP 的参考结论。LLM 负责解释和结构化研判，几何计算、门禁和最终风险约束仍由确定性代码负责。

### 3.5 上一轮分析连续性

来源：`decision_context.py::build_analysis_continuity`。

控制器只选择同产品、严格早于当前分析时间、不同批次且不超过连续性最大年龄的上一条状态。结构如下：

```json
{
  "contract_version": "analysis_continuity_v1",
  "schema_version": 1,
  "continuity_status": "ok|missing|stale|future|same_batch|cross_symbol",
  "previous": {
    "analysis_state_id": 123,
    "analysis_time": 0,
    "grade": "B",
    "confidence": 0.0,
    "bias": "bullish|bearish|neutral|mixed|unknown",
    "stage": "early|middle|late|range|transition|unknown",
    "timeframe_summary": {},
    "key_levels": {},
    "plan_status": "executable|withheld|unknown",
    "reason_codes": [],
    "next_triggers": [],
    "trigger_price": null,
    "side": "LONG|SHORT|"
  },
  "delta": {
    "elapsed_bars": null,
    "grade_change": null,
    "bias_change": null,
    "stage_change": null,
    "timeframe_changes": {},
    "new_reason_codes": [],
    "cleared_reason_codes": [],
    "trigger_progress": [],
    "thesis_status": "confirmed|invalidated|unchanged|unknown"
  }
}
```

连续性在提示词裁剪链中属于**受保护字段**。正常提示、严格 JSON 重试、最小安全重试都会携带它。若强制保留连续性后仍无法满足提示词预算，调用会故障关闭，而不是静默删除上一轮上下文。

### 3.6 输出契约摘要

完整契约见 `schemas/ga_decision.schema.json`。关键要求：

- 必填：`symbol`、`analysis_time_utc`、`decision`、`signal_grade`、`confidence`、`summary`、`counter_evidence`、`has_trade_plan`、`suggested_actions`、`risk_notes`、`timeframe_context`、`alignment`、`htf_conflict`、`market_reason_codes`。
- `decision`：`trade_plan_available`、`wait_for_pullback`、`wait_for_breakout`、`wait_for_reclaim`、`avoid_chop`、`no_edge`、`monitor_only`。
- `signal_grade`：`S|A|B|C|D`。
- `market_bias`：`bullish|bearish|neutral|mixed|unknown`。
- `trend_stage`：`early|middle|late|range|transition|unknown`。
- `confidence`：`0..1`。
- `counter_evidence` 至少一项。
- `suggested_actions` 只能使用五个动作枚举，不能混入 `decision` 枚举。
- `timeframe_context` 固定包含 `1d/4h/1h/15m`，每项必须给出 bias、structure、closed、close_time。
- `trade_plan.entry_trigger_confirmation.type` 只能是 `closed_candle_confirmation`，且 symbol 必须与顶层一致。

### 3.7 重试提示词

市场决策最多有三种提示词层级：

| 尝试 | 构造器 | 内容 |
|---|---|---|
| 1 | `build_llm_decision_prompt` | 完整正常提示 |
| 2 | `build_llm_strict_json_prompt` | 同一 payload，改用严格 JSON 系统提示 |
| 3 | `build_llm_minimal_safe_prompt` | 仅保留身份、风险规则、连续性、确定性参考和输出要求 |

最小安全提示的动态 payload 为：

```json
{
  "symbol": "...",
  "analysis_time_utc": 0,
  "strategy_name": "...",
  "strategy_version": "...",
  "hard_rules": [
    "不得输出实盘交易或真实下单能力",
    "创建模拟盘必须经过风控：RR>={min_rr}、confidence>={min_conf}",
    "只输出一个 JSON 对象，禁止 Markdown"
  ],
  "analysis_continuity": {},
  "deterministic_reference": {},
  "output_requirements": {
    "format": "JSON object only",
    "language": "Chinese for summary/evidence/risk_notes",
    "must_keep": ["symbol", "analysis_time_utc", "strategy_name", "strategy_version"]
  },
  "_trim_note": "prompt_over_budget_minimal_fallback"
}
```

注意：Schema/语义校验失败属于非重试类别；传输、限流、空响应、JSON 解析失败、无文本工具调用和输出截断可进入重试。输出格式错误不会打开基础设施熔断器。

## 4. 通用 JSON 任务提示词

入口：`run_agent_json_task`

构造器：`build_agent_json_task_prompt`

### 4.1 公共动态模板

```text
{SYSTEM_PROMPT}

{
  "task_name": "...",
  "task": "基于结构化证据执行 GA/LLM SOP 任务，并只输出一个 JSON 对象。",
  "instructions": [...],
  "hard_rules": [
    "禁止实盘交易、真实下单、保存交易或提现权限 API Key",
    "策略变更只能进入 candidate/shadow/review 流程，不得直接 active，除非输入明确允许且门禁通过",
    "必须说明证据、反证和下一步动作",
    "如果证据不足，输出保守结论并说明缺口"
  ],
  "payload": {...},
  "deterministic_fallback": {...},
  "output_requirements": {
    "format": "JSON object only",
    "language": "Chinese for human-facing text",
    "preserve_required_ids": true
  }
}
```

调用失败时直接返回确定性 fallback，并写入 `agent_source=deterministic_fallback`、`llm_status=failed`。该通用链路当前没有市场决策链路的三层公平重试。

### 4.2 任务清单

#### `historical_replay_backtest_analysis`

- 位置：`backtest/historical_replay.py`
- 输入：产品、周期、起止时间、统计、策略对比、最多 80 条信号和交易、no-lookahead 结果。
- 输出参考：`summary`、`regime_findings`、`strategy_findings`、`recommended_next_steps`。

```text
分析历史回放/回测结果，指出行情状态、策略版本表现、过拟合风险和下一步 shadow/candidate 建议。
必须检查 no_lookahead 是否通过。
不要输出实盘交易建议。
```

#### `trade_review_attribution`

- 位置：`review/trade_reviewer.py`
- 输入：交易、确定性复盘、快照上下文。
- 输出：`trade_review.schema.json`；这是通用任务中唯一显式传入独立 Schema 的调用。

```text
复盘昨日或单笔模拟盘交易，判断亏损/盈利是否来自方向、入场、趋势阶段、反向证据、执行质量或止盈止损设计。
可以修正 primary_reason、summary、improvement_suggestion 和 candidate patch，但 patch 只能进入 candidate。
不要输出实盘建议。
```

#### `paper_execution_quality_update`

- 位置：`paper/paper_position_updater.py`
- 触发：存在模拟盘执行事件或回撤预警时。
- 输入：执行事件、权益快照。
- 输出参考：`summary`、`quality_findings`、`risk_actions`。

```text
总结模拟盘成交、止盈止损、MFE/MAE、回撤和执行质量。
只允许模拟盘/复盘建议，不得输出实盘下单建议。
```

#### `daily_paper_review_summary`

- 位置：`review/daily_reviewer.py`
- 输入：UTC 窗口、最多 50 笔交易与新复盘、错误、策略记忆、自进化状态、确定性模拟盘摘要。
- 输出参考：`summary_text`、`key_findings`、`strategy_actions`、`risk_focus`。

```text
总结昨日 UTC 模拟盘表现、亏损原因、策略表现和下一步 candidate/shadow 事项。
输出 summary_text 字段，适合直接推送飞书。
不要建议实盘交易。
交易概览必须使用以下确定性数据：净 PnL={daily_pnl} USDT，胜={wins}，负={losses}，平仓={trades}，avg_r={avg_r}。
```

#### `hourly_alert_quality_brief`

- 位置：`notify/hourly_report.py`
- 输入：启用产品、最多 30 条紧凑信号、最多 20 个订单、过滤后的当前失败任务、队列统计。
- 输出参考：`summary`、`focus_symbols`、`why_no_opportunity`、`next_checks`。

```text
总结本小时各产品趋势状态、为什么有/没有机会、下一小时应重点观察什么。
不要输出实盘建议。
summary 字段应适合放在飞书简报顶部。
```

#### `higher_timeframe_kline_summary`

- 位置：`scheduler/cron_scheduler.py`
- 触发：1d、4h、1h K 线更新后。
- 输入：产品、周期、分析时间、最近 40 根已收盘 K 线。
- 输出参考：`summary`、`trend_context`、`key_levels`、`risk_notes`。

```text
总结高周期 K 线背景，提取趋势状态、关键区域和风险，供低周期巡航复用。
只基于已收盘 K 线，不得使用未来函数，不得输出实盘建议。
```

#### `opportunity_watch_review`

- 位置：`scheduler/opportunity_watcher.py`
- 输入：机会监控记录、确定性规则结果。
- 输出参考：`summary`、`status`、`action`、`risk_notes`。

```text
复核机会监控条件是否真的值得提醒，解释触发/失效/继续等待原因。
只能输出观察、提醒、失效等模拟盘研究建议，不得输出实盘建议。
```

#### `self_evolution_candidate_patch`

- 位置：`strategy/self_evolution.py`
- 输入：策略名、复盘聚合、最多 50 条近期复盘、样本门禁。
- 输出参考：`patch`、`rationale`、`needs_patch`。
- 后置门禁：patch Schema、candidate 数量、配置许可、回测和 shadow 流程仍由确定性代码控制。

```text
基于复盘聚合提出策略 candidate patch。
必须避免单品种过拟合；只能输出 candidate patch，不能直接 active。
patch 字段为空表示当前不应生成补丁。
```

#### `shadow_test_strategy_verdict`

- 位置：`strategy/shadow_testing.py`
- 输入：active/candidate 版本、样本数、回测状态和两侧统计。
- 用途：LLM 只补充 `llm_explanation` 与 `llm_notes`；最终 verdict 由确定性硬门禁决定。

```text
复核影子测试结果，判断候选策略是否样本不足、拒绝、或可进入人工确认升级。
必须保守处理过拟合风险；不能绕过人工确认或配置门禁。
```

#### `strategy_version_management_summary`

- 位置：`strategy/version_manager.py`
- 输入：策略名、版本列表。
- 输出参考：`summary`、`risks`、`next_actions`。

```text
总结 active/candidate/deprecated 策略版本状态、风险和下一步 shadow/review 动作。
不得建议直接绕过 candidate/shadow 流程。
```

#### `candidate_strategy_config_review`

- 位置：`strategy/version_manager.py`
- 输入：策略 patch、生成后的 candidate 配置。
- 输出参考：`summary`、`config_notes`、`risk_controls`。

```text
复核候选策略配置是否保守、是否需要补充风控说明。
只能补充说明字段，不能将 candidate 改为 active。
```

## 5. Provider 会话约束

`_call_ga_llm` 在每次调用时统一执行以下设置：

- `session.system = SYSTEM_PROMPT`。
- 清空 `session.tools`，并设置 `tools_optional=True`；JSON 任务不提供工具，避免模型转向 tool call 后不输出文本。
- 生产配置默认 `max_output_tokens=8192`、`thinking_budget_tokens=2048`、`min_structured_answer_tokens=4096`、`temperature=0.2`。
- 公平调度默认最多 4 并发；每产品总截止 300 秒，每次 provider 调用最多 180 秒。
- 公平路径把 provider 内部重试设为 0，由外层调度器统一管理重试、公平性和错误分类。
- 可使用子进程隔离实现硬超时，超时后可终止 provider 子进程。
- 提示词硬上限 48 KiB，目标 32 KiB；裁剪顺序优先移除历史记忆、持仓/监控和详细模块，不静默删除连续性。

需要注意：完整 prompt 文本已经包含 `SYSTEM_PROMPT`，同时 `session.system` 也设置为 `SYSTEM_PROMPT`，因此 provider 看到的系统约束和用户消息前缀存在重复。这是当前实现，不是本文档另行推荐的结构。

## 6. 维护注意事项

1. **通用任务的系统提示词存在语义漂移。** `run_agent_json_task` 的 docstring 明确是“non-market-decision JSON task”，但它仍复用要求输出 `GADecision schema` 的 `SYSTEM_PROMPT`。回测、日报、策略版本摘要等任务的 fallback 字段并不属于 `GADecision`。这可能造成模型遵循系统提示而忽略任务自己的输出形状。若后续调整，建议拆出独立的 `GENERIC_JSON_TASK_SYSTEM_PROMPT`，并用行为测试锁定两条链路。
2. **大多数通用任务没有独立 JSON Schema。** 目前只有 `trade_review_attribution` 显式使用 `trade_review.schema.json`；其余任务依赖 fallback 合并和调用方读取字段。新增任务时应优先提供独立 Schema 或至少做显式字段校验。
3. **不要把确定性计算转回 LLM。** 价格几何、风控、评级门禁、shadow 样本门禁、active 切换和真实执行权限均必须继续由代码控制。
4. **连续性是生产契约。** 修改提示词裁剪、最小重试或上下文构造时，必须验证真实上一轮记录仍进入 `analysis_continuity.previous`，不能只验证字段名存在。
5. **修改枚举时同步四处。** 至少同步提示词 `schema_contract`、硬规则、JSON Schema 和 normalize/repair 逻辑，并补 revert-fail 测试。
6. **禁止把密钥、DSN、Header 或完整 provider 错误写进提示词/日志。** 当前错误文本按 300 字符截断；整理和调试时也应保持这一边界。

## 7. 快速源码索引

| 目的 | 文件 / 符号 |
|---|---|
| 系统提示词 | `reasoning/llm_agent_judge.py::SYSTEM_PROMPT` |
| 严格 JSON 提示词 | `reasoning/llm_agent_judge.py::SYSTEM_PROMPT_STRICT_JSON` |
| 市场决策提示构造 | `reasoning/llm_agent_judge.py::build_llm_decision_prompt` |
| 严格重试构造 | `reasoning/llm_agent_judge.py::build_llm_strict_json_prompt` |
| 最小安全重试 | `reasoning/llm_agent_judge.py::build_llm_minimal_safe_prompt` |
| 通用 JSON 模板 | `reasoning/llm_agent_judge.py::build_agent_json_task_prompt` |
| 通用 JSON 执行 | `reasoning/llm_agent_judge.py::run_agent_json_task` |
| Provider 会话 | `reasoning/llm_agent_judge.py::_call_ga_llm` |
| 上一轮连续性 | `reasoning/decision_context.py::build_analysis_continuity` |
| 多周期特征包 | `reasoning/decision_context.py::build_multi_timeframe_feature_pack` |
| 公平调度 | `reasoning/llm_fair_scheduler.py` |
| 市场决策 Schema | `schemas/ga_decision.schema.json` |
| 复盘 Schema | `schemas/trade_review.schema.json` |
| LLM 配置 | `config/trading_mode.yaml::llm` |
