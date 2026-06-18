# 02 存储与缓存建议

## MVP 后推荐组合

```text
SQLite：主库
Redis：队列、锁、实时缓存
Parquet：长期 K 线归档
DuckDB：历史回放和分析型查询
```

## SQLite 负责

- symbols
- scheduler_runs
- agent_jobs
- market_snapshots
- module_analysis_results
- strategy_evaluations
- signals
- opportunity_watches
- paper_orders
- paper_trades
- trade_reviews
- strategy_versions
- strategy_patches
- shadow_test_results
- user_feedback

## Redis 负责

- 用户消息队列
- 后台任务队列
- 实时 mark price
- 最新成交流窗口
- 任务锁
- 飞书事件去重
- 模拟盘实时权益缓存

## Parquet / DuckDB 负责

- 长期 K 线归档
- 历史回放
- 策略版本对比
- 多品种统计
- MFE/MAE 聚合分析
