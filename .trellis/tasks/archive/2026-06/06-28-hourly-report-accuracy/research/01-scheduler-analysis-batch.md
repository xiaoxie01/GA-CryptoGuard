# 01 — Scheduler / Analysis Batch 身份

- **Query**: scheduler 是谁？分析批次 identity 如何决定？有无 batch_id/run_id？job 如何入排队？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 调度入口 `plugins/crypto_guard/run_scheduler.py:18` `run_job(job_name)`：每种 job 显式 if-else；`hourly_feishu_report` 在 82-100 行，先 `latest_closed_close_time_ms("1h", now)` 拿 scheduled_time，然后调 `repo.enqueue_job_once("hourly_feishu_report", 3, "scheduler", f"system:scheduled:hourly_report:{scheduled_time}", {...})`。
- `cron` 表在 `plugins/crypto_guard/config/scheduler.yaml:49-55`，写的是 `cron: "1-10 * * * *"`，每分钟都触发；`service_manager._due_scheduler_jobs` (`service_manager.py:119-157`) 在 minute ∈ {1..10} 每 20 秒 tick 中 attempt 一次，靠 `scheduler_runs(job_name, scheduled_time) UNIQUE` + `scheduler_success_exists` (`repository.py:943`) 做幂等，所以实际只在第 1 分钟成功一次。
- 分析批次的 identity：`plugins/crypto_guard/scheduler/cron_scheduler.py:87-112` `enqueue_market_analysis`。session_id 形如 `system:scheduled:{primary_interval}:{symbol}:{analysis_time}`（line 88），每个 symbol 一条 `agent_jobs` 行；**没有顶层 batch_id/run_id 跨 symbol 聚合**。
- `agent_jobs` schema（`schema.sql:510-525`）字段仅 `job_type, priority, source, session_id, payload_json, status, scheduled_at, started_at, finished_at, error_message, result_json`；status 取值 pending/running/success/failed/cancelled/duplicate。
- scheduler 自身在 `scheduler_runs` 表 (`schema.sql:497-508`) 跟踪：`job_name, scheduled_time, started_at, finished_at, status, error_message, result_json`，UNIQUE(job_name, scheduled_time)；`create_scheduler_run` / `finish_scheduler_run` (`repository.py:950-971`)。
- 15m 分析入队：`run_scheduler.py:51-55` `analyze_market_15m` → `enqueue_15m_analysis` → `enqueue_market_analysis(primary_interval="15m")` (`cron_scheduler.py:126`)。

## Why broken

- hourly_report 用 `scheduled_time = latest_closed_close_time_ms("1h", now)`（整点），而 15m 分析批次用 `analysis_time = latest_closed_close_time_ms("15m", now)`，两个时间基准不同。Reports cron 与 analyze_market_15m cron 都在 minute=1 启动，但报告 worker 可能先抢到 lock（不同 lock_name），导致 report 取到的是 15m 分析尚未写入的上一轮决策。
- 报告与一批 15m 分析之间没有共享 batch_id，hourly_report 无法判断"本小时这一轮 analyze 是否全部 finished"。

## Where to fix
- `plugins/crypto_guard/config/scheduler.yaml:49` — 改 cron 到 `10 * * * *`（或等到 minute=10 让 15m 分析批次完成）。
- `plugins/crypto_guard/notify/hourly_report.py:build_hourly_report` — 取决策时下推 `min_analysis_time = latest_closed_close_time_ms("15m", now)` 同时检查 `agent_jobs WHERE job_type='scheduled_market_analysis' AND status IN ('pending','running')` 是否为 0，否则延迟或取下一批。
- `plugins/crypto_guard/scheduler/cron_scheduler.py:enqueue_market_analysis` — 在 payload 中写入 `batch_id`（例如 `f"15m:{analysis_time}"`），并由 `ga_decisions` 引用，便于 report 校验。

## Tests to add
- hourly_report 在 15m 批次仍有 running 时不渲染该轮决策
- 两次 minute=1 tick 中第二次 `scheduler_runs` 命中 UNIQUE 不重复入队
- 同 batch_id 内所有 symbol 完成后 report 才采信