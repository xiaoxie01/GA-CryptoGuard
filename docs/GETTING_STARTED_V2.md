# GA CryptoGuard 补充规格 v2：结构化状态、动态 Skill、日内共振、模拟盘风控、自进化闭环

## 0. 核心原则

系统仍然保持：

```text
不接实盘
不执行真实交易
不保存交易权限 API Key
所有开仓、平仓、风控、复盘、进化均作用于模拟盘
```

新增核心原则：

```text
每次分析都必须产生结构化状态。
每次结构化状态都必须持久化。
下一次分析必须读取上一次状态作为上下文基石。
GA Skill 不只是代码函数，而是 Prompt + Tool + Feedback + Memory 的闭环。
模拟盘只接收通过风控筛选的信号。
实时飞书预警只在模拟盘开仓、平仓、止损、止盈、风控警报时推送。
每小时飞书播报属于报告，不属于实时预警。
```

---

# 一、每次分析必须输出结构化状态并持久化

## 1.1 目标

每次分析不能只输出自然语言结论，必须生成标准化 `MarketAnalysisState`，并写入 SQLite。

该状态是下次分析的上下文基石，用于判断：

```text
市场结构是否延续
之前等待的触发条件是否发生
突破是否确认
机会监控是否触发
趋势是否升级或衰竭
之前为什么没有交易机会
```

---

## 1.2 每次分析必须记录的字段

必须记录：

```text
1. 当前市场结构状态
2. 趋势清晰度
3. 无交易机会的归因
4. 关键关注点位
5. 下次分析的触发条件
6. 下次分析的建议时间
7. 等待突破的结构边界
8. 当前是否允许模拟盘开仓
9. 是否进入机会监控
10. 是否存在有效交易计划
```

---

## 1.3 MarketAnalysisState JSON Schema

Codex 应实现类似结构：

```json
{
  "symbol": "BTCUSDT",
  "analysis_time_utc": "2026-05-26T12:15:00Z",
  "analysis_mode": "scheduled_15m",
  "timeframes": ["4h", "1h", "15m", "5m"],

  "market_structure": {
    "direction_4h": "bullish",
    "trend_1h": "bullish_pullback",
    "structure_15m": "breakout_retest",
    "trigger_5m": "waiting_reversal",
    "structure_status": "trend_continuation_candidate"
  },

  "trend_clarity": {
    "score": 0.74,
    "level": "clear",
    "reason": [
      "4H direction remains bullish",
      "1H pullback holds previous structure low",
      "15M breakout retest is forming"
    ]
  },

  "no_trade_reason": {
    "has_no_trade": true,
    "reason_code": "waiting_for_pullback_confirmation",
    "detail": "5M has not yet produced reversal confirmation near the planned entry zone."
  },

  "key_levels": {
    "support": [68120, 67680],
    "resistance": [69200, 69850],
    "invalid_level": 68120,
    "breakout_boundary": {
      "upper": 69200,
      "lower": 68120
    },
    "waiting_zone": [68300, 68550]
  },

  "next_triggers": [
    {
      "type": "price_retest_zone",
      "timeframe": "5m",
      "condition": "price enters 68300-68550"
    },
    {
      "type": "order_flow_confirm",
      "timeframe": "5m",
      "condition": "cvd_slope turns up"
    }
  ],

  "next_analysis": {
    "suggested_time_utc": "2026-05-26T12:30:00Z",
    "reason": "Next 15M candle close"
  },

  "breakout_watch": {
    "enabled": true,
    "direction": "bullish",
    "boundary_high": 69200,
    "boundary_low": 68120,
    "confirmation_required": "15m close above 69200 and 5m retest hold"
  },

  "trade_permission": {
    "paper_trade_allowed": false,
    "reason": "Entry trigger not confirmed"
  },

  "opportunity_watch_recommended": true,

  "trade_plan": {
    "has_trade_plan": false
  }
}
```

---

## 1.4 SQLite 表设计

新增表：

