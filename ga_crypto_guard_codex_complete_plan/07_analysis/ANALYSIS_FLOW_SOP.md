# 分析流程 SOP

## 1. 临时分析

```text
Feishu user message: 分析 XXX
  ↓
GA Master Controller receives request
  ↓
ContextBuilder loads state and data
  ↓
SkillOrchestrator runs selected Skills
  ↓
GA multi-timeframe reasoning
  ↓
RiskGate
  ↓
GADecision
  ↓
Persist ga_decisions + analysis_states
  ↓
Feishu compact analysis card + buttons
```

## 2. 定时分析

```text
Scheduler only creates background GA job
  ↓
GA Background Worker consumes job
  ↓
GA Master Controller performs analysis
  ↓
GADecision persisted
  ↓
Only S/A/B and critical events are eligible for hourly report details
```

Scheduler 禁止直接生成交易结论。

## 3. 多周期日内逻辑

默认：

```text
4H：找方向
1H / 15M：找趋势和结构
5M / 1M：找入场或反转
```

可选日线：背景过滤，不默认参与日内主权重。

## 4. 顺大逆小

顺大周期趋势，逆小周期回调找反转入场。

多头例子：

```text
4H bullish
1H/15M pullback holds structure
5M sell-side sweep and reclaim
momentum turns up
orderflow confirms
```

空头例子：

```text
4H bearish
1H/15M rebound rejects resistance
5M buy-side sweep and fail
momentum turns down
orderflow confirms
```

## 5. No Edge 处理

如果多周期矛盾、趋势不清晰、RR 不足或反向证据过强：

```text
signal_grade = C or D
decision = no_edge or monitor_only
只记录，不创建模拟盘
D 级不建议机会监控
```

## 6. 分析状态必须持久化

每次分析都必须写入 `analysis_states`，包含：

- 当前市场结构状态
- 趋势清晰度
- 无交易机会归因
- 关键关注点位
- 下次触发条件
- 下次分析时间
- 等待突破边界
- 趋势演化监控
- 模拟盘权限
