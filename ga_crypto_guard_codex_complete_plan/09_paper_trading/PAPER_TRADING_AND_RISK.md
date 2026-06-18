# 风控与模拟盘规范

## 1. 开仓门槛

默认：

```yaml
risk:
  min_rr: 2.0
  min_confidence: 0.72
  require_structure_momentum_alignment: true
  allow_manual_bypass: false
```

必须拒绝：

- RR < 2
- confidence < 0.72
- 高周期方向相反
- 趋势末期追单
- 无止损
- 无止盈
- 无 invalid_level
- 反向证据过强
- 极端行情

## 2. 人工指令不能绕过风控

用户发：

```text
立即模拟开仓 BTC
```

系统必须：

```text
GA 分析 -> 风控 -> 通过后才允许模拟盘
```

## 3. 价格更新

每 3 分钟更新一次最新价格。

优先读取 Redis：

```text
latest_price:{symbol}
```

若 Redis 缺失，再调用行情接口，更新 Redis。

## 4. 市价模拟成交

```text
下一根 K 线开盘价 ± 0.1% 滑点
```

多单：

```text
fill_price = next_open * (1 + 0.001)
```

空单：

```text
fill_price = next_open * (1 - 0.001)
```

## 5. 挂单模拟成交

高置信度可使用挂单：

```text
low <= entry_price <= high -> 成交
```

成交价默认 `entry_price`。

## 6. 模拟盘状态

SQLite 必须存：

- account balance
- equity
- realized pnl
- unrealized pnl
- drawdown
- positions
- trade logs
- MFE / MAE
- Entry Efficiency
- Exit Efficiency

## 7. 只有 GADecision 可以创建订单

`paper_orders` 必须引用 `ga_decision_id`。
