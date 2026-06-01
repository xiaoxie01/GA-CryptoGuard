# 07. 飞书交互设计

## 1. 交互原则

- 飞书入口快速 ACK，不执行长任务。
- 用户消息 priority=1。
- 按钮回调 priority=2。
- 后台任务不得阻塞用户响应。
- 所有用户输入通过自然语言意图识别转为结构化命令。

## 2. 支持的自然语言命令

### 2.1 产品池管理

```text
把 WIFUSDT 加入监控
以后也分析 ORDIUSDT
暂停分析 DOGE
移除 LTCUSDT
列出当前监控品种
今天重点分析 BTC ETH SOL
```

### 2.2 临时分析

```text
分析一下 SUIUSDT 现在有没有机会
只临时看一下 WIF，不要加入监控
帮我看 BTC 现在能不能做多
```

### 2.3 机会监控

```text
SOL 如果回踩 168 附近再提醒我
这个币不确定，先帮我盯着
如果 BTC 重新站回 68000 并且 CVD 转强提醒我
```

### 2.4 模拟盘

```text
把刚才那个 SOL 计划加入模拟盘
列出当前模拟盘持仓
复盘最近 10 笔模拟单
```

## 3. Intent Schema

```json
{
  "intent": "add_symbol|remove_symbol|pause_symbol|resume_symbol|list_symbols|analyze_once|create_opportunity_watch|create_paper_order|list_paper_positions|review_trades",
  "symbol": "SOLUSDT",
  "timeframes": ["4h", "1h", "15m", "5m"],
  "scope": "long_term_watchlist|temporary|opportunity_watch",
  "raw_text": "用户原始消息"
}
```

## 4. 临时分析卡片

### 4.1 有完整交易计划

卡片内容：

```text
📊 SOLUSDT 临时分析

方向：模拟多单候选
趋势阶段：1H 多头中期，15m 回踩确认
信号等级：A
置信度：76%

理由：
- 1H 高周期偏多
- 15m bullish BOS
- SMC 回踩 FVG
- CVD 转强

风险：
- 5m 短线动能偏热
- 跌破 165.90 结构失效

模拟计划：
Entry: 168.40
SL: 165.90
TP1: 173.20
TP2: 177.60
RR: 2.1

不构成实盘建议。
```

按钮：

```text
[加入模拟盘] [加入机会监控] [加入长期产品池] [忽略]
```

### 4.2 无完整交易计划但有等待条件

```text
📊 WIFUSDT 临时分析

当前结论：做多观察，但不适合追价
趋势阶段：15m 初期启动，1H 仍在震荡上沿
信号等级：B

等待条件：
- 回踩 2.31-2.36 区域
- 5m 形成 higher low
- CVD 转强

失效条件：
跌破 2.24

不构成实盘建议。
```

按钮：

```text
[加入机会监控] [加入长期产品池] [忽略]
```

### 4.3 无优势

```text
📊 DOGEUSDT 分析

当前结论：无明显优势，不建议模拟盘
原因：
- 1H 震荡
- 15m 信号互相矛盾
- 订单流无确认

系统仅记录本次分析。
```

按钮：

```text
[加入长期产品池] [忽略]
```

## 5. 按钮回调逻辑

### 5.1 加入模拟盘

前置条件：

- `has_trade_plan = true`。
- signal_id 存在。
- 同 signal_id 没有重复 paper_order。

动作：

```text
读取 signal.trade_plan
创建 paper_order
写入 paper_orders
通知 paper_worker
飞书确认
```

### 5.2 加入机会监控

前置条件：

- GA decision 中存在 opportunity_watch。
- watch_condition_json 有效。

动作：

```text
创建 opportunity_watches
设置 expires_at
后续 15m/5m 任务检查
```

### 5.3 加入长期产品池

动作：

```text
写入 symbols
source=user
enabled=1
后续 scheduler 自动覆盖
```

## 6. 飞书推送频率控制

推送：

- S/A 级信号。
- 用户关注品种的 B 级信号。
- 模拟盘开仓/平仓。
- 机会监控触发/失效。
- 4H/1D 重大结构变化。
- 每日复盘。

不推送或聚合：

- C/D 级普通分析。
- 高频模拟盘浮盈亏普通更新。
- shadow testing 普通信号。

## 7. GA 工具列表

```python
crypto_symbol_add(symbol, category="custom", timeframes=None, enabled=True)
crypto_symbol_remove(symbol)
crypto_symbol_pause(symbol)
crypto_symbol_resume(symbol)
crypto_symbol_list()
crypto_analyze_symbol_once(symbol, timeframes=None)
crypto_create_opportunity_watch(symbol, watch_condition, expire_minutes=240)
crypto_create_paper_order_from_signal(signal_id)
crypto_get_market_state(symbol, timeframes)
crypto_get_open_paper_positions()
crypto_review_trade(trade_id)
crypto_get_strategy_stats(strategy_name)
```
