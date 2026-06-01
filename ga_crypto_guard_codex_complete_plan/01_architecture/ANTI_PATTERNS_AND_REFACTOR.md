# 反模式清单与重构要求

## 反模式 1：LLM 只做文案总结

错误：

```text
各模块先输出结论，然后 LLM 把结论写成一段话。
```

正确：

```text
GA Master Controller 读取 SkillResult，并由 GA 输出 GADecision。
```

## 反模式 2：策略模块直接决定交易

错误：

```text
strategy_score >= 0.72 -> create_paper_order
```

正确：

```text
strategy_score 只是 evidence，必须进入 GA 综合和风控检查。
```

## 反模式 3：D 级信号建议机会监控

错误：

```text
signal_grade=D, decision=no_edge, opportunity_watch_recommended=true
```

正确：

```text
D 级只记录，不建议机会监控。
```

## 反模式 4：每小时播报输出完整分析状态

错误：

```text
每小时逐币输出模块明细、完整反向证据、所有关键点位。
```

正确：

```text
每小时只输出管理摘要；详细分析仅在用户主动请求时展开。
```

## 反模式 5：Redis / Parquet / DuckDB 只安装不使用

错误：

```text
requirements.txt 有 duckdb/redis/pyarrow，但主流程不读写。
```

正确：

```text
Redis 管队列/缓存/锁；Parquet 写长期 K 线；DuckDB 查 Parquet 和做报表聚合。
```
