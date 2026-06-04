# CryptoGuard Module Conventions

> Executable contracts and design decisions for the GA CryptoGuard plugin.

---

## 1. Analysis Cycle Architecture

### Convention: 5m is data-only, not an analysis cycle

**What**: The 5m timeframe is used ONLY for data fetching (`fetch_5m_klines`) and entry triggers. There is NO separate `analyze_market_5m` scheduled job.

**Why**: Running a full analysis cycle every 5 minutes was too frequent and produced noise. 15m is the minimum analysis interval.

**Correct**:
```python
# service_manager.py _due_scheduler_jobs
if minute % 5 == 1:
    jobs.append("fetch_5m_klines")  # Data fetch only
# No analyze_market_5m here
if minute in {1, 16, 31, 46}:
    jobs.append("analyze_market_15m")  # 15m is the minimum analysis cycle
```

**Wrong**:
```python
# Do NOT add analyze_market_5m back
if minute % 5 == 2:
    jobs.append("analyze_market_5m")  # REMOVED - causes noise
```

**Config sync**: `scheduler.yaml` must also NOT contain `analyze_market_5m` in jobs or queues sections.

---

## 2. Scoring Logic

### Convention: Base score starts at 0.50, not 0.35

**What**: The deterministic scoring in `strategy_scorer.py` starts at `base = 0.50`.

**Why**: Starting at 0.35 caused almost all analyses to grade as D, even when there was moderate directional evidence. Base 0.50 means "neutral" and requires evidence to move up or down.

**Score math**:
```
base = 0.50
+ PA bullish/bearish: +0.15
+ PA BOS/CHoCH event: +0.05
+ Momentum direction: +0.10
+ Momentum healthy: +0.05
+ Trend early/middle: +0.10
+ Trend multi-TF resonance: +0.05
+ SMC liquidity sweep: +0.06
+ SMC FVG exists: +0.04
+ Order flow confirms: +0.08
+ Chanlun signal: +0.05
+ Low contradiction: +0.08
- Order flow conflicts: -0.05
- High contradiction: -0.15
- Late trend: -0.08
- Range market: -0.10
= capped at [0.0, 0.95]
```

**Grade thresholds** (unchanged):
```
S >= 0.80
A >= 0.72
B >= 0.65
C >= 0.50
D < 0.50
```

---

## 3. Multi-Timeframe Weights

### Convention: 5m weight reduced, 4h/1h/15m emphasized

**What**: In `market_state_builder.py` intraday_framework:
```python
"weights": {"daily": 0.10, "4h": 0.35, "1h": 0.30, "15m": 0.25}
"default_intraday_weights": {"4h": 0.35, "1h": 0.30, "15m": 0.25, "5m": 0.10}
```

**Why**: 5m is noisy and should not dominate the multi-timeframe picture. Higher timeframes carry more weight for direction and structure.

---

## 4. Skill Dynamic Loading

### Convention: Skill contracts must be loaded, not just checked

**What**: `_load_skill_contract()` in `skills/runner.py` loads actual content:
- `skill.yaml` -> parsed YAML dict
- `prompt.md` -> text content
- `schema.json` -> parsed JSON schema
- `feedback_rules.yaml` -> parsed rules list

**Why**: Previously `_load_skill_contract` only checked if files existed. GA needs the actual prompt and schema to interpret tool results and validate outputs.

**Contract shape**:
```python
{
    "skill_name": "price_action",
    "contract_dir": "...",
    "files_present": {"skill.yaml": True, ...},
    "skill_yaml": {"skill_name": ..., "version": ..., "evolution": ...},
    "prompt_md": "Use deterministic swing...",
    "output_schema": {"type": "object", "required": [...], ...},
    "feedback_rules": [{"when": "...", "action": "..."}],
}
```

---

## 5. Skill Feedback Memory

### Convention: Write feedback on anomalies, not just on trades

**What**: `_maybe_write_skill_feedback()` writes to `skill_feedback_memory` when:
- Confidence < 0.30
- Schema validation errors detected
- Skill-specific anomalies (range with high confidence, exhausted divergence, late stage risk)

**Why**: Previously `skill_feedback_memory` had 0 rows because feedback was only written from trade reviews. Proactive feedback helps GA learn from analysis quality issues.

---

## 6. LLM Status Tracking

### Convention: All GA decisions must record analysis_source and llm_status