```sql
CREATE TABLE IF NOT EXISTS analysis_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    analysis_time INTEGER NOT NULL,
    analysis_time_utc TEXT NOT NULL,
    analysis_mode TEXT NOT NULL,
    timeframes TEXT NOT NULL,

    market_structure_json TEXT NOT NULL,
    trend_clarity_json TEXT NOT NULL,
    no_trade_reason_json TEXT,
    key_levels_json TEXT,
    next_triggers_json TEXT,
    next_analysis_json TEXT,
    breakout_watch_json TEXT,
    trade_permission_json TEXT,
    trade_plan_json TEXT,

    opportunity_watch_recommended INTEGER DEFAULT 0,
    paper_trade_allowed INTEGER DEFAULT 0,
    state_json TEXT NOT NULL,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analysis_states_symbol_time
ON analysis_states(symbol, analysis_time);
```

读取最近状态：

```sql
SELECT *
FROM analysis_states
WHERE symbol = :symbol
ORDER BY analysis_time DESC
LIMIT 1;
```

---

## 1.5 分析上下文规则

每次分析前必须执行：

```text
1. 读取该 symbol 最近一次 analysis_state
2. 读取 active opportunity_watch
3. 读取 open paper_position
4. 读取最新 market profiles
5. 构建本次分析上下文
```

分析完成后必须写入：

```text
analysis_states
market_snapshots
module_analysis_results
strategy_evaluations
signals，若有信号
opportunity_watches，若用户确认加入
paper_orders，若用户确认且通过风控
```

---

# 二、GA Skill 定义：Prompt + Tool + Feedback + Memory 闭环

## 2.1 核心要求

以下能力必须封装为 GA 动态 Skill：

```text
1. 缠论 Skill
2. 价格行为 Skill
3. SMC 订单流 Skill
4. 动能判断 Skill
5. 趋势阶段判断 Skill
```

每个 Skill 不是单纯代码函数，也不是纯 Prompt，而是：

```text
Skill = Prompt SOP + Deterministic Tools + Output Schema + Feedback Memory + Evolution Rule
```

---

## 2.2 Skill 基础结构

建议新增目录：

```text
plugins/crypto_guard/skills/
  chanlun_skill/
    skill.yaml
    prompt.md
    tools.py
    schema.json
    feedback_rules.yaml

  price_action_skill/
  smc_orderflow_skill/
  momentum_skill/
  trend_stage_skill/
```

---

## 2.3 skill.yaml 标准格式

```yaml
skill_name: chanlun_skill
version: 1.0
status: active

description: >
  Identify Chanlun structure using deterministic preprocessing and GA logical interpretation.

tools:
  - chanlun_detect_fractals
  - chanlun_build_bi
  - chanlun_build_segments
  - chanlun_detect_zhongshu
  - chanlun_detect_divergence
  - chanlun_detect_buy_sell_points

input:
  required:
    - symbol
    - timeframe
    - closed_candles
    - previous_analysis_state

output_schema: schema.json

memory:
  read:
    - skill_memory
    - recent_trade_reviews
    - strategy_feedback
  write:
    - skill_execution_logs
    - skill_feedback_memory

evolution:
  enabled: true
  allow_prompt_patch: true
  allow_tool_param_patch: true
  require_shadow_testing: true
```

---

## 2.4 Skill 执行日志表

```sql
CREATE TABLE IF NOT EXISTS skill_execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    analysis_time INTEGER NOT NULL,
    input_summary_json TEXT,
    tool_result_json TEXT NOT NULL,
    ga_interpretation_json TEXT NOT NULL,
    final_result_json TEXT NOT NULL,
    confidence REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 2.5 Skill 反馈记忆表

```sql
CREATE TABLE IF NOT EXISTS skill_feedback_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    finding TEXT NOT NULL,
    suggested_adjustment_json TEXT,
    status TEXT DEFAULT 'candidate',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

反馈来源：

```text
trade_review
user_feedback
shadow_test
daily_review
strategy_evaluation
```

