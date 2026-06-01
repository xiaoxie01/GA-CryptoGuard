# Parquet 自主管理规范

## 1. 为什么必须实现

Parquet 不是装一个库就完成。Codex 必须把它接入数据链路：

```text
Binance closed candles
  ↓
SQLite 热数据
  ↓
Parquet 长期归档
  ↓
DuckDB 查询与统计
```

## 2. 写入策略

推荐实现：按月文件合并重写。

伪代码：

```python
def archive_klines(df, symbol, interval):
    df = normalize_schema(df)
    df = df[df["is_closed"] == True]
    df["yyyy_mm"] = df["open_time_utc"].str.slice(0, 7)

    for yyyy_mm, batch in df.groupby("yyyy_mm"):
        path = base / symbol / interval / f"{yyyy_mm}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            merged = pd.concat([old, batch], ignore_index=True)
        else:
            merged = batch

        merged = merged.drop_duplicates(["symbol", "interval", "open_time"])
        merged = merged.sort_values("open_time")
        merged.to_parquet(path, engine="pyarrow", compression="snappy", index=False)
```

## 3. 归档运行记录

```sql
CREATE TABLE IF NOT EXISTS parquet_archive_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    file_path TEXT NOT NULL,
    rows_written INTEGER DEFAULT 0,
    min_open_time INTEGER,
    max_open_time INTEGER,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

## 4. 验收

1. 运行一次 15m K 线任务后，本地出现 Parquet 文件。
2. 重复运行不会产生重复 K 线。
3. DuckDB 可以读取该文件。
4. `/status` 显示最近写入时间。
