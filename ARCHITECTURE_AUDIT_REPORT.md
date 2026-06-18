# GA CryptoGuard Step 0 Architecture Audit Report

Audit time: 2026-05-26

Scope:

- Read the implementation plan in `ga_crypto_guard_codex_complete_plan/`.
- Scan current `plugins/crypto_guard` and Feishu frontend paths.
- Identify current final-decision paths that bypass the required GA Master Controller.
- No business-code refactor was performed in this step.

## 1. Executive Summary

Current repository has a working CryptoGuard prototype with:

- Feishu entry and button callbacks.
- SQLite-backed job queue, scheduler, signals, analysis states, paper trading, reviews, skill logs.
- Deterministic market preprocessing engines.
- A partial dynamic Skill scaffold under `plugins/crypto_guard/skills/*_skill`.
- LLM/GA calls in several places.

However, it does **not** yet conform to the complete architecture plan because:

1. `plugins/crypto_guard/ga_master/` does not exist.
2. There is no `ga_decisions` table.
3. Final analysis still persists through `signals.ga_decision_json`, not through first-class `ga_decisions`.
4. Feishu buttons are generated from `decision.suggested_actions`, not from `ga_decisions.feishu_actions_json`.
5. Paper orders are created from `signal_id`, not `ga_decision_id`.
6. Opportunity watches are created from `signal_id` or arbitrary tool input, not from `ga_decision_id + user button confirmation`.
7. Hourly reports read `signals` and `analysis_states` directly and expand per-symbol details.
8. Redis and DuckDB are not implemented. Parquet is only a path/read helper, not a project-managed archive writer.

This means the current system is still a rule/LLM hybrid worker around `signals`, not a GA Master Controller-led decision system.

## 2. Required Architecture Baseline

The target flow from the plan is:

```text
Feishu / Scheduler
  -> GA Master Controller
  -> Context Builder
  -> Skill Orchestrator
  -> SkillResult[]
  -> GA multi-timeframe reasoning
  -> Risk Gate
  -> GADecision
  -> Persistence / Feishu Actions / Paper Trading / Opportunity Watch
```

Hard rule:

```text
Any final trading judgment, opportunity-watch suggestion, paper-trading action,
Feishu button, and hourly report item must originate from GADecision.
```

## 3. Current Entry Points

### 3.1 Feishu Entry

Current Feishu entry is:

- `frontends/fsapp.py:811` `handle_message`
- `frontends/fsapp.py:834` imports `enqueue_feishu_message`
- `frontends/fsapp.py:836` enqueues CryptoGuard messages
- `frontends/fsapp.py:862` `handle_card_action`
- `frontends/fsapp.py:884` calls `enqueue_button_callback`
- `frontends/fsapp.py:905` registers message callback
- `frontends/fsapp.py:906` registers card action callback
- `frontends/fsapp.py:916` auto-starts CryptoGuard services

CryptoGuard Feishu queue integration:

- `plugins/crypto_guard/notify/feishu_integration.py:16` `enqueue_feishu_message`
- `plugins/crypto_guard/notify/feishu_integration.py:87` can directly run `run_once(user_only=True, send_message=send_message)` inline if services are not started

Assessment:

- Feishu messages are routed into the CryptoGuard queue.
- They are not routed into a GA Master Controller because no controller exists.

### 3.2 Scheduler Entry

Current scheduler entry:

- `plugins/crypto_guard/run_scheduler.py:18` `run_job`
- `plugins/crypto_guard/scheduler/cron_scheduler.py:71` `enqueue_market_analysis`
- `plugins/crypto_guard/scheduler/cron_scheduler.py:103` builds a market snapshot before enqueueing the analysis job
- `plugins/crypto_guard/scheduler/cron_scheduler.py:106` enqueues `scheduled_market_analysis`
- `plugins/crypto_guard/run_scheduler.py:69` queues `update_opportunity_watches`
- `plugins/crypto_guard/run_scheduler.py:92` queues `hourly_feishu_report`
- `plugins/crypto_guard/run_scheduler.py:130` queues `update_paper_positions_3m`

Assessment:

- Scheduler mostly queues work and records scheduler runs.
- But it currently builds snapshots outside GA Master Controller, and downstream workers produce final decisions without a controller.

## 4. Current Analysis Flow

### 4.1 User Ad Hoc Analysis

Path:

```text
Feishu text
  -> enqueue_feishu_message
  -> agent_jobs(feishu_user_message)
  -> run_ga_workers.process_job
  -> crypto_handle_text_command
  -> crypto_analyze_symbol_once
  -> build_market_state_snapshot
  -> run_agent_sop_decision
  -> repo.create_signal
  -> build_analysis_card_json
```

Key references:

- `plugins/crypto_guard/run_ga_workers.py:33` handles `feishu_user_message`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:85` `crypto_analyze_symbol_once`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:116` calls `run_agent_sop_decision`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:121` builds `analysis_state`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:125` calls `repo.create_signal`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:136` builds Feishu card

Bypass finding:

- Final analysis is produced by `run_agent_sop_decision`, then persisted as a `signal`.
- No `GAMasterController.analyze_symbol`.
- No first-class `ga_decisions` row.
- Feishu card is not based on `feishu_actions_json`.

### 4.2 Scheduled Analysis

Path:

```text
Scheduler
  -> scheduled_market_analysis job
  -> run_ga_workers.process_job
  -> run_agent_sop_decision
  -> build_market_analysis_state
  -> repo.create_signal
```

Key references:

- `plugins/crypto_guard/run_ga_workers.py:42` handles `scheduled_market_analysis`
- `plugins/crypto_guard/run_ga_workers.py:43` calls `run_agent_sop_decision`
- `plugins/crypto_guard/run_ga_workers.py:45` builds analysis state
- `plugins/crypto_guard/run_ga_workers.py:49` calls `repo.create_signal`

Bypass finding:

- Scheduled analysis final result is a signal, not a `GADecision`.
- Scheduler/worker path bypasses GA Master Controller.

## 5. Current Final Decision Producers

### 5.1 Rule-Based GA SOP Decision

Key references:

- `plugins/crypto_guard/reasoning/ga_judge.py:50` `run_ga_sop_decision`
- `plugins/crypto_guard/reasoning/ga_judge.py:57` builds trade plan when score/risk filters pass
- `plugins/crypto_guard/reasoning/ga_judge.py:59` sets `decision = "trade_plan_available"`
- `plugins/crypto_guard/reasoning/ga_judge.py:61` emits `create_paper_order` and `create_opportunity_watch`
- `plugins/crypto_guard/reasoning/ga_judge.py:65` emits `create_opportunity_watch`

Assessment:

- This function makes final decision, grade, trade plan, opportunity watch, and suggested actions.
- In the target architecture this logic should be evidence/reasoning inside GA Master Controller, not a standalone final-decision producer.

### 5.2 LLM/GA SOP Wrapper

Key references:

- `plugins/crypto_guard/reasoning/llm_agent_judge.py:21` `run_agent_sop_decision`
- It calls `run_ga_sop_decision` as fallback/reference.
- It validates against old `schemas/ga_decision.schema.json`.

Assessment:

- This is closer to an agent decision, but it is still not the specified GA Master Controller.
- It outputs the old decision shape with `suggested_actions`, not the new `GADecision` with `feishu_actions`, `skill_result_refs`, and persistence.

### 5.3 Strategy Scorer

Key references:

- `plugins/crypto_guard/strategy/strategy_scorer.py:7` `grade_from_score`
- `plugins/crypto_guard/strategy/strategy_scorer.py:17` `score_snapshot`
- `plugins/crypto_guard/strategy/strategy_scorer.py:80` returns `signal_grade`

Assessment:

- Strategy scoring currently computes grade and market bias. It does not directly create orders, but it feeds `run_ga_sop_decision`, which produces final actions.
- In the target design, this should remain evidence only and should not be treated as final grade/action authority outside GA Master Controller.

## 6. Current Persistence Model

### 6.1 Existing Tables

Relevant current tables:

- `analysis_states`
- `skill_execution_logs`
- `skill_feedback_memory`
- `signals`
- `ad_hoc_analyses`
- `opportunity_watches`
- `paper_orders`
- `paper_trades`
- `paper_positions`
- `paper_equity_snapshots`

Key references:

- `plugins/crypto_guard/storage/schema.sql:86` `analysis_states`
- `plugins/crypto_guard/storage/schema.sql:109` `skill_execution_logs`
- `plugins/crypto_guard/storage/schema.sql:125` `skill_feedback_memory`
- `plugins/crypto_guard/storage/schema.sql:140` `signals`
- `plugins/crypto_guard/storage/schema.sql:179` `opportunity_watches`
- `plugins/crypto_guard/storage/schema.sql:212` `paper_orders`

### 6.2 Missing Tables / Columns

Missing required model:

- `ga_decisions` table is absent.
- `paper_orders.ga_decision_id` is absent.
- `paper_orders.source = 'ga_decision'` is absent.
- `paper_orders.risk_check_passed` is absent.
- `opportunity_watches.ga_decision_id` is absent.
- `opportunity_watches.created_by_user_action` is absent.
- `opportunity_watches.source_button_action` is absent.
- `parquet_archive_runs` is absent.

Current persistence still stores old decision JSON in:

- `plugins/crypto_guard/storage/repository.py:365` `signals.ga_decision_json`

Bypass finding:

- The system treats `signals` as the durable final decision object. This conflicts with the required `ga_decisions` source of truth.

## 7. Current Feishu Button Flow

Current button generation:

- `plugins/crypto_guard/notify/feishu_cards.py:97` defines `create_paper_order`
- `plugins/crypto_guard/notify/feishu_cards.py:98` defines `create_opportunity_watch`
- `plugins/crypto_guard/notify/feishu_cards.py:102` checks `decision.get("suggested_actions")`

Current button callback handling:

- `plugins/crypto_guard/run_ga_workers.py:142` if action is `create_paper_order`
- `plugins/crypto_guard/run_ga_workers.py:143` calls `create_paper_order_from_signal`
- `plugins/crypto_guard/run_ga_workers.py:146` if action is `create_opportunity_watch`
- `plugins/crypto_guard/run_ga_workers.py:157` creates opportunity watch from signal

Bypass finding:

- Buttons are not generated from `ga_decisions.feishu_actions_json`.
- Button callbacks reference `signal_id`, not `ga_decision_id`.
- Opportunity watch creation does not verify `signal_grade in B/A/S` at the repository boundary.

## 8. Current Paper Trading Flow

Current paper order creation:

- `plugins/crypto_guard/paper/paper_broker.py:11` `create_paper_order_from_signal`
- `plugins/crypto_guard/paper/paper_broker.py:30` validates trade plan with current risk engine
- `plugins/crypto_guard/paper/paper_broker.py:39` calls `repo.create_paper_order`
- `plugins/crypto_guard/storage/repository.py:699` `create_paper_order`
- `plugins/crypto_guard/storage/repository.py:703` inserts `paper_orders(signal_id, ...)`

Manual Feishu intent path:

- `plugins/crypto_guard/tools/ga_crypto_tools.py:349` detects `create_paper_order`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:350` runs ad hoc analysis
- `plugins/crypto_guard/tools/ga_crypto_tools.py:353` creates paper order from `signal_id`