**What**: `controller_decision_from_legacy()` in `decision_schema.py` includes:
```python
"analysis_source": legacy.get("analysis_source") or "ga_master_controller",
"llm_status": legacy.get("llm_status") or "ok",
"llm_error": legacy.get("llm_error"),
```

**Why**: Without these fields, it was impossible to tell whether a decision came from LLM or deterministic fallback.

**Possible values**:
- `analysis_source`: `"llm_agent"`, `"deterministic_sop"`, `"deterministic_fallback"`, `"ga_master_controller"`
- `llm_status`: `"ok"`, `"failed"`, `"disabled"`, `"controller"`

---

## 7. DuckDB Integration

### Convention: Always have CLI fallback for DuckDB

**What**: `DuckDBAnalytics` tries Python `import duckdb` first, falls back to CLI subprocess at `D:/Program Files/duckdb/duckdb.exe`.

**Why**: The Python duckdb module may not be installed in all environments. The CLI is always available.

**Fallback chain**:
1. `import duckdb` + `duckdb.connect()` -> success -> `"engine": "duckdb_python"`
2. CLI subprocess -> success -> `"engine": "duckdb_cli"`
3. Both fail -> `"status": "degraded"`

**`_query_sqlite_frame` fallback**: When pandas or duckdb unavailable, falls back to pure sqlite3 query.

---

## 8. Scheduler Config Sync

### Don't: Let scheduler.yaml drift from code

**Problem**: `scheduler.yaml` had stale `analyze_market_5m` entries after removing the job from `service_manager.py` and `run_scheduler.py`.

**Fix**: When adding/removing scheduler jobs, update ALL three locations:
1. `service_manager.py` `_due_scheduler_jobs()`
2. `run_scheduler.py` `run_job()`
3. `config/scheduler.yaml` jobs + queues sections

---

## 9. GA Decision Data Flow

### Convention: All decisions flow through GAMasterController

```
Feishu/Scheduler
  -> GAMasterController.analyze_symbol()
  -> ContextBuilder.build()
  -> run_agent_sop_decision() [LLM or deterministic]
  -> RiskGate.check()
  -> build_feishu_actions()
  -> DecisionPersistence.save()
  -> ga_decisions table
```

**Forbidden bypasses**:
- Direct `repo.create_signal()` without GA decision
- Direct `repo.create_paper_order()` without `ga_decision_id`
- Direct `repo.create_opportunity_watch()` without `ga_decision_id` + user button

---

## 10. Strategy Evolution Pipeline

### Convention: Backtest gate before online shadow testing

**What**: Candidate strategies must pass historical backtest gate before entering online shadow testing. This accelerates the feedback loop from 3 trading days to immediate backtest validation.

**Flow**:
```
candidate → backtest_gate (immediate)
           ↓ fail → rejected (strategy_versions.status='rejected', strategy_patches.status='rejected')
           ↓ pass/skip → shadow_testing (5 online signals)
           ↓ pass → waiting for manual confirmation
           ↓ confirm → active
```

**Signatures**:
```python
def run_paired_backtest(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    warmup: int = 30,
    candidate_score_adjustment: float = 0.0,
) -> dict[str, Any]:
    """Same historical data, two strategy versions compared side-by-side.

    Returns: {ok, active_stats, candidate_stats, active_r_values, candidate_r_values,
              active_trade_outcomes, candidate_trade_outcomes, paired_count, no_lookahead}
    """

def run_backtest_gate(
    repo: CryptoGuardRepository,
    *,
    strategy_name: str,
    candidate_version: str,
    symbols: list[str] | None = None,
    lookback_days: int | None = None,
    min_simulated_trades: int | None = None,
    min_decision_samples: int | None = None,
    min_avg_r_improvement: float | None = None,
    max_win_rate_degradation: float | None = None,
    max_drawdown_increase: float | None = None,
    candidate_score_adjustment: float | None = None,
) -> dict[str, Any]:
    """Run historical backtest as fast admission gate.

    Returns gate result. Caller is responsible for saving to strategy_patches.backtest_result_json.
    If candidate patch doesn't contain scoring-related changes, gate is skipped.
    """
```

