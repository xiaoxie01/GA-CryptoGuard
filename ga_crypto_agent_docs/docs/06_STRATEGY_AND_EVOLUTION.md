# 06. 策略与自进化设计

## 1. 自进化定义

本系统中的自进化不是让 GA 随意修改策略，而是：

```text
SOP 执行 → 结构化记录 → 模拟盘验证 → 复盘归因 → 策略补丁 → 候选版本 → 影子测试 → 达标后升级
```

GA 可以提出建议，但不能直接覆盖 active 策略。

## 2. 策略模板

策略模板是：

```text
适用市场状态 + 必要证据 + 可选证据 + 风控过滤 + 权重评分 + 输出动作
```

示例见 `configs/strategies.yaml`。

## 3. 策略评分

每个策略输出：

```json
{
  "strategy_name": "smc_pullback_long",
  "strategy_version": "1.0",
  "score": 0.76,
  "decision": "paper_trade_candidate",
  "evidence": [],
  "counter_evidence": [],
  "risk_filters_passed": true
}
```

动作映射：

| score | 动作 |
|---:|---|
| >= 0.80 | S 级，可创建模拟盘候选 |
| 0.72 - 0.79 | A 级，推送并允许用户选择 |
| 0.65 - 0.71 | B 级，机会监控 |
| 0.50 - 0.64 | C 级，仅记录 |
| < 0.50 | D 级，忽略 |

## 4. 复盘 SOP

```text
SOP_TRADE_REVIEW

1. 读取开单时 market_snapshot。
2. 读取策略评分和 GA 原始判断。
3. 读取持仓过程价格路径。
4. 读取出场原因。
5. 计算：
   - pnl_r
   - MFE
   - MAE
   - entry_efficiency
   - exit_efficiency
   - holding_minutes
6. 对照开单理由逐条检查：
   - 哪条证据有效？
   - 哪条证据失效？
   - 是否忽略反向证据？
   - 趋势阶段是否判断错误？
   - 是否追价？
   - 止损是否过窄？
   - 是否 BTC 环境不支持？
7. 输出归因 JSON。
```

## 5. 亏损原因分类

`primary_loss_reason` 必须从以下枚举中选择：

```text
wrong_direction
trend_stage_misclassified
late_trend_chasing
range_misread_as_trend
ignored_counter_evidence
ignored_btc_context
entry_chasing
entry_too_early
entry_too_late
stop_loss_too_tight
take_profit_too_far
orderflow_not_confirmed
smc_false_signal
chanlun_divergence_missed
volatility_spike
news_like_move
```

## 6. 策略补丁

复盘后 GA 可以生成策略补丁：

```json
{
  "strategy_name": "smc_pullback_long",
  "from_version": "1.0",
  "candidate_version": "1.1-candidate",
  "change_reason": "连续亏损集中在 1H late trend + BTC risk_off 背景",
  "patch": {
    "score_adjustments": {
      "late_trend_penalty": -0.2,
      "btc_risk_off_penalty": -0.15
    },
    "risk_filters": [
      "disallow_if btc_context == risk_off and trend_stage == late"
    ]
  }
}
```

状态必须为 `candidate`。

## 7. 策略版本状态

```text
candidate       新生成，尚未验证
shadow_testing  影子测试中
active          正式启用
deprecated      已废弃
disabled        禁用
```

## 8. 影子测试

active 策略正常推送和创建模拟盘候选。

candidate 策略只记录假设信号：

- 不推送飞书。
- 不创建模拟盘订单。
- 只保存 shadow evaluations。

升级条件建议：

```text
sample_count >= 30
candidate_avg_r > active_avg_r
candidate_max_drawdown <= active_max_drawdown
candidate 不明显减少优质机会
不同品种表现不过度集中
```

## 9. 学习对象

GA 不应该学习模糊句子，而应该学习结构化经验：

```json
{
  "lesson_type": "strategy_weight_adjustment",
  "condition": {
    "strategy": "smc_pullback_long",
    "symbol_category": "high_beta",
    "higher_tf_trend_stage": "late",
    "btc_context": "risk_off"
  },
  "finding": "long setups have poor follow-through",
  "adjustment": {
    "score_penalty": -0.18,
    "preferred_action": "opportunity_watch"
  },
  "evidence": {
    "sample_count": 42,
    "avg_r": -0.22,
    "loss_rate": 0.64
  }
}
```

## 10. 用户反馈学习

飞书按钮可写入 `user_feedback`：

- 有用。
- 误报。
- 错过机会。
- 方向错了。
- 入场太晚。
- 止损太近。
- 解释不清楚。
- 推送太频繁。

用户反馈不直接改策略，但可作为复盘证据。
