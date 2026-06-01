# Redis / Parquet / DuckDB 接入规范

## 1. 存储分工

| 组件 | 职责 |
|---|---|
| SQLite | 业务状态、GA 决策、模拟盘、复盘、策略版本、审计 |
| Redis | 队列、缓存、任务锁、飞书去重、静默期、最新价格 |
| Parquet | 长期 K 线归档，由项目自行管理 |
| DuckDB | 查询 Parquet、报表聚合、回测统计、策略表现分析 |

## 2. Redis key 规范

```text
queue:user:feishu
queue:ga:background
queue:market:data
latest_price:{symbol}
mark_price:{symbol}
lock:job:{job_name}
quiet:{symbol}:{alert_type}
dedupe:feishu_event:{event_id}
health:redis:last_ping
```

## 3. Redis Adapter 接口

Codex 应实现：

```python
class RedisAdapter:
    def is_available(self) -> bool: ...
    def enqueue_user_job(self, payload: dict) -> str: ...
    def enqueue_background_job(self, payload: dict) -> str: ...
    def pop_user_job(self) -> dict | None: ...
    def pop_background_job(self) -> dict | None: ...
    def set_latest_price(self, symbol: str, price: float, ttl_seconds: int = 600): ...
    def get_latest_price(self, symbol: str) -> float | None: ...
    def acquire_lock(self, name: str, ttl_seconds: int) -> bool: ...
    def release_lock(self, name: str): ...
    def is_quiet(self, symbol: str, alert_type: str) -> bool: ...
    def set_quiet(self, symbol: str, alert_type: str, ttl_seconds: int): ...
    def dedupe_event(self, event_id: str, ttl_seconds: int = 3600) -> bool: ...
```

如果 Redis 不可用，必须 fallback SQLite，但 `/status` 要显示 degraded。

## 4. Parquet K 线归档

路径规范：

```text
data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet
```

字段规范：

```text
exchange
market_type
symbol
interval
open_time
open_time_utc
open
high
low
close
volume
close_time
close_time_utc
quote_volume
trade_count
taker_buy_base_volume
taker_buy_quote_volume
ingested_at_utc
```

归档规则：

1. 只归档已收盘 K 线。
2. 每批写入前按 `symbol + interval + open_time` 去重。
3. 若目标文件已存在，读取旧文件，合并去重后重写。
4. 写入完成后记录 `parquet_archive_runs`。
5. 失败不影响 SQLite 热数据，但必须记录错误。

## 5. DuckDB Analytics

DuckDB 数据库路径：

```text
D:/Program Files/duckdb/crypto_guard_analytics.duckdb
```

Codex 应实现：

```python
class DuckDBAnalytics:
    def health_check(self) -> dict: ...
    def query_klines(self, symbol: str, interval: str, start: str, end: str): ...
    def hourly_signal_distribution(self, start: str, end: str): ...
    def paper_account_summary(self, date_utc: str): ...
    def daily_review_stats(self, date_utc: str): ...
    def strategy_performance(self, strategy_name: str, days: int = 30): ...
```

DuckDB 查询 Parquet 示例：

```sql
SELECT symbol, interval, COUNT(*) AS n, MIN(open_time_utc), MAX(close_time_utc)
FROM read_parquet('data/parquet/klines/binance_um/BTCUSDT/15m/*.parquet')
GROUP BY symbol, interval;
```

## 6. /status 必须显示

```json
{
  "redis": {"status": "ok", "url": "redis://localhost:6379/0"},
  "sqlite": {"status": "ok"},
  "parquet": {"status": "ok", "last_write": "2026-05-26T06:00:00Z"},
  "duckdb": {"status": "ok", "database": "D:/Program Files/duckdb/crypto_guard_analytics.duckdb"}
}
```
