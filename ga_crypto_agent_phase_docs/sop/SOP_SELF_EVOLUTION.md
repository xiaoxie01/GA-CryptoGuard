# SOP 自进化闭环

## 流程

```text
signal
  ↓
paper_order
  ↓
paper_trade
  ↓
trade_review
  ↓
strategy_patch(candidate)
  ↓
shadow_testing
  ↓
promotion_or_rejection
```

## 强制约束

- 复盘建议不能直接修改 active 策略。
- 候选策略必须 shadow_testing。
- 样本不足不能升级。
- 升级必须保留旧版本。
- 升级原因必须可解释。
