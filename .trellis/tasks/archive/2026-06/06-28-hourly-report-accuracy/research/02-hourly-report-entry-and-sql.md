# 02 — hourly_report 入口与决策 SQL

- **Query**: hourly_report 在哪触发？整点后 1 分钟怎么来？取决策的 SQL 是什么？为什么能取到上一轮？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 触发链：`run_scheduler.run_job("hourly_feishu_report")` (`run_scheduler.py:82-100`) → `repo.enqueue_job_once("hourly_feishu_report", 3, "scheduler", session_id, {"scheduled_time": scheduled_time})` → `agent_jobs` pending 行被 worker 拉。Worker 处理在 `run_ga_workers.py:216-227`：`report = build_hourly_report(repo)` 然后 `send_markdown_alert(text=report["text"])`。
- "整点后 1 分钟"：`scheduler.yaml:49` cron `"1-10 * * * *"`；`service_manager._due_scheduler_jobs:123` `if minute in {1..10}: jobs.append("hourly_feishu_report")`；通过 `scheduler_runs UNIQUE(job_name, scheduled_time)` + `scheduler_success_exists` 保证只成功一次。
- 取决策核心：`build_hourly_report` (`hourly_report.py:27-87`) line 40 调 `repo.latest_ga_decisions_by_symbol(limit=120)`。
- SQL（`repository.py:312-333`）：
  ```sql
  SELECT gd.* FROM ga_decisions gd JOIN (
    SELECT symbol, MAX(analysis_time) AS max_time
    FROM ga_decisions WHERE analysis_time >= ?  -- 可选 min
    GROUP BY symbol
  ) latest ON latest.symbol=gd.symbol AND latest.max_time=gd.analysis_time
  ORDER BY gd.analysis_time DESC, gd.id DESC LIMIT ?
  ```
- 没有传入 `min_analysis_time`（line 40 调用未传第二参数），所以"取每个 symbol 最新一条决策"——不区分本轮或上一轮。

## Why broken

- 06:59:59Z 的 15m 分析决策在 07:03-07:08Z 才落库；07:01Z 的 hourly_report 一旦被 worker 拉走，`MAX(analysis_time)` 就指向 06:44:59Z 的上一轮决策。
- `load_config().scheduler.yaml` cron 写"1-10"，但 worker drain 没有等待 analyze_market_15m 作业清空，存在 race。
- 报告"具备模拟盘做多条件"等语句直接来自 stale 决策的 final_summary。

## Where to fix
- `plugins/crypto_guard/notify/hourly_report.py:40` — 改为 `repo.latest_ga_decisions_by_symbol(limit=120, min_analysis_time=latest_closed_close_time_ms("15m", now))`，强制取本轮 15m close_time。
- 或新增 `repo.latest_ga_decisions_for_batch(batch_id)`，按 batch_id 取决策。
- `plugins/crypto_guard/storage/repository.py:312` — 增加 batch filter 或 status='success' 的关联查询。

## Tests to add
- 7:01Z batch_id=15m:06:45 的决策未到时不渲染任何行
- 上一轮 stale 决策能被 min_analysis_time 过滤掉
- `build_hourly_report` 当 ga_decisions 为空时返回 ok 但 text 说明"本轮尚未分析"