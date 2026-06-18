# 04 GA 输出契约

所有 GA 分析必须返回结构化 JSON，并保存到数据库。

## 决策枚举

```text
paper_trade_candidate
opportunity_watch
monitor_only
no_edge
avoid_chop
wait_for_pullback
wait_for_breakout
wait_for_reclaim
```

## 信号等级

```text
S: 强信号，可进入模拟盘候选
A: 高质量机会，推送飞书
B: 有倾向，加入机会监控
C: 观察，只入库不推送
D: 无优势，忽略
```

## 必须包含反向证据

每次分析必须包含：

- bullish_evidence
- bearish_evidence
- neutral_or_risk_evidence
- contradiction_level

## 交易计划完整性要求

只有同时具备以下字段，才能允许“一键加入模拟盘”：

- side
- entry_type
- entry_price 或 trigger_condition
- stop_loss
- take_profits
- risk_percent
- invalid_condition
- reason
