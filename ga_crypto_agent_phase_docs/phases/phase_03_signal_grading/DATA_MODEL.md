# Phase 03 数据模型

## 相关表

本阶段可能涉及以下表，完整 SQL 见 `global/schema.sql`：

- scheduler_runs
- agent_jobs
- task_locks
- symbols
- candles
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

## 迁移要求

- 所有新增字段必须有默认值或兼容旧数据。
- 迁移脚本必须可重复执行。
- 建议使用 `CREATE TABLE IF NOT EXISTS` 与 `CREATE INDEX IF NOT EXISTS`。
- 结构化 JSON 字段必须能被后续复盘读取。

## 数据质量要求

- analysis_time 必须为 UTC timestamp。
- K 线使用 close_time <= analysis_time。
- signal 必须能追溯到 snapshot。
- trade 必须能追溯到 signal。
