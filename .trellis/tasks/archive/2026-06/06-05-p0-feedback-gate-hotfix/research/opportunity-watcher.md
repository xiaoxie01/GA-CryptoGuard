# Research: Opportunity Watcher Mechanics

- **Query**: Understand watch condition types, processing loop, LLM usage, statuses, expiry handling, and table schema
- **Scope**: internal
- **Date**: 2025-06-05

## Findings

### Files Found

| File Path | Description |
|---|---|
| `plugins/crypto_guard/scheduler/opportunity_watcher.py` | Main watcher logic (254 lines) |
| `plugins/crypto_guard/storage/schema.sql` | Table DDL for opportunity_watches |
| `plugins/crypto_guard/storage/repository.py` | Repository methods for opportunity_watches |
| `plugins/crypto_guard/storage/migrations.py` | Migration additions to opportunity_watches |

### Valid Watch Condition Types

Defined in `opportunity_watcher.py` lines 135-177, function `_condition_hit()`:

| Type | Description | Key Parameters |
|---|---|---|
| `price_below` / `close_below` | Close price falls below level | `level` (price) |
| `price_above` / `close_above` | Close price rises above level | `level` (price) |
| `pullback` | Price dips to level then recovers (LONG) or spikes then falls (SHORT) | `level`, `tolerance_pct` (default 0.003) |
| `breakout` | Close breaks through level in direction of side | `level` |
| `reclaim` | Crosses back above/below level from the opposite side (uses previous candle) | `level` |
| `cvd_confirmation` | CVD/order-flow confirmation matches expected direction | `flow_confirmation` or `value` (string) |
| `manual_review` | Gate-downgraded watch (not auto-triggering) -- set by `_create_opportunity_watch_from_gate()` in paper_broker.py line 447 | `type: "manual_review"` |

Note: String conditions return `{"hit": False, "reason": "文本条件等待人工或后续结构化确认：..."}` (line 137). Unknown dict types return `{"hit": False, "reason": "未知或未满足条件：..."}` (line 177).

### Watcher Main Loop

Function `update_opportunity_watches()` at line 15:
1. Iterates over all active watches via `repo.list_active_opportunity_watches()` (line 20)
2. For each watch, calls `evaluate_watch()` (line 22)
3. Passes result through `_agent_review_watch_result()` (line 23) -- **this calls LLM on every watch iteration**
4. Based on status, updates DB: expired, invalidated, triggered, or just touches `last_checked_at`

### Does It Call LLM for Every Watch? YES

Line 23: `result = _agent_review_watch_result(watch, result)` is called for **every** active watch on every iteration.

`_agent_review_watch_result()` at line 112 calls `run_agent_json_task()` (line 119) with task_name `"opportunity_watch_review"`. This is an LLM call for every single watch, regardless of status (waiting, triggered, invalidated, expired).

### Watch Statuses

| Status | Set When | DB Update |
|---|---|---|
| `active` | Initial creation (schema.sql line 223: `DEFAULT 'active'`) | Default |
| `waiting` | Evaluate returns "waiting" (conditions not yet met) | `touch_opportunity_watch()` -- only updates `last_checked_at` (line 46) |
| `triggered` | At least one condition hit (line 89) | `update_opportunity_watch_status()` with `triggered_at` set (line 35); enqueues alert job |
| `invalidated` | Invalid condition hit (line 79) | `update_opportunity_watch_status()` with `invalidated_reason` set (line 31) |
| `expired` | `expires_at` is in the past (line 67) | `update_opportunity_watch_status()` with `invalidated_reason="expired"` (line 28) |

### Expiry Handling

- `_is_expired()` at line 209: compares `expires_at` (ISO format) against `datetime.now(timezone.utc)`
- If expired, status is set to `"expired"` and `invalidated_reason` is set to `"expired"` (line 28)
- No special cleanup -- expired watches remain in the table with status `expired`

### opportunity_watches Table Schema (All Columns)

From `schema.sql` lines 214-233:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `symbol` | TEXT | NOT NULL |
| `direction` | TEXT | |
| `watch_reason` | TEXT | |
| `watch_condition_json` | TEXT | NOT NULL |
| `invalid_condition_json` | TEXT | |
| `source_analysis_id` | INTEGER | |
| `source_signal_id` | INTEGER | |
| `status` | TEXT | DEFAULT 'active' |
| `expires_at` | TEXT | |
| `triggered_at` | TEXT | |
| `invalidated_reason` | TEXT | |
| `last_checked_at` | TEXT | |
| `ga_decision_id` | INTEGER | Added via migration (migrations.py line 385) |
| `created_by_user_action` | INTEGER | DEFAULT 0, added via migration (migrations.py line 386) |
| `source_button_action` | TEXT | Added via migration (migrations.py line 387) |
| `created_at` | TEXT | DEFAULT CURRENT_TIMESTAMP |
| `updated_at` | TEXT | DEFAULT CURRENT_TIMESTAMP |

Index: `idx_opportunity_status_symbol ON opportunity_watches(status, symbol)` (schema.sql line 234)

### Existing Unique Indexes on opportunity_watches

**None.** There are no UNIQUE indexes or constraints on `opportunity_watches`. The only index is `idx_opportunity_status_symbol` which is a non-unique composite index on `(status, symbol)`.

## Caveats / Not Found

- The original `opportunity_watch.py` path given in the task (`plugins/crypto_guard/ga_master/opportunity_watch.py`) does not exist. The actual file is at `plugins/crypto_guard/scheduler/opportunity_watcher.py`.
- The watcher calls LLM (`run_agent_json_task`) on **every** active watch on **every** iteration, which could be expensive with many watches.
- There is no deduplication at the database level for opportunity_watches -- the deduplication in `_create_opportunity_watch_from_gate()` (paper_broker.py line 466) is done via a SELECT-then-INSERT pattern with an explicit transaction, not via a UNIQUE index.