---

# 三、五大核心分析 Skill 实现要求

## 3.1 缠论 Skill

### 工具层必须负责

```text
包含关系处理
顶底分型
笔
线段
中枢
背驰
买卖点候选
```

### GA 负责

```text
判断当前结构含义
判断买卖点是否有效
判断是否进入趋势末端
结合多周期判断是否可交易
输出风险和等待条件
```

### 输出必须包含

```json
{
  "skill": "chanlun",
  "trend_structure": "up_after_zhongshu_breakout",
  "current_bi_direction": "down",
  "zhongshu": {
    "exists": true,
    "range_low": 68100,
    "range_high": 68900,
    "price_position": "above"
  },
  "divergence": {
    "exists": false,
    "type": null
  },
  "buy_sell_point": {
    "type": "class_2_buy_candidate",
    "valid": true,
    "confidence": 0.68
  },
  "risk_notes": [],
  "next_condition": "wait for lower timeframe reversal near zhongshu upper boundary"
}
```

---

## 3.2 价格行为 Skill

必须识别：

```text
关键支撑阻力
Swing High / Swing Low
HH / HL / LH / LL
BOS
CHoCH
突破回踩
假突破
区间震荡
形态识别
```

输出必须包含：

```json
{
  "skill": "price_action",
  "market_structure": "bullish",
  "swing_sequence": "HH_HL",
  "last_event": "bullish_bos",
  "pattern": "breakout_retest",
  "key_support": [68120, 67680],
  "key_resistance": [69200, 69850],
  "invalid_level": 68120,
  "confidence": 0.72
}
```

---

## 3.3 SMC 订单流 Skill

必须识别：

```text
订单块 Order Block
公允价值缺口 FVG
流动性猎取 Liquidity Sweep
Equal High / Equal Low
Premium / Discount
CVD
主动买入 / 主动卖出比例
价格-CVD 背离
```

输出必须包含：

```json
{
  "skill": "smc_orderflow",
  "liquidity_event": {
    "type": "sell_side_sweep",
    "reclaimed": true
  },
  "order_block": {
    "exists": true,
    "direction": "bullish",
    "range": [67880, 68150],
    "status": "unmitigated"
  },
  "fvg": {
    "exists": true,
    "direction": "bullish",
    "range": [68200, 68480],
    "status": "unfilled"
  },
  "order_flow": {
    "cvd_slope": "up",
    "aggressive_buy_ratio": 0.61,
    "delta_divergence": false
  },
  "setup": "bullish_reversal_after_sweep",
  "confidence": 0.74
}
```

---

## 3.4 动能判断 Skill

必须识别：

```text
量价配合
RSI slope
MACD histogram
ATR expansion
成交量脉冲
实体强度
指标背离
CVD 背离
上涨/下跌效率
```

输出必须包含：

```json
{
  "skill": "momentum",
  "direction": "bullish",
  "momentum_score": 78,
  "quality": "strong_but_extended",
  "volume_price_alignment": true,
  "indicator_divergence": false,
  "atr_state": "expanding",
  "risk": "short_term_overextended"
}
```

---

## 3.5 趋势阶段 Skill

必须识别：

```text
初期启动 early
中期延续 middle
末期衰竭 late
震荡 range
转折 transition
```

输出必须包含：

```json
{
  "skill": "trend_stage",
  "stage": "middle",
  "clarity": 0.76,
  "features": [
    "4H bullish direction",
    "1H pullback holding",
    "15M breakout retest"
  ],
  "late_stage_risk": false,
  "next_evolution": "5M reversal may grow into 15M continuation if structure holds"
}
```

---

# 四、多周期共振与交易逻辑

## 4.1 分析周期

系统采用自顶向下框架：

```text
日线 / 4H：方向背景
1H / 15M：结构与趋势
5M / 1M：入场、反转、触发机会
```

但当前日内策略默认主链路为：

