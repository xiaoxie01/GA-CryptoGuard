# 03 — 决策表 schema

- **Query**: ga_decisions / paper_decisions 表 schema，analysis_time、signal_grade、confidence、risk_check_json、trade_plan_json、decision、market_bias、independent_trend 字段
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 表定义 `plugins/crypto_guard/storage/schema.sql:145-173` `ga_decisions`：
  - `id INTEGER PK`
  - `symbol, analysis_time INTEGER (ms), analysis_time_utc TEXT`
  - `decision_type TEXT` (scheduled_analysis / ...)
  - `signal_grade TEXT (S/A/B/C/D)`
  - `confidence REAL`
  - `market_bias TEXT, trend_stage TEXT`
  - `decision TEXT` (create_paper_order / trade_plan_available / wait_for_pullback / opportunity_watch / monitor_only / no_edge / close_position / adjust_stop_loss / hold_position)
  - `skill_result_refs_json, evidence_json, counter_evidence_json, risk_check_json (NOT NULL), trade_plan_json (NULLABLE), opportunity_watch_json, feishu_actions_json`
  - `final_summary TEXT NOT NULL`
  - `raw_decision_json TEXT NOT NULL`
  - `analysis_state_id, snapshot_id`
  - `account_feedback_gate_json, market_regime_gate_json`（migrations.py 加）
  - `created_by DEFAULT 'ga_master_controller'`
  - `created_at TEXT DEFAULT CURRENT_TIMESTAMP`
  - 索引：`idx_ga_decisions_symbol_time (symbol, analysis_time)`；`idx_ga_decisions_grade_time (signal_grade, analysis_time)`
- 写入：`repository.create_ga_decision` (`repository.py:250-288`) — trade_plan 为 None 时 trade_plan_json 写 NULL，risk_check_json 始终写入（`{}` 也写）。
- 相关表 `signals` (`schema.sql:175-198`)：兼容读模型，由 `legacy_decision_from_ga_decision` (`ga_master/decision_schema.py:39`) 转写。`ga_decision_id` 外键指向 `ga_decisions.id`。
- `analysis_states` 表保存 trend_stage / market_structure / trade_permission；`market_snapshots` 保存 K 线与模块输出。
- "independent_trend" 不是表字段，是 `market_regime_gate_json` 内的 regime_alignment 取值之一（`market_regime_engine.py:516`）。

## Why broken

- `risk_check_json` 与 `trade_plan_json`、`final_summary` 之间没有 DB level 一致性约束；LLM 输出可写 `risk_check.ok=false` 同时 `final_summary` 写"风控全部满足"，持久化路径不校验。
- 没有 `previous_grade` 列，rating 稳定性检查无法实现。
- 没有 `batch_id` 列，report 无法与 batch 对齐。

## Where to fix
- `plugins/crypto_guard/storage/migrations.py` + `storage/schema.sql:145` — 增加 `previous_grade TEXT`、`batch_id TEXT`、`rendered_summary TEXT`（deterministic-validator 输出）可选。
- `repository.create_ga_decision:253-287` — 写入新列；并在 LongString 字段上做 risk_check.ok == has_trade_plan 一致性断言。

## Tests to add
- 风控 ok=false 时拒绝写入 has_trade_plan=true 的决策
- previous_grade 落库后能从历史决策读取
- migrate 现有库 + new column 默认值