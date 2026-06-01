# 03 调度与队列

## 设计原则

- UTC-0 调度。
- 所有 K 线任务只处理已收盘 K 线。
- 定时任务不得直接占用 GA 主对话 Loop。
- 用户消息优先级最高。
- 后台任务必须有独立 session_id。
- 任务必须幂等，可重试，可跳过。

## 推荐优先级

| priority | 类型 |
|---:|---|
| 1 | 飞书用户消息 |
| 2 | 用户按钮回调 |
| 3 | 重要预警解释 |
| 4 | 模拟盘开平仓复盘 |
| 5 | 15m 定时分析 |
| 7 | 日线 / 4H 总结 |
| 9 | 历史回放 / 影子测试 |

## 任务调度

```yaml
timezone: UTC

jobs:
  fetch_1d_klines:
    cron: "1 0 * * *"
  fetch_4h_klines:
    cron: "1 0,4,8,12,16,20 * * *"
  fetch_1h_klines:
    cron: "1 * * * *"
  analyze_market_15m:
    cron: "1,16,31,46 * * * *"
  update_paper_positions_3m:
    cron: "*/3 * * * *"
  daily_review:
    cron: "8 0 * * *"
```

## session_id 规范

```text
feishu:user:{open_id}
feishu:chat:{chat_id}
system:scheduled:15m_analysis
system:scheduled:daily_review
system:symbol:{symbol}:{timeframe}
system:review:{trade_id}
```
