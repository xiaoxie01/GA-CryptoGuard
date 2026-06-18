# 调度与队列规范

## 1. Scheduler 的职责

Scheduler 只负责：

- 到点唤醒
- 数据准备
- 写入 GA job
- 记录 scheduler_runs

Scheduler 禁止：

- 直接输出交易结论
- 直接创建模拟盘订单
- 直接推送完整分析

## 2. UTC 调度

```yaml
scheduler:
  timezone: UTC
  jobs:
    fetch_4h: "0 */4 * * *"
    fetch_1h: "0 * * * *"
    analyze_15m: "*/15 * * * *"
    update_paper_prices: "*/3 * * * *"
    hourly_report: "0 * * * *"
    daily_review: "5 0 * * *"
```

## 3. K 线收盘边界

15m 分析必须在下一根 15m 收盘后延迟 30 秒执行。

示例：

```text
now = 06:09:59Z
next 15m close = 06:15:00Z
analysis_time = 06:15:30Z
```

## 4. Redis 队列优先级

用户消息高于后台任务：

```text
queue:user:feishu > queue:ga:background
```

后台 Worker 每次处理前必须检查用户队列。

## 5. 锁

用 Redis：

```text
lock:job:analyze_15m
lock:job:update_paper_prices
lock:symbol:BTCUSDT:15m
```

Redis 不可用时 fallback SQLite `task_locks`。