```text
4H 找方向
1H / 15M 找趋势
5M 找入场和反转机会
```

日线可作为背景过滤，不强制作为日内权重主项。

---

## 4.2 顺大逆小原则

必须严格执行：

```text
顺大周期方向
逆小周期回调寻找反转入场
```

示例：

```text
4H 多头
1H / 15M 回调不破结构
5M 出现卖方流动性扫荡后回收
动能恢复
=> 才能考虑多头模拟盘候选
```

禁止：

```text
只因 5M 出现短线多头信号，就逆 4H 空头方向开多。
```

---

## 4.3 多周期评分模型

建议默认：

```yaml
multi_timeframe_logic:
  daily:
    role: background_filter
    enabled: true
    weight: 0.10

  4h:
    role: direction
    weight: 0.30

  1h:
    role: trend_structure
    weight: 0.25

  15m:
    role: setup_structure
    weight: 0.20

  5m:
    role: entry_trigger
    weight: 0.15
```

如果只做纯日内，可配置：

```yaml
daily.enabled: false
4h.weight: 0.35
1h.weight: 0.25
15m.weight: 0.20
5m.weight: 0.20
```

---

## 4.4 趋势演化监控

系统必须持续判断：

```text
小级别走势是否正在生长为更大级别趋势
```

例如：

```text
5M 反转
  ↓
15M 形成 higher low
  ↓
1H 回调结束
  ↓
4H 趋势延续
```

每次分析必须输出：

```json
{
  "trend_evolution": {
    "small_tf_growth": true,
    "from_timeframe": "5m",
    "target_timeframe": "15m",
    "condition": "5M reversal must hold 68120 and 15M close above 68800",
    "position_management_impact": "if confirmed, move stop_loss to breakeven and extend TP2"
  }
}
```

---

## 4.5 动态调整持仓预期与止损

如果小级别走势成功演化为大级别趋势，系统可以建议：

```text
延长持仓预期
移动止损到保本
提高 TP2 / TP3 权重
减少过早平仓
```

如果演化失败：

```text
缩短持仓预期
提前减仓
调整止损
关闭机会监控
平仓模拟单
```

所有止损调整必须推送飞书。

---

# 五、风控与模拟盘系统

## 5.1 量化风控标准

开仓必须满足：

```text
RR > 2:1，默认 min_rr = 2.0
高置信度，默认 confidence >= 0.72
结构 + 动能共振
高周期方向不冲突
非极端行情
不是低质量信号
```

配置：

```yaml
risk:
  min_rr: 2.0
  min_confidence: 0.72
  require_structure_momentum_alignment: true
  allow_manual_bypass: false
```

---

## 5.2 低质量信号拒绝规则

以下情况拒绝进入模拟盘：

```text
RR < 2
confidence < 0.72
高周期方向相反
趋势阶段为 late 且追单
震荡市误判趋势市
没有明确 invalid_level
没有止损
没有止盈
反向证据过强
极端行情状态
```

拒绝后可以建议：

```text
加入机会监控
等待回踩
等待突破确认
忽略
```

---

## 5.3 SQLite 模拟盘表

必须有：

