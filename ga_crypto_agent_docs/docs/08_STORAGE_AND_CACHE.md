# 08. 存储与缓存设计

## 1. 推荐组合

MVP：

```text
SQLite + WAL
```

稳定运行：

```text
SQLite + WAL + Redis
```

回测分析阶段：

```text
SQLite + Redis + Parquet + DuckDB
```

## 2. SQLite 用途

SQLite 作为主库，存储：

- symbols。
- candles 热数据。
- market_profiles。
- module_analysis_results。
- signals。
- opportunity_watches。
- paper_orders。
- paper_trades。
- trade_reviews。
- strategy_versions。
- strategy_patches。
- scheduler_runs。
- agent_jobs。
- user_feedback。

推荐 PRAGMA：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

## 3. Redis 用途，可选但推荐

Redis 用于：

- 用户消息队列。
- 后台任务队列。
- 实时价格缓存。
- mark price 缓存。
- orderflow 窗口缓存。
- 分布式锁。
- 飞书事件去重。
- 模拟盘实时浮盈亏缓存。

Key 示例：

```text
price:BTCUSDT
mark_price:BTCUSDT
funding:BTCUSDT
orderflow:BTCUSDT:5m
lock:analyze:BTCUSDT:15m
feishu:event:{event_id}
queue:ga:user
queue:ga:background
```

## 4. DuckDB + Parquet，用于第二阶段

长期 K 线归档不是 MVP 必须项，但对自进化有价值：

- 历史回放。
- 策略版本比较。
- 防未来函数检查。
- 多市场状态样本积累。
- 减少重复请求 Binance。

建议路径：

```text
data/klines/binance_um/BTCUSDT/15m/2026-05.parquet
data/klines/binance_um/BTCUSDT/1h/2026.parquet
```

SQLite 可保留热数据：

```text
1m: 最近 3-7 天
3m/5m: 最近 14 天
15m: 最近 60-90 天
1h/4h/1d: 可长期保留
```

## 5. 数据保留策略

MVP 不做归档也可以，但 schema 需要为后续保留扩展：

- `candles.source`。
- `candles.is_closed`。
- `candles.created_at` / `updated_at`。
- `market_profiles.profile_time`。
- `analysis_time`。

## 6. 未来函数防护

所有分析查询必须：

```sql
WHERE close_time <= :analysis_time_utc
```

禁止在 15m 分析时使用未收盘的 15m K 线。

允许低周期实时触发使用实时价格/订单流，但必须在 snapshot 中标记：

```json
{
  "data_type": "realtime_trigger",
  "is_closed_candle": false
}
```

## 7. 数据库迁移

实现时建议提供：

```text
plugins/crypto_guard/storage/migrations.py
plugins/crypto_guard/storage/schema.sql
```

启动时：

1. 连接 SQLite。
2. 设置 PRAGMA。
3. 执行 schema。
4. 检查必要配置。
5. 初始化默认 symbols 和策略版本。
