# 00 — 总结

- **Task**: hourly-report-accuracy 修复（行情准确性 / 结构化与文本措辞 / 评级稳定性）
- **Date**: 2026-06-28
- **Scope**: internal (crypto_guard)

## TL;DR 根因表

| 编号 | 根因 | 现状定位 | 优先级 |
|---|---|---|---|
| 1 | hourly_report 入队时机与 15m 分析批次无同步关系，取决策按 `MAX(analysis_time)` 取最近一条，能用上一轮 stale 决策 | `run_scheduler.py:82-100` (cron 1-10 min)、`hourly_report.py:40`、`repository.py:312` `latest_ga_decisions_by_symbol` 按 symbol 取 max(analysis_time) | P0 |
| 2 | 报表"高等级机会（S/A/B）"只判 grade，不校验 risk_check.ok / trade_plan / min_confidence | `hourly_report.py:108-201` `render_ga_hourly_summary` | P0 |
| 3 | LLM 生成的 `final_summary` 直接渲染进报表，没有 deterministic validator 校验与结构化字段一致 | `llm_agent_judge.py:295-350` `_normalize_llm_decision`、`controller.py:461` 持久化、`hourly_report.py:200` 渲染 `final_summary` | P0 |
| 4 | 评级由 `score -> grade_from_score` 单点计算，无迟滞/无 previous_grade 记录 | `grade_config.py:33`、`strategy_scorer.py:175`、`ga_judge.py:201` | P1 |
| 5 | drawdown 渲染直接取 `snap['drawdown_percent']`，符号约定未统一（`execution_quality.py:152` 强制 <=0，但 `account_risk_guard._drawdown_percent` 正负不一） | `hourly_report.py:147,825,388`、`execution_quality.py:152`、`account_risk_guard.py:340` | P1 |
| 6 | `in_memory_fallback` 真实语义是 DuckDB 不可用时回退到内存 grade_counts，但措辞误导：被当成"假数据/不可信" | `hourly_report.py:212-213,375` | P2 |
| 7 | liquidity sweep 语义实际正确（sweep_low=sell_side_liquidity_sweep→bullish），但变量名/日志易误解 | `smc_engine.py:13-22`、`counter_evidence_engine.py:35-37`、`strategy_scorer.py:119` | P2 |
| 8 | 行情价全部来自 Binance USDⓈ-M 期货（`fapi.binance.com`），无 Yahoo/CoinGecko 备选；close_time 已记录在 K 线 | `data/binance_rest.py:15,108`、`candle_store.py` | P2 (非 bug) |
| 9 | 没有给每个决策渲染 analysis_time / age / risk_check / trade_plan / 可执行状态 | `hourly_report.py:198-201` 仅显示符号+grade+decision+summary | P1 |
| 10 | min_confidence（0.72）只在 `risk_engine` 写入决策时生效；`hourly_report` 渲染不再校验，无法过滤 < 0.72 的高等级机会 | `grade_config.MIN_CONFIDENCE_FOR_PAPER_ORDER`、`risk_engine.py:34`、`hourly_report.py:113` | P0 |
| 11 | scheduler 用 `scheduler_runs(job_name, scheduled_time)` UNIQUE 做幂等；分析批次 identity 是 `system:scheduled:{primary_interval}:{symbol}:{analysis_time}` 字符串，无独立 batch_id/run_id 字段链接到 hourly_report | `cron_scheduler.py:88`、`schema.sql:497-525` | P1 |

## 必须触达的修改文件清单

### P0（行情时效 + 文/字段一致性）
- `plugins/crypto_guard/notify/hourly_report.py` — `build_hourly_report` 限定决策时间窗 / `render_ga_hourly_summary` `_decision_row` 加 risk_check·trade_plan·min_confidence 校验、加 analysis_time·age 列、改"高等级机会"过滤
- `plugins/crypto_guard/reasoning/llm_agent_judge.py` — `_normalize_llm_decision` 加 consistency validator：不允许 `final_summary` 与 `decision/risk_check/trade_plan` 矛盾
- `plugins/crypto_guard/ga_master/controller.py` — 持久化前再次断言 risk_check 与 has_trade_plan 一致
- `plugins/crypto_guard/config/scheduler.yaml` + `plugins/crypto_guard/service_manager.py:_due_scheduler_jobs` — 把 `hourly_feishu_report` 从整点后 1 分钟移到 "上一轮 analyze_market_15m 完成" 之后；或 build_hourly_report 在取决策时加 `min_analysis_time = 上一个 15m close_time` 并等待队列空

### P1（评级稳定性 + drawdown 符号 + 报表诊断列）
- `plugins/crypto_guard/strategy/grade_config.py` — 增加 grade hysteresis（previous_grade 字段）
- `plugins/crypto_guard/storage/schema.sql` + `migrations.py` — `ga_decisions` 增 `previous_grade` 列
- `plugins/crypto_guard/ga_master/controller.py` / `context_builder.py` — 读取上一条决策 grade 写入 previous_grade
- `plugins/crypto_guard/risk/account_risk_guard.py:_drawdown_percent` / `paper/execution_quality.py:152` — 统一 drawdown 符号（负值表示亏损）并让 hourly_report 显示明确 sign
- `plugins/crypto_guard/notify/hourly_report.py:147,825` — 显示时区分 "回撤 -0.50%" 而非 "-0.50%"

### P2（语义澄清，非阻断）
- `plugins/crypto_guard/notify/hourly_report.py:212-213` — `in_memory_fallback` 改为 "SQLite 实时等级统计（DuckDB 未启用）"
- `plugins/crypto_guard/analysis/smc_engine.py:14-19` — 注释说明 sell_side_sweep 含义；加单测锁定方向语义
- `plugins/crypto_guard/data/binance_rest.py` — 文档化 supplier=Binance USDⓈ-M；非缺陷

## 子研究文件索引
- `01-scheduler-analysis-batch.md`
- `02-hourly-report-entry-and-sql.md`
- `03-decision-schema.md`
- `04-opportunity-classification.md`
- `05-llm-summary-vs-structured.md`
- `06-market-data-source.md`
- `07-liquidity-sweep-semantics.md`
- `08-drawdown-render.md`
- `09-in-memory-fallback.md`
- `10-rating-stability-gate.md`
- `11-sa-rating-gate.md`
- `12-execution-gate.md`