Assessment:

- Risk validation exists and is useful.
- It still uses `signal_id`, not `ga_decision_id`.
- Repository layer does not require a GA decision reference.
- The target rule "only GADecision can create paper_order" is not enforced.

## 9. Current Opportunity Watch Flow

Current creation paths:

- `plugins/crypto_guard/tools/ga_crypto_tools.py:143` `crypto_create_opportunity_watch`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:149` directly calls `repo.create_opportunity_watch`
- `plugins/crypto_guard/run_ga_workers.py:146` button path loads watch from `signals`
- `plugins/crypto_guard/run_ga_workers.py:157` creates watch from `source_signal_id`
- `plugins/crypto_guard/storage/repository.py:427` `create_opportunity_watch`
- `plugins/crypto_guard/storage/repository.py:438` inserts `opportunity_watches`

Current watch trigger path:

- `plugins/crypto_guard/scheduler/opportunity_watcher.py:15` `update_opportunity_watches`
- `plugins/crypto_guard/scheduler/opportunity_watcher.py:35` marks triggered
- `plugins/crypto_guard/scheduler/opportunity_watcher.py:36` enqueues alert

Bypass finding:

- Opportunity watch can be created from tool input and signal JSON.
- No enforced `ga_decision_id`.
- No enforced `created_by_user_action = 1`.
- No repository-level guard preventing D/C watches.

## 10. Current Hourly Report Flow

Current report:

- `plugins/crypto_guard/notify/hourly_report.py:24` `build_hourly_report`
- `plugins/crypto_guard/notify/hourly_report.py:27` reads `latest_signals_by_symbol`
- `plugins/crypto_guard/notify/hourly_report.py:28` reads `latest_analysis_states`
- `plugins/crypto_guard/notify/hourly_report.py:78` starts product analysis overview
- `plugins/crypto_guard/notify/hourly_report.py:89` renders each symbol via `_signal_report_lines`
- `plugins/crypto_guard/notify/hourly_report.py:191` `_signal_report_lines`
- `plugins/crypto_guard/notify/hourly_report.py:343` `_analysis_state_report_lines`

Bypass finding:

- Hourly report uses `signals` and `analysis_states`, not `ga_decisions`.
- It is still per-symbol detailed output, not the required management summary.
- It includes GA/LLM brief wording and not the target "dynamic Skill tools + GA Master Controller" provenance.

## 11. Current Skill State

Existing skill directories:

- `plugins/crypto_guard/skills/price_action_skill`
- `plugins/crypto_guard/skills/momentum_skill`
- `plugins/crypto_guard/skills/trend_stage_skill`
- `plugins/crypto_guard/skills/smc_orderflow_skill`
- `plugins/crypto_guard/skills/chanlun_skill`

Required names:

- `plugins/crypto_guard/skills/price_action`
- `plugins/crypto_guard/skills/momentum`
- `plugins/crypto_guard/skills/trend_stage`
- `plugins/crypto_guard/skills/smc_orderflow`
- `plugins/crypto_guard/skills/chanlun`

Current runner:

- `plugins/crypto_guard/skills/runner.py:17` `execute_market_skills`
- `plugins/crypto_guard/skills/runner.py:35` hard-coded price action execution
- `plugins/crypto_guard/skills/runner.py:46` hard-coded SMC execution
- `plugins/crypto_guard/skills/runner.py:47` hard-coded order-flow execution
- `plugins/crypto_guard/skills/runner.py:48` hard-coded chanlun execution
- `plugins/crypto_guard/skills/runner.py:77` writes `skill_execution_logs`

Assessment:

- Skill logs exist.
- Deterministic tools exist.
- But the runner does not load `skill.yaml`, `prompt.md`, `schema.json`, or `feedback_rules.yaml` as a dynamic skill contract.
- The `ga_interpretation` is a static dictionary, not a GA interpretation based on prompt + tool result + memory.
- Tool outputs are mostly facts, which is a useful starting point.

## 12. Redis / Parquet / DuckDB Audit

### Redis

Findings:

- No `RedisAdapter`.
- No `import redis`.
- No Redis queue keys such as `queue:user:feishu`.
- No Redis latest price cache.
- No Redis Feishu event dedupe.
- No Redis quiet-period keys.
- Current queues, dedupe, and locks are SQLite/in-memory:
  - `agent_jobs`
  - `feishu_events`
  - `task_locks`
  - process-local `_SEEN_MESSAGES`

Status:

- Not implemented. Requires SQLite fallback preservation.

### Parquet

Existing file:

- `plugins/crypto_guard/storage/parquet_archive.py`

Current behavior:

- `planned_archive_path` only returns a path.
- `archive_status` explicitly says `path_contract_only`.
- `read_klines_file` can read Parquet/JSON/CSV for replay.

Missing:

- No `ParquetKlineArchive`.
- No project-managed monthly write path.
- No merge/dedupe writer.
- No `parquet_archive_runs`.
- No `/status` last-write time.

### DuckDB

Findings:

- No `DuckDBAnalytics`.
- No `import duckdb`.
- No database file integration at `D:/Program Files/duckdb/crypto_guard_analytics.duckdb`.
- No DuckDB-backed report or review query.

Status:

- Not implemented.

## 13. Safety Audit

Positive findings:

- `plugins/crypto_guard/data/binance_rest.py` only uses public GET endpoints.
- No `fapi/v1/order` or signed order path was found in CryptoGuard code.
- `plugins/crypto_guard/config/loader.py` rejects `live_trading_enabled`, `allow_trade_api`, `allow_withdraw_api`, and `real_order_api_enabled`.

Residual concern:

- Tool/function names like `crypto_create_paper_order_from_signal` are explicitly paper-only, but the architecture must still move them behind `GADecision` and `RiskGate`.

## 14. Bypass Paths That Must Be Refactored

### Bypass 1: Ad hoc analysis creates final signal directly

Current:

```text
crypto_analyze_symbol_once -> run_agent_sop_decision -> repo.create_signal
```

Files:

- `plugins/crypto_guard/tools/ga_crypto_tools.py:116`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:125`

