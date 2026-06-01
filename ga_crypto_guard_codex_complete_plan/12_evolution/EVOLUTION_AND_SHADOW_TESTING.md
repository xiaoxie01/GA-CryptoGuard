# 自进化与影子测试

## 1. 触发条件

```yaml
evolution:
  trigger:
    consecutive_stop_losses: 3
    max_account_drawdown_pct: 0.10
```

## 2. 触发后动作

1. 暂停相关策略的新模拟盘开仓权限。
2. 执行归因分析。
3. 检查是否极端行情。
4. 非极端行情才允许生成 candidate patch。
5. candidate 进入 shadow_testing。
6. 与 active 进行对比。
7. 用户确认后才能升级。

## 3. 极端行情过滤

以下情况暂停进化触发：

- extreme_volatility
- funding_shock
- news_like_event
- low_liquidity

亏损仍记录，但不直接归因于策略失效。

## 4. Skill patch 类型

允许：

- prompt patch
- tool 参数 patch
- confidence rule patch
- strategy filter patch
- timeframe weight patch

禁止：

- 自动覆盖 active
- 静默升级
- 直接接入实盘

## 5. Shadow Testing

candidate 版本：

- 并行分析
- 只记录，不创建模拟盘订单
- 不推送实时预警
- 统计假设表现

最小观察：3 个有效信号。
推荐升级样本：>=30。