**Gate checks** (all must pass):
1. Active: `simulated_trades >= min_simulated_trades` OR `decision_samples >= min_decision_samples`
2. Candidate: `simulated_trades >= min_simulated_trades` OR (`decision_samples >= min_decision_samples` AND `r_count >= min_r_count`)
3. `no_lookahead_ok == True` (no future data leakage)
4. `candidate.avg_r - active.avg_r >= min_avg_r_improvement`
5. `candidate.win_rate - active.win_rate >= -max_win_rate_degradation`
6. `candidate.drawdown - active.drawdown >= -max_drawdown_increase`

**Skip condition**: If candidate patch doesn't contain scoring-related changes (`score_adjustment`, `score_adjustments`), gate returns `skipped_or_needs_online_shadow`.

**Config** (`trading_mode.yaml`):
```yaml
evolution:
  backtest_gate:
    enabled: true
    lookback_days: 60
    min_simulated_trades: 30
    min_decision_samples: 80
    min_avg_r_improvement: 0.05
    max_win_rate_degradation: 0.10
    max_drawdown_increase: 0.20
    min_r_count_for_performance_gate: 5
  online_shadow:
    min_samples_after_backtest: 5
    min_samples_without_backtest: 30
```

**Online shadow sample logic**:
| Backtest result | `effective_min_samples` |
|-----------------|-------------------------|
| `gate_disabled: True` | 5 (system configured to skip gate) |
| `passed: True, skipped: False` | 5 (truly passed) |
| `skipped: True` (no scoring changes) | 30 (conservative) |
| `passed: False` (failed) | N/A (candidate rejected) |

**Database**:
- `strategy_patches.backtest_result_json` stores backtest result for audit
- On failure: `strategy_versions.status='rejected'`, `strategy_patches.status='rejected'`

**Why**: Waiting 3 trading days for online shadow testing is too slow when trading frequency is low. Historical backtest provides immediate feedback on candidate viability.

**Key invariant**: Even if backtest passes, online shadow (5 signals) and manual confirmation are still required before activation.

**Error handling**:
- Backtest failure → `strategy_versions.status='rejected'`, `strategy_patches.status='rejected'`, `backtest_result_json` saved
- Config reads from `trading_mode.yaml` with function parameter overrides
- No scoring changes in patch → skip gate (avoid false rejections)
- Candidate must have own minimum data (trades or R values) to prevent "no trade = good" gaming

**Tests** (`test_smoke.py`):
- `test_backtest_gate_disabled_uses_5_samples` — when gate disabled, online shadow uses 5 samples
- `test_backtest_gate_skipped_uses_30_samples` — when skipped (no scoring changes), uses 30 samples
- `test_score_adjustments_field_is_recognized` — `score_adjustments` (plural, dict) recognized as scoring change

---

## 11. Evolution Review Notification Flow

### Convention: verdict_promotion must use interactive outbox, not direct send

**What**: When a candidate strategy passes shadow testing and enters `review_required` status, the notification must be sent via `alert_outbox` with `msg_type="interactive"` (card with buttons), not via direct `send_message()` call.

**Why**: Direct `send_message()` calls have no retry capability and fail silently. Using `alert_outbox` provides:
- Automatic retry on failure
- Consistent delivery tracking
- Deduplication via `dedupe_key`

**Flow**:
```
verdict_runner promotes candidate to review_required
  → enqueue job: evolution_trigger_alert (trigger_type=verdict_promotion)
  → handle_evolution_trigger_alert()
    → build_evolution_review_card() with backtest/shadow details
    → repo.enqueue_alert(alert_type="evolution_review", msg_type="interactive", ...)
    → alert_outbox processes and sends via Feishu API
    → User sees card with approve/reject buttons
    → Button callback triggers approve_evolution or reject_evolution
```

**Card content** (`feishu_cards.py:build_evolution_review_card`):
```python
{
    "candidate_version": "v2-trigger-3",
    "sample_count": 53,
    "reason": "单日 3 笔止损...",
    "backtest_status": {"passed": True/False/Skipped},
    "active_stats": {"avg_r": 0.15, "win_rate": 0.6},
    "candidate_stats": {"avg_r": 0.22, "win_rate": 0.65},
}
```