Required:

```text
crypto_analyze_symbol_once -> GAMasterController.analyze_symbol -> save ga_decision
```

### Bypass 2: Scheduled analysis creates final signal directly

Current:

```text
scheduled_market_analysis -> run_agent_sop_decision -> repo.create_signal
```

Files:

- `plugins/crypto_guard/run_ga_workers.py:42`
- `plugins/crypto_guard/run_ga_workers.py:49`

Required:

```text
scheduled_market_analysis -> GAMasterController.analyze_symbol -> save ga_decision
```

### Bypass 3: Buttons come from suggested_actions, not feishu_actions_json

Current:

```text
decision.suggested_actions -> Feishu buttons
```

Files:

- `plugins/crypto_guard/notify/feishu_cards.py:97`
- `plugins/crypto_guard/notify/feishu_cards.py:102`

Required:

```text
ga_decisions.feishu_actions_json -> Feishu buttons
```

### Bypass 4: Paper orders are created from signal_id

Current:

```text
create_paper_order_from_signal(signal_id) -> repo.create_paper_order(signal_id, ...)
```

Files:

- `plugins/crypto_guard/paper/paper_broker.py:11`
- `plugins/crypto_guard/paper/paper_broker.py:39`
- `plugins/crypto_guard/storage/repository.py:699`

