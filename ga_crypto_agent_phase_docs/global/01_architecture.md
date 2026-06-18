# 01 系统架构

## 推荐进程

```text
feishu_agent_server.py
  - 接收飞书用户消息
  - 处理按钮回调
  - 快速 ACK
  - 投递高优先级 user job

crypto_scheduler_worker.py
  - UTC cron
  - 拉取已收盘 K 线
  - 更新高周期画像
  - 创建后台分析 job

market_data_worker.py
  - Binance WebSocket
  - 最新价格 / mark price / aggTrade
  - 写入 Redis 或轻量缓存

paper_trade_worker.py
  - 更新模拟盘浮盈亏
  - 触发止盈止损
  - 生成平仓事件

ga_user_worker.py
  - 只处理用户飞书交互
  - 最高优先级

ga_background_worker.py
  - 处理定时总结、复盘、策略补丁
  - 不污染用户 session
```

## 核心数据流

```text
Binance 行情
  ↓
K 线缓存 / 实时价格缓存
  ↓
结构化分析模块
  ↓
MarketStateSnapshot
  ↓
GA Reasoning
  ↓
飞书预警 / 机会监控 / 模拟盘
  ↓
SQLite 持久化
  ↓
复盘归因
  ↓
策略补丁
  ↓
影子测试
  ↓
策略升级
```

## 关键隔离原则

- 用户消息走 user queue。
- 定时任务走 background queue。
- 后台 session_id 不能复用用户 session_id。
- 用户短期记忆不能被后台任务写入。
- 数据处理任务不直接调用 GA，只有需要解释、总结、策略判断时才投递 GA job。
