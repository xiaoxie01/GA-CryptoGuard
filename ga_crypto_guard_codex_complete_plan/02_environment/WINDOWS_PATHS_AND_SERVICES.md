# Windows 环境与路径配置

## 1. 用户指定路径

```yaml
windows_paths:
  redis_install_dir: "D:/Program Files/Redis"
  duckdb_dir: "D:/Program Files/duckdb"
  duckdb_database: "D:/Program Files/duckdb/crypto_guard_analytics.duckdb"
  parquet_base_dir: "data/parquet/klines/binance_um"
```

注意：Python 配置中建议使用 `/`，避免反斜杠转义问题。

## 2. Redis

Redis 目录：`D:\Program Files\Redis`

Codex 应支持两种启动方式：

1. 用户已手动启动 Redis 服务。
2. 项目脚本尝试启动 `redis-server.exe`。

Redis URL：

```yaml
redis:
  enabled: true
  url: "redis://localhost:6379/0"
  install_dir: "D:/Program Files/Redis"
  fallback_to_sqlite: true
```

如果连接失败：

- 不要让系统崩溃。
- `/status` 显示 `redis: degraded`。
- 用户消息队列和后台队列 fallback 到 SQLite。
- 静默期和锁 fallback 到 SQLite。

## 3. DuckDB

DuckDB 是嵌入式数据库，Python 库会直接打开文件。

数据库文件：

```text
D:/Program Files/duckdb/crypto_guard_analytics.duckdb
```

Codex 必须确保目录存在：

```python
Path("D:/Program Files/duckdb").mkdir(parents=True, exist_ok=True)
```

## 4. Parquet

Parquet 由项目自行处理，不依赖用户预先创建目录。

默认目录：

```text
data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet
```

Codex 必须实现：

- 自动创建目录
- 自动按月分区
- 增量写入或安全重写
- 去重 open_time / close_time
- status 记录最近写入时间

## 5. 推荐依赖

```text
redis
pandas
numpy
pyarrow
duckdb
pydantic
pyyaml
```

如果已有依赖管理文件，Codex 应追加，而不是覆盖。