Required:

```text
create_paper_order_from_ga_decision(ga_decision_id) -> RiskGate -> paper_orders.ga_decision_id
```

### Bypass 5: Opportunity watches are created from signal/tool input

Current:

```text
crypto_create_opportunity_watch(symbol, watch_condition, signal_id)
button_callback(signal_id) -> repo.create_opportunity_watch(...)
```

Files:

- `plugins/crypto_guard/tools/ga_crypto_tools.py:143`
- `plugins/crypto_guard/tools/ga_crypto_tools.py:149`
- `plugins/crypto_guard/run_ga_workers.py:146`
- `plugins/crypto_guard/run_ga_workers.py:157`
- `plugins/crypto_guard/storage/repository.py:427`

Required:

```text
button_callback(ga_decision_id) -> verify feishu_actions_json -> create opportunity_watch with created_by_user_action=1
```

### Bypass 6: D/no-edge state can still be marked opportunity-watch recommended

Current:

```python
opportunity_watch_recommended = bool("create_opportunity_watch" in suggested_actions or not paper_allowed)
```

File:

- `plugins/crypto_guard/reasoning/analysis_state.py:61`

Impact:

- For D/no_edge without paper permission, this can mark opportunity watch recommended even when no edge exists.

Required:

- D/C must not recommend opportunity watch by default.
- Opportunity watch recommendation must come from `GADecision.feishu_actions_json` and grade rules.

### Bypass 7: Hourly report reads signals/analysis_states directly

Current:

```text
build_hourly_report -> latest_signals_by_symbol + latest_analysis_states -> per-symbol long output
```

Files:

- `plugins/crypto_guard/notify/hourly_report.py:24`
- `plugins/crypto_guard/notify/hourly_report.py:27`
- `plugins/crypto_guard/notify/hourly_report.py:28`
- `plugins/crypto_guard/notify/hourly_report.py:89`

Required:

```text
report_adapter -> ga_decisions summary + DuckDB/SQLite stats
```

## 15. Step 0 Acceptance Checklist

- [x] Feishu entry file identified.
- [x] Scheduler entry identified.
- [x] Current analysis flow entry identified.
- [x] Current strategy/signal generation positions identified.
- [x] Current paper order creation positions identified.
- [x] Current database access layer identified.
- [x] Redis implementation status checked.
- [x] DuckDB implementation status checked.
- [x] Parquet implementation status checked.
- [x] GA bypass final decision paths identified.

## 16. Recommended Step 1 Refactor Boundaries

Do not delete the working system in one sweep. Use adapter migration:

1. Add `plugins/crypto_guard/ga_master/` modules.
2. Add `ga_decisions` table and persistence APIs.
3. Make `GAMasterController.analyze_symbol` initially wrap current snapshot + skill + LLM/rule logic.
4. Persist every final result as `ga_decisions`.
5. Keep `signals` temporarily as compatibility/read-model only, but stop treating it as source of truth.
6. Generate Feishu actions via `feishu_action_builder.py` and persist to `ga_decisions.feishu_actions_json`.
7. Change button callbacks to use `ga_decision_id`.
8. Add repository-level guards for paper orders and opportunity watches.

This keeps behavior testable while moving authority from `signals` to `GADecision`.