```sql
CREATE TABLE IF NOT EXISTS paper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    initial_balance REAL NOT NULL,
    current_balance REAL NOT NULL,
    equity REAL NOT NULL,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL,
    quantity REAL NOT NULL,
    stop_loss REAL,
    take_profit_json TEXT,
    unrealized_pnl REAL DEFAULT 0,
    unrealized_pnl_pct REAL DEFAULT 0,
    max_favorable_excursion REAL DEFAULT 0,
    max_adverse_excursion REAL DEFAULT 0,
    status TEXT DEFAULT 'open',
    opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_trade_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    event_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    price REAL,
    quantity REAL,
    pnl REAL,
    pnl_pct REAL,
    reason TEXT,
    event_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5.4 价格更新

后台每 3 分钟获取币安最新价格。

使用最新价格计算：

```text
浮盈浮亏
真实收益率
最大浮盈 MFE
最大浮亏 MAE
账户权益
回撤
止盈止损是否触发
```

---

## 5.5 模拟盘开仓来源

只有以下来源可以创建模拟盘订单：

```text
通过风控的 GA 分析结果
用户点击“加入模拟盘”且风控通过
机会监控触发后重新评估并风控通过
```

人工指令不能绕过风控。

---

# 六、飞书预警机制

## 6.1 实时预警触发条件

实时飞书预警仅在以下情况触发：

```text
模拟盘开仓
模拟盘平仓
止损触发
止盈触发
调整止损
强风控警报
账户回撤超过阈值
连续止损触发自进化
机会监控触发后产生可执行计划
```

普通分析结果不作为实时预警刷屏。

用户主动分析的结果需要回复用户，但不算系统主动预警。

---

## 6.2 每小时整点播报

每小时通过飞书推送结构化报告。

必须包含：

```text
1. 已分析币种及结论
2. 当前等待的触发条件
3. 模拟盘持仓
4. 模拟盘净值曲线摘要
5. 系统健康度指标
6. 风险事件
7. 当前机会监控
8. 是否存在待用户确认的操作
```

---

## 6.3 每小时播报结构

示例：

```json
{
  "report_type": "hourly_summary",
  "time_utc": "2026-05-26T13:00:00Z",
  "analyzed_symbols": [
    {
      "symbol": "BTCUSDT",
      "conclusion": "bullish pullback, waiting 5M reversal",
      "trade_plan": "no active entry, watch 68300-68550",
      "position": "none"
    }
  ],
  "opportunity_watches": [
    {
      "symbol": "SOLUSDT",
      "condition": "wait for 5M CVD turn up near 168.40"
    }
  ],
  "paper_account": {
    "equity": 10234.5,
    "daily_pnl": 123.4,
    "drawdown": 0.018
  },
  "system_health": {
    "scheduler": "ok",
    "market_data": "ok",
    "feishu": "ok",
    "pending_jobs": 2
  }
}
```

---

# 七、自进化触发条件

## 7.1 触发条件

必须实现以下触发条件：

```text
1. 连续 3 次模拟盘止损
2. 模拟盘总资金回撤 > 10%
```

配置：

```yaml
evolution:
  trigger:
    consecutive_stop_losses: 3
    max_account_drawdown_pct: 0.10
```

---

## 7.2 触发后流程

触发后不能直接改策略。

必须执行：

```text
1. 暂停相关策略的新模拟盘开仓权限，进入观察模式
2. 执行归因分析
3. 检查是否为极端行情
4. 如果非极端行情，生成 candidate strategy patch
5. candidate 进入 shadow_testing
6. 与原策略对比
7. 满足条件后等待用户确认升级
```

---

## 7.3 连续止损计数

建议表：

```sql
CREATE TABLE IF NOT EXISTS evolution_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    strategy_name TEXT,
    symbol TEXT,
    trigger_value REAL,
    threshold_value REAL,
    related_trade_ids TEXT,
    market_regime TEXT,
    evolution_allowed INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);
```

---

# 八、每日复盘

## 8.1 执行时间

每日自动执行，建议 UTC 00:05。

```yaml
daily_review:
  cron: "5 0 * * *"
  timezone: "UTC"