**Button callbacks** (`run_ga_workers.py:handle_button_callback`):
```python
# approve_evolution
action == "approve_evolution" →
  1. Look up strategy_name from strategy_versions
  2. Call promote_shadow_candidate(repo, strategy_name, candidate_version, confirm=True)
  3. If success: update evolution_triggers.resolved_at, strategy_patches.status='active'

# reject_evolution
action == "reject_evolution" →
  1. UPDATE strategy_versions SET status='rejected' WHERE version=?
  2. UPDATE strategy_patches SET status='rejected' WHERE candidate_version=?
  3. UPDATE evolution_triggers SET status='rejected', resolved_at=now WHERE id IN (SELECT trigger_id FROM strategy_patches WHERE candidate_version=?)
```

**Three-table state sync**:
| Table | Column | approve | reject |
|-------|--------|---------|--------|
| strategy_versions | status | 'active' | 'rejected' |
| strategy_patches | status | 'active' | 'rejected' |
| evolution_triggers | status | 'resolved' | 'rejected' |
| evolution_triggers | resolved_at | now | now |

**Deduplication**: Uses `dedupe_key="evolution_review:{candidate_version}"` to prevent duplicate review cards for the same candidate.

**Cleanup**: Old `msg_type="text"` evolution alerts in outbox are superseded before sending new interactive cards.

**Forbidden**:
```python
# WRONG: Direct send without retry
send_message(receive_id, ..., msg_type="interactive", content=json.dumps(card))

# CORRECT: Use outbox
repo.enqueue_alert(
    alert_type="evolution_review",
    payload={
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        ...
    },
    dedupe_key=f"evolution_review:{candidate_version}",
)
```

---

## 12. Pending Order Lifecycle

### Convention: TTL uses entry_type keys, expires_at computed at creation

**What**: `TTL_CONFIG` keys must match `trade_plan.entry_type` values, not `order_type`:
```python
TTL_CONFIG = {
    "limit": timedelta(hours=8),    # pullback/limit entries
    "trigger": timedelta(hours=4),  # breakout/trigger entries
    "market": timedelta(hours=0),   # immediate fill
}
DEFAULT_TTL = timedelta(hours=8)    # unknown types
```

**Why**: `order_type` (`limit`/`trigger`/`market`) is the execution mechanism. `entry_type` from `trade_plan` is the strategy context. Config keys must match the actual values stored in the database.

**expires_at**: Computed at order creation time in `create_paper_order()` using `compute_expires_at(entry_type)`. Stored in `paper_orders.expires_at`. `expire_pending_orders()` uses `expires_at` when available, falls back to `created_at + TTL`.

**Conflict cancellation**: Must write `invalidated_by_ga_decision_id` from the conflicting GA decision. `conflict_cancelled` is in `feishu.never_silence` to ensure notifications are always delivered.

**Notification**: `notify_order_cancelled()` uses `resolve_report_target()` + `send_markdown_alert()` for delivery via `alert_outbox`. Never creates bare `msg_type: text` payloads without `receive_id`.

**cleanup_stale_pending()**: Uses Python `datetime.fromisoformat()` parsing, NOT SQL string comparison, to avoid format mismatches between ISO timestamps and SQLite `CURRENT_TIMESTAMP`.

**Forbidden**:
```python
# WRONG: bare outbox payload without receive_id
repo.enqueue_alert(alert_type=..., payload={"msg_type": "text", "content": text})

# CORRECT: use resolve_report_target + send_markdown_alert
target = resolve_report_target(repo)
send_markdown_alert(repo, send_message, receive_id=target["receive_id"], ...)

# WRONG: SQL string comparison for time
"WHERE created_at < ?" with isoformat cutoff

# CORRECT: Python datetime parsing
created_at = datetime.fromisoformat(created_at_str)
if created_at < cutoff: ...
```

---

## 13. Account Risk Guard

### Convention: Three-tier account risk system with hard_risk_off and daily_loss_pause

**What**: `AccountRiskGuard` implements a three-tier risk system:
1. **risk_off** (drawdown <= -2.5%): Reduce risk_percent, block bad symbol+side combos
2. **hard_risk_off** (drawdown <= -3.0%): Block ALL new paper orders
3. **daily_loss_pause** (2 consecutive -1R OR daily avg_r <= -0.5): Block ALL new paper orders

**Why**: Without account-level checks, the system could keep opening new trades while the account was in deep drawdown. The three-tier system provides graduated responses.

