# 02. 系统架构设计

## 1. 总体架构

```text
Feishu 用户 / 群聊
        ↓
GenericAgent Feishu Frontend
        ↓
User Interaction Queue，高优先级
        ↓
ga_user_worker
        ↓
crypto_guard tools
        ↓
SQLite / Redis / Market Data Cache
```

后台任务链路：

```text
UTC Scheduler Worker
        ↓
K 线获取 / 市场画像 / 模拟盘更新 / 复盘触发
        ↓
需要 GA 推理时，写入 Background Queue
        ↓
ga_background_worker
        ↓
写 signals / reviews / strategy_memory
        ↓
Feishu 通知
```

## 2. 为什么不能把定时任务全部放进 GA 主 Loop

用户已经遇到过：GA 定时任务覆盖用户消息，导致飞书消息没有回应。

根本原因是：

- 定时任务和用户对话共用 session / context。
- 定时任务长推理阻塞主 Loop。
- 后台任务污染用户短期上下文。
- 多个任务并发时状态串线。

必须使用：

```text
独立 scheduler worker
+ 独立 queue
+ 独立 user/background GA workers
+ 独立 session_id
```

## 3. 进程划分

### 3.1 feishu_agent_server

职责：

- 接收飞书事件。
- 快速 ACK。
- 解析基础 event。
- 写入 `agent_jobs` 或 Redis user stream。
- 不执行长任务。

### 3.2 crypto_scheduler_worker

职责：

- 基于 UTC cron 触发任务。
- 记录 `scheduler_runs`。
- 拉取已收盘 K 线。
- 构建 market profile。
- 触发 15m 分析。
- 触发 daily review。
- 不直接占用用户对话 Loop。

### 3.3 market_data_worker

职责：

- Binance REST / WebSocket。
- mark price、kline、aggTrade。
- 实时价格缓存。
- 订单流窗口缓存。
- K 线增量写库。

### 3.4 paper_trade_worker

职责：

- 每 3/5 分钟更新模拟盘。
- 计算浮盈亏。
- 检查止盈止损。
- 写 equity snapshots。
- 触发平仓复盘 job。

### 3.5 ga_user_worker

职责：

- 处理飞书用户消息。
- 自然语言意图识别。
- 产品池管理。
- 临时分析。
- 用户按钮回调。
- 优先级最高。

### 3.6 ga_background_worker

职责：

- 处理定时市场分析。
- 日线/4H总结。
- 交易复盘。
- 策略补丁生成。
- 影子测试结果总结。

## 4. Session 隔离

必须使用不同 session_id：

```text
用户会话：feishu:user:{open_id}
群聊会话：feishu:chat:{chat_id}
15m 定时分析：system:scheduled:15m:{symbol}
日线总结：system:scheduled:daily:{symbol}
复盘任务：system:review:{trade_id}
影子测试：system:shadow:{strategy_name}:{version}
```

后台任务不得写入用户短期记忆。它只能写：

- market_profiles。
- signals。
- trade_reviews。
- strategy_memory。
- strategy_versions。

## 5. 数据流

### 5.1 定时 K 线数据流

```text
scheduler tick
  → calculate expected closed candle time
  → fetch missing candles
  → upsert candles
  → update module_analysis_results
  → update market_profiles
```

### 5.2 15m 分析数据流

```text
fetch latest closed 15m candle
  → read 1D/4H/1H profiles
  → read latest 15m/5m candles
  → read orderflow cache
  → build MarketStateSnapshot
  → pre-score
  → if score high, enqueue GA background job
  → GA outputs decision JSON
  → save signal / opportunity_watch / paper candidate
  → notify Feishu when needed
```

### 5.3 用户临时分析数据流

```text
Feishu message
  → user queue
  → ga_user_worker
  → parse intent
  → normalize symbol
  → ensure data available
  → build snapshot
  → run SOP
  → return Feishu card with actions
```

### 5.4 模拟盘数据流

```text
paper_order pending/open
  → paper worker reads latest price
  → update fills / PnL / MFE / MAE
  → hit SL/TP/timeout/invalid condition
  → close trade
  → enqueue review job
```

## 6. GenericAgent 改造原则

优先新增插件，不修改核心：

```text
plugins/crypto_guard/*
frontends/fsapp.py 仅做必要接入
```

尽量不要改：

```text
agent_loop.py
llmcore.py
memory/*
```

除非确实需要给工具注册和 session 隔离做最小改动。