```

---

## 8.2 每日复盘内容

必须总结：

```text
当日所有模拟订单
盈利订单原因
亏损订单原因
分析失效原因
未交易机会是否合理
机会监控触发质量
策略表现
Skill 表现
下次注意要点
是否触发自进化
```

---

## 8.3 复盘输出结构

```json
{
  "date_utc": "2026-05-26",
  "paper_summary": {
    "trades": 8,
    "wins": 4,
    "losses": 4,
    "daily_pnl": 128.5,
    "max_drawdown": 0.032
  },
  "win_analysis": [
    {
      "trade_id": 101,
      "reason": "4H direction and 15M setup aligned, 5M reversal confirmed"
    }
  ],
  "loss_analysis": [
    {
      "trade_id": 102,
      "primary_reason": "trend_stage_misclassified",
      "detail": "1H was late stage but treated as middle continuation"
    }
  ],
  "analysis_failures": [
    {
      "symbol": "ETHUSDT",
      "failure": "waited for breakout but structure evolved into range"
    }
  ],
  "next_focus_points": [
    "Reduce long continuation score when 1H late-stage risk is detected",
    "Require stronger 5M order flow confirmation during high volatility"
  ],
  "skill_memory_updates": [
    {
      "skill": "trend_stage_skill",
      "finding": "Late-stage misclassification caused 2 losses today"
    }
  ],
  "evolution": {
    "triggered": true,
    "reason": "3 consecutive stop losses",
    "action": "created candidate patch for smc_pullback_long"
  }
}
```

---

## 8.4 写入 GA Skill 记忆库

每日复盘必须更新：

```text
skill_feedback_memory
strategy_memory
trade_reviews
daily_review_reports
```

建议表：

```sql
CREATE TABLE IF NOT EXISTS daily_review_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL UNIQUE,
    summary_json TEXT NOT NULL,
    ga_report TEXT NOT NULL,
    skill_updates_json TEXT,
    evolution_actions_json TEXT,
    pushed_to_feishu INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 8.5 推送飞书

每日复盘完成后必须推送飞书。

推送内容：

```text
今日模拟盘表现
主要盈利原因
主要亏损原因
明日注意事项
是否触发自进化
策略是否进入观察期
```

---

# 九、Codex 实现顺序

请 Codex 按以下顺序实现：

```text
1. analysis_states 表和 MarketAnalysisState 输出
2. 每次分析前读取上一次 analysis_state
3. 五大 Skill 的目录结构、配置、Schema 和执行日志
4. 价格行为 / 动能 / 趋势阶段 Skill 优先落地
5. SMC 订单流 Skill
6. 缠论 Skill
7. 多周期共振与趋势演化监控
8. SQLite 模拟盘账户、持仓、日志增强
9. 每 3 分钟价格更新与账户权益计算
10. 飞书实时预警规则收敛
11. 每小时结构化播报
12. 自进化触发器
13. 每日复盘与 Skill 记忆更新
14. 验收测试
```

---

# 十、Codex 验收标准

## 10.1 分析状态验收

```text
每次分析必须写入 analysis_states。
analysis_states 必须包含：
市场结构状态
趋势清晰度
无交易机会归因
关键关注点位
下次触发条件
下次分析时间
突破边界
```

---

## 10.2 Skill 验收

```text
五大 Skill 必须有 skill.yaml、prompt.md、tools.py、schema.json。
每次 Skill 执行必须写入 skill_execution_logs。
Skill 结果必须能被 MarketAnalysisState 引用。
每日复盘必须能写入 skill_feedback_memory。
```

---

## 10.3 多周期验收

```text
系统必须支持 4H -> 1H/15M -> 5M 主链路。
日线只能作为背景过滤，除非配置启用。
必须执行顺大逆小。
5M 信号不能单独覆盖 4H 方向。
```

---

## 10.4 模拟盘验收

```text
模拟账户余额、持仓、日志必须存 SQLite。
价格每 3 分钟更新。
浮盈亏、收益率、回撤必须计算。
只有通过风控的信号才能创建模拟盘订单。
```

---

## 10.5 飞书验收

```text
实时预警只在模拟盘开仓、平仓、止盈、止损、风控警报时推送。
每小时必须推送结构化报告。
用户主动分析必须有回复和按钮。
普通分析不得主动刷屏。
```

---

## 10.6 自进化验收

```text
连续 3 次止损必须触发 evolution_triggers。
总资金回撤 >10% 必须触发 evolution_triggers。
每日复盘必须生成归因。
每日复盘必须更新 Skill 记忆。
策略补丁必须进入 candidate / shadow_testing，不得直接覆盖 active。
```