**Flow**:
```
GAMasterController.analyze_symbol()
  → RiskGate(repo).check()
    → validate_trade_plan() — trade-level
    → AccountRiskGuard(repo).check() — account-level
      → checks drawdown vs thresholds
      → checks daily loss conditions
      → checks symbol+side cooldown
      → checks symbol+side historical avg_r
      → returns risk_off, hard_risk_off, daily_loss_pause, pause_active, blocked
```

**Tier effects**:
- **risk_off** (`drawdown <= -2.5%`):
  - `risk["account_risk"]["risk_off"] = True`
  - `risk["account_risk"]["effective_risk_percent"] = 0.25`
  - Blocked if symbol+side cooldown or negative avg_r
- **hard_risk_off** (`drawdown <= -3.0%`):
  - `risk["account_risk"]["hard_risk_off"] = True`
  - `risk["account_risk"]["pause_active"] = True`
  - ALL new paper orders blocked (only opportunity_watch allowed)
  - Controller forces `decision = "monitor_only"`
- **daily_loss_pause** (2 consecutive -1R OR daily avg_r <= -0.5):
  - `risk["account_risk"]["daily_loss_pause"] = True`
  - `risk["account_risk"]["pause_active"] = True`
  - ALL new paper orders blocked
  - Resets at midnight UTC

**Recovery conditions**:
- Wait 24h since last loss (`recovery_wait_hours: 24`)
- Last N closed trades (default 10) avg_r > recovery_min_avg_r (default 0.0)
- loss_count <= recovery_max_loss_count (default 4)

**Pending order revalidation**:
- When `pause_active` is True, `force_risk_off_pending_revalidation()` converts ALL pending/needs_recheck orders to `risk_off_cancelled`
- Creates `opportunity_watch` entries so signals aren't lost

**Config** (`trading_mode.yaml`):
```yaml
account_risk:
  drawdown_risk_off_threshold: -2.5   # %
  drawdown_hard_risk_off_threshold: -3.0  # %
  risk_off_risk_percent: 0.25
  recovery_min_avg_r: 0.0
  recovery_max_loss_count: 4
  recovery_lookback: 10
  recovery_wait_hours: 24
  daily_loss_pause_consecutive_losses: 2
  daily_loss_pause_avg_r_threshold: -0.5
  cooldown_symbols:
    BTCUSDT_LONG: 48
    LTCUSDT_LONG: 48
    ETHUSDT_LONG: 48
    BNBUSDT_SHORT: 48
```

**Forbidden**:
```python
# WRONG: RiskGate without repo (no account access)
risk_gate = RiskGate()

# CORRECT: RiskGate with repo
risk_gate = RiskGate(repo)
```

---

## 14. Shadow Testing Data Quality

### Convention: Verdict must not promote candidates with only pseudo-R data

**What**: `_stats()` returns `data_source: "real_pnl"` when `pnl_r` values exist, or `"pseudo_r_from_score"` when falling back to score-based pseudo-R. The verdict logic in `run_shadow_test()` blocks promotion when `data_source == "pseudo_r_from_score"`.

**Why**: Pseudo-R (`(score - 0.5) * 2`) has no relation to actual trade outcomes. A candidate with 20 pseudo-R samples showing "high win rate" may perform terribly in real trading. Requiring real `pnl_r` data ensures promotion decisions are based on actual performance.

**Verdict logic**:
```python
if sample_count < effective_min_samples:
    recommendation = "insufficient_samples"
elif pseudo_only:
    recommendation = "data_quality_insufficient"
    shadow_quality_alert = sample_count >= 20  # warning when enough samples but all pseudo
elif candidate_stats better than active_stats:
    recommendation = "candidate_can_be_promoted_with_manual_confirmation"
else:
    recommendation = "reject_candidate"
```

**shadow_quality_alert**: Logged when candidate has >= 20 samples but all are pseudo-R. This indicates the shadow system is accumulating samples without real trade outcomes.

**Forbidden**:
```python
# WRONG: Promote based on pseudo-R data
if candidate_stats["avg_r"] > active_stats["avg_r"]:
    recommend promotion  # avg_r from pseudo_r_from_score is unreliable

# CORRECT: Check data_source first
if candidate_stats.get("data_source") == "pseudo_r_from_score":
    recommend "data_quality_insufficient"  # block until real pnl_r available
```

---

## 15. Pending Order Revalidator

### Convention: Multi-dimensional review beyond TTL and conflict

**What**: `pending_revalidator.py` runs hourly (offset 15 min from pending_order_management) and applies conservative rules to `pending` and `needs_recheck` orders.

**Why**: TTL expiry and direction conflict are necessary but insufficient. Orders can become stale due to trend stage changes, price deviations, or BTC context shifts.

**Rules** (priority order):
1. **needs_recheck timeout**: Orders in `needs_recheck` for > 4 hours → `convert_to_watch`
2. **Late trend stage**: GA decision has `trend_stage in {late, exhausted, transition}` → `convert_to_watch`
3. **Price deviation**: Price moved > 3% from entry → `convert_to_watch`; > 6% → `cancel`
4. **Conflict re-check**: `needs_recheck` order conflicting with strong GA bias → `cancel`

**Actions**:
- `keep` — no change
- `cancel` — set status to `revalidator_cancelled`
- `convert_to_watch` — set status to `watch_cancelled`, create `opportunity_watches` entry
- `needs_manual_review` — set status to `needs_manual_review`

**Notification**: Cancelled/converted orders send markdown alerts via `alert_outbox`.

**Scheduler** (`service_manager.py`):
```python
if minute == 15:
    jobs.append("pending_order_revalidation")
```

**Forbidden**:
```python
# WRONG: Only checking TTL and conflict
expire_pending_orders(repo)
cancel_conflict_pending_orders(repo)
# Missing: trend stage, price deviation, needs_recheck timeout

# CORRECT: Also run revalidator
run_pending_order_management(repo)
revalidate_pending_orders(repo)  # 15 min offset via scheduler
```

---

## 16. Trade Quality Gates (Late Stage + Overextension)

### Convention: Late trend stage and RSI overbought/oversold block trend continuation orders

**What**: `validate_trade_plan()` now includes two additional hard gates:
1. **Late trend stage gate**: `trend_stage` in `{"late", "exhausted"}` blocks trend continuation orders
2. **Overbought/oversold gate**: RSI >= 75 blocks LONG, RSI <= 25 blocks SHORT

**Why**: 
- Late/exhausted trends have high reversal risk — continuation orders get trapped
- Overbought/oversold RSI indicates exhaustion — chasing moves at extremes leads to poor entries

**Behavior**:
- Late stage only blocks **continuation** orders (side aligns with market structure)
- Late stage allows **reversal** orders (side counter to structure)
- RSI thresholds are configurable via `risk.rsi_overbought_threshold` and `risk.rsi_oversold_threshold`

**Config** (`trading_mode.yaml`):
```yaml
risk:
  rsi_overbought_threshold: 75
  rsi_oversold_threshold: 25
```

**Tests**:
- `test_late_stage_trend_continuation_blocked`
- `test_late_stage_reversal_allowed`
- `test_oversold_blocks_short`
- `test_overbought_blocks_long`
- `test_exhausted_stage_blocks_continuation`

---

## 17. Order Flow + Chanlun Confirmation Gates

### Convention: Degraded or opposite order_flow/chanlun signals block trades

**What**: `validate_trade_plan()` now includes order_flow and chanlun confirmation gates:
1. **Order flow gate**: `signal == "degraded"` blocks as primary evidence; opposite `supports` blocks
2. **Chanlun gate**: Opposite `supports` direction blocks trades

**Why**: 
- Degraded order flow cannot serve as primary entry confirmation
- Opposite order_flow/chanlun signals indicate conflicting evidence — entering against them is high-risk

**Behavior**:
- `order_flow.signal == "degraded"` → blocks regardless of direction
- `order_flow.supports == "bearish"` + side == "LONG" → blocks
- `order_flow.supports == "bullish"` + side == "SHORT" → blocks
- Same logic for `chanlun.supports`

**Expected snapshot structure**:
```python
snapshot["modules"]["order_flow"] = {
    "signal": "normal" | "degraded",
    "supports": "bullish" | "bearish" | "neutral",
}
snapshot["modules"]["chanlun"] = {
    "signal": "bullish_divergence" | "bearish_divergence" | ...,
    "supports": "bullish" | "bearish" | "neutral",
}
```

**Tests**:
- `test_order_flow_degraded_blocks_long`
- `test_order_flow_opposite_blocks_short`
- `test_chanlun_opposite_signal_blocks_trade`
- `test_order_flow_normal_allows_trade`

---

**Last updated**: 2026-06-04
