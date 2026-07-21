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

## 18. State Consistency Diagnostics

### Convention: Proactive state monitoring prevents silent evolution loop failures

**What**: `diagnose_state_consistency(repo)` detects four types of state inconsistencies:
1. **Orphan patches**: `strategy_patches` with no matching `strategy_version`
2. **Status mismatches**: trigger/patch/version state inconsistencies
3. **Stale shadows**: candidates in `shadow_testing` >7 days with no new samples
4. **Draft limbo**: patches in `draft` >72 hours (human approval timeout)

**Why**: State inconsistencies can silently break the evolution loop. For example, a trigger stuck in `pending` while its patch is `rejected` prevents new triggers from being created for that pattern.

**Signature**:
```python
def diagnose_state_consistency(repo: CryptoGuardRepository) -> dict[str, Any]:
    """
    Returns:
        {
            ok: bool,
            issues: [{type, severity, details, suggested_action}],
            summary: {orphan_patches, status_mismatches, stale_shadows, draft_limbo},
            total_issues: int,
        }
    """
```

**Issue types and severity**:
| Type | Severity | Detection |
|------|----------|-----------|
| `orphan_patch` | warning | No matching strategy_version |
| `status_mismatch` | error | trigger_pending + patch_rejected |
| `status_mismatch` | warning | version_active + trigger_pending |
| `stale_shadow` | warning | shadow_testing >7 days, no new evals |
| `draft_limbo` | warning | draft >72 hours |

**Module**: `plugins/crypto_guard/diagnostics/state_consistency.py`

**Tests**: 7 tests covering no issues, each issue type, multiple issues, severity levels

---

## 19. Hourly Report Enhancements

### Convention: Report includes risk state, shadow quality, feedback patterns, and direction performance

**What**: `build_hourly_report()` now includes four additional sections:
1. **risk_state**: Current risk_off/hard_risk_off/daily_loss_pause state
2. **shadow_data_quality**: real_pnl vs pseudo_r counts
3. **feedback_patterns**: Top 3 failure patterns this week + most active feedback skill
4. **long_short_performance**: LONG vs SHORT performance breakdown (last 30 days)

**Why**: The original report lacked visibility into:
- Whether the account was in risk_off (critical for trading decisions)
- Shadow testing data quality (pseudo_r vs real_pnl)
- Which failure patterns were recurring
- Whether LONG or SHORT was performing better

**New report sections**:
```
**风险状态：risk_off**（回撤 -2.8%）

**模拟盘方向表现（近 30 天）**
- LONG：15 笔，胜率 60%，avg R=0.25
- SHORT：8 笔，胜率 50%，avg R=0.12

**影子测试数据质量**
- 样本总数：53；真实 PnL：20（38%）；伪 R：33

**本周失败模式（反馈记忆）**
- false_breakout_loss：3 次
- momentum_exhaustion_loss：2 次
- 最活跃反馈 Skill：price_action（5 条）
```

**Data sources**:
- `risk_state`: Uses `AccountRiskGuard.check()` for real-time state
- `shadow_data_quality`: Queries `strategy_evaluations` for pnl_r presence
- `feedback_patterns`: Queries `skill_feedback_memory` last 7 days grouped by pattern_type
- `long_short_performance`: Queries `paper_trades` last 30 days grouped by side

**Module**: `plugins/crypto_guard/notify/hourly_report.py`

**Tests**: 3 existing tests verify rendering still works

---

## 20. Feedback Rules Dry-Run

### Convention: Rules are loaded and matched but never executed (dry-run only)

**What**: `evaluate_feedback_rules_dry_run(repo, lookback_days=30)`:
1. Loads all `feedback_rules.yaml` from skill directories
2. Matches recent feedback entries against `when` conditions via `pattern_type`
3. Outputs matches with `would_execute: True` action
4. Does NOT execute any strategy changes

**Why**: Before enabling rule execution, we need to validate:
- Which rules would fire on real data
- Whether the pattern_type → rule mapping is correct
- Whether rule actions make sense for the matched patterns

**Signature**:
```python
def evaluate_feedback_rules_dry_run(
    repo: CryptoGuardRepository,
    *,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """
    Returns:
        {
            ok: bool,
            matches: [{skill, pattern_type, action, feedback_id, would_execute}],
            summary: {total_matches, by_skill, by_pattern},
            rules_loaded: int,
            feedback_checked: int,
        }
    """
```

**Rule structure** (`feedback_rules.yaml`):
```yaml
feedback_rules:
  - when: false_breakout_loss
    action: increase_confirmation_requirement
  - when: range_misclassified_as_trend
    action: lower_trend_confidence
```

**Skill directory normalization**: `_skill` suffix removed (e.g., `price_action_skill` → `price_action`)

**Module**: `plugins/crypto_guard/diagnostics/feedback_rules_dry_run.py`

**Tests**: 5 tests covering no matches, pattern matching, multiple skills, old feedback skipped, result structure

---

## 21. Feedback TTL/Decay

### Convention: Feedback entries age through fresh → decayed → archived states

**What**: `apply_feedback_ttl(repo)` manages feedback lifecycle:
- **fresh** (0-30 days): Full weight (1.0) in context builder
- **decayed** (30-90 days): Half weight (0.5)
- **archived** (>90 days): Excluded from context unless referenced by active patches

**Why**: Unbounded feedback accumulation:
- Drowns recent signal in old noise
- Makes context builder prompts too long
- Old patterns may no longer be relevant

**Protection**: Feedback referenced by active `strategy_patches` is never archived, even if >90 days old. This prevents breaking feedback chains for in-flight evolution cycles.

**Signatures**:
```python
def apply_feedback_ttl(repo: CryptoGuardRepository) -> dict[str, Any]:
    """
    Returns:
        {
            ok: bool,
            transitions: {fresh_to_decayed, decayed_to_archived, stale_to_archived, protected},
            summary: {fresh, decayed, archived, total},
            previous_summary: {fresh, decayed, archived, total},
        }
    """

def get_feedback_with_ttl_weight(
    repo: CryptoGuardRepository,
    *,
    limit: int = 100,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Returns feedback entries with ttl_weight field (1.0, 0.5, or 0.0)."""
```

**Transition logic**:
```python
# fresh → decayed (30-90 days, not protected)
UPDATE skill_feedback_memory
SET status = 'decayed'
WHERE status = 'fresh' AND created_at < fresh_cutoff AND created_at >= decayed_cutoff
AND id NOT IN (protected_ids)

# decayed → archived (>90 days, not protected)
UPDATE skill_feedback_memory
SET status = 'archived'
WHERE status = 'decayed' AND created_at < decayed_cutoff
AND id NOT IN (protected_ids)

# stale candidate/active → archived (>90 days, not protected)
UPDATE skill_feedback_memory
SET status = 'archived'
WHERE status IN ('candidate', 'active') AND created_at < decayed_cutoff
AND id NOT IN (protected_ids)
```

**Protected IDs**: Extracted from `strategy_patches.evidence_json.feedback_ids` for active/candidate/draft patches.

**Module**: `plugins/crypto_guard/diagnostics/feedback_ttl.py`

**Tests**: 6 tests covering no transitions, fresh→decayed, decayed→archived, protected entries, summary counts, TTL weights

---

**Last updated**: 2026-06-04 (P2: State Diagnostics + Reports + Rules Dry-Run + Feedback TTL)

---

## Breakeven Stop-Loss Idempotency Contracts

> **Trigger**: Production incident on 2026-06-27 — paper order #1 (XRPUSDT) produced 72 identical `stop_loss_adjustment` log/event/outbox rows with `old_stop == new_stop == 1.0494`; 17 hourly summary alerts and 1 `paper_order_filled` alert were wrongly soft-marked as `status='duplicate'` because the `alert_outbox.dedupe_key` unique index scope was too broad.
>
> This section captures the executable contracts that prevent a recurrence.

### Contract 5.1: `update_paper_order_stop_loss` MUST be an atomic conditional UPDATE

**What**: The repository method must use a single conditional `UPDATE ... WHERE` that embeds all inequality, status, and direction guards; check `cur.rowcount`; return `bool`. The caller MUST only enqueue downstream effects on `True`.

**Why**: A `SELECT → compare in Python → UPDATE` sequence has a TOCTOU window. Two worker connections can both pass the Python check and both write a log/event/outbox, producing duplicate rows even when downstream dedupe exists. Embedding the guard in the SQL WHERE makes the check-and-write atomic.

**Signatures**:
```python
def update_paper_order_stop_loss(
    self, order_id: int, stop_loss: float, *, reason: str
) -> bool:
    """Returns True iff a row was actually changed. False on no-op, race-loss,
    closed order, or direction violation. Never raises on these cases."""
```

**Required SQL shape** (side-conditional):
```python
# NULL stop_loss initialization
"UPDATE paper_orders SET stop_loss=? WHERE id=? AND stop_loss IS NULL AND status='open'"

# LONG: new stop must be >= old stop (move toward breakeven/profit only)
"UPDATE paper_orders SET stop_loss=? WHERE id=? AND stop_loss=? AND status='open' AND ? >= stop_loss"

# SHORT: new stop must be <= old stop
"UPDATE paper_orders SET stop_loss=? WHERE id=? AND stop_loss=? AND status='open' AND ? <= stop_loss"
```

**Validation & Error Matrix**:
| Condition | Return | Side effect |
|---|---|---|
| Row missing | `False` | None |
| `new_stop == old_stop` (within 1e-8) | `False` | None — DB untouched |
| `status != 'open'` (closed/cancelled) | `False` | None — closed orders are immutable |
| LONG and `new_stop < old_stop` | `False` | None — forbids widening risk |
| SHORT and `new_stop > old_stop` | `False` | None — forbids widening risk |
| Lost race (another conn won) | `False` | None |
| Successful update | `True` | 1 log row in `paper_trade_logs` with `event_json={order_id, old_stop_loss, new_stop_loss}` |

**Forbidden patterns**:
- `float(row["stop_loss"])` without `is not None` guard — crashes on NULL.
- Python-level `if abs(old-new) < 1e-8: return` followed by unconditional UPDATE — non-atomic.
- Returning `None` — caller cannot distinguish no-op from success.
- Writing the `paper_trade_logs` row before the UPDATE commits — log without state change.

**Reference**: `plugins/crypto_guard/storage/repository.py:1076-1131` (atomic conditional UPDATE + rowcount guard + bool return); `plugins/crypto_guard/storage/repository.py:1115-1118` (NULL-safe branch).

**Tests required**:
- `test_update_paper_order_stop_loss_rejects_closed_order` — status='closed' → `False`, no log row written
- `test_update_paper_order_stop_loss_rejects_wrong_direction_long` — LONG 95→90 → `False`, stop unchanged
- `test_update_paper_order_stop_loss_rejects_wrong_direction_short` — SHORT 105→110 → `False`; 105→100 still allowed
- `test_update_paper_order_stop_loss_atomic_concurrent` — two connections same stop → only one log
- `test_update_paper_order_stop_loss_null_safe` — `stop_loss IS NULL` → updates, no crash
- `test_stop_loss_update_empty_guard_skips_duplicate` — same stop call → `False`, no log

**Audit (grep)**:
```bash
# Must return 0 lines (no Python-side SELECT-then-UPDATE path that ignores rowcount):
grep -nP "SELECT \* FROM paper_orders WHERE id=\?.*\n.*if.*stop_loss.*return\n.*UPDATE paper_orders SET stop_loss" plugins/crypto_guard/storage/repository.py
# Must return the conditional UPDATE:
grep -nP "UPDATE paper_orders SET stop_loss=\? WHERE id=\? AND stop_loss=\?" plugins/crypto_guard/storage/repository.py
```

---

### Contract 5.2: Caller MUST report the real outcome of stop-loss updates

**What**: `_maybe_adjust_stop_to_breakeven` (and any future caller of `update_paper_order_stop_loss`) MUST branch on the `bool` return. `False` MUST short-circuit before `enqueue_job_once` and return a result with `stop_loss_adjusted=False, skip_reason="no_change"`.

**Why**: Before the fix, the caller ignored the method's outcome and always set `stop_loss_adjusted=True`. Downstream consumers (logs, alert delivery, hourly report) treated rejected/raced/same-value updates as successful, which made the duplicated alert problem invisible in production for weeks.

**Reference**: `plugins/crypto_guard/paper/paper_position_updater.py:270-279` — `changed = repo.update_paper_order_stop_loss(...)`; `if changed:` enqueues `paper_event_alert`; else returns `{"ok": True, "stop_loss_adjusted": False, "skip_reason": "no_change", "action": "skip"}`.

**Forbidden patterns**:
```python
# FORBIDDEN — ignores return value, fabricates success
repo.update_paper_order_stop_loss(order["id"], entry, reason="...")
repo.enqueue_job_once("paper_event_alert", ...)              # enqueues even on no-op
result["stop_loss_adjusted"] = True                           # always True
```

**Correct**:
```python
changed = repo.update_paper_order_stop_loss(order["id"], entry, reason="...")
if not changed:
    return {"ok": True, "stop_loss_adjusted": False, "skip_reason": "no_change", "action": "skip"}
repo.enqueue_job_once("paper_event_alert", ...)
return {"ok": True, "stop_loss_adjusted": True}
```

**Tests required**:
- `test_breakeven_returns_no_change_when_atomic_update_fails` — closed order → caller returns `stop_loss_adjusted=False`, `paper_event_alert` job absent

---

### Contract 5.3: `alert_outbox.dedupe_key` unique index MUST be pending-only

**What**: The partial unique index on `alert_outbox(dedupe_key)` MUST cover `WHERE dedupe_key IS NOT NULL AND status='pending'` — NOT `status IN ('pending', 'sent')`.

**Why**: A sent row is historical evidence that an alert was delivered. If the same `dedupe_key` is reused by a later periodic alert (e.g. next hour's `hourly_summary`) or by a retry-after-send, the new pending row must be allowed. The over-broad index made 17 hourly summaries and 1 order-fill alert unservable; the dispatcher silently deduped them against the sent history and they never reached the user.

**Required schema**:
```sql
-- schema.sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_outbox_dedupe_unique
    ON alert_outbox(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status = 'pending';
```

**`enqueue_alert` dedup check**:
```python
# repository.py
if dedupe_key:
    existing = self.conn.execute(
        "SELECT id FROM alert_outbox WHERE dedupe_key=? AND status='pending' LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    if existing:
        return int(existing["id"])
# proceed with INSERT
```

**Default `dedupe_key` policy** (in callers, not in `enqueue_alert` itself):
```python
# alert_delivery.py
PERIODIC_ALERT_TYPES = {"hourly_summary", "daily_summary", "weekly_summary"}

# Time-bucketed keys: each period produces an independent pending row.
# A fixed key like "-:hourly_summary" would make the next period's enqueue
# collide with the previous period's pending row and silently reuse the
# stale payload (e.g. if the dispatcher is slow or the outbox backs up).
now_utc = datetime.now(timezone.utc)
if alert_type == "hourly_summary":
    default_dedupe_key = f"hourly_summary:{now_utc.strftime('%Y-%m-%dT%H')}"
elif alert_type == "daily_summary":
    default_dedupe_key = f"daily_summary:{now_utc.strftime('%Y-%m-%d')}"
elif alert_type == "weekly_summary":
    default_dedupe_key = f"weekly_summary:{now_utc.strftime('%Y-W%W')}"
else:
    default_dedupe_key = None  # one-shot events: no default dedupe_key
```

**Why time-bucketed**: A fixed key like `-:hourly_summary` is permanently unique across all time. If the 14:00 report is still pending when the 15:00 report is enqueued, the 15:00 enqueue would collide with the 14:00 pending row and return the 14:00 id — the 15:00 payload is silently lost. Time-bucketed keys let adjacent periods coexist; within the same bucket, the pending-only unique index still prevents true duplicates.

**Reference**: `plugins/crypto_guard/storage/schema.sql:559-564`; `plugins/crypto_guard/storage/repository.py:1938-1953` (pending-only dedup check + IntegrityError catch); `plugins/crypto_guard/notify/alert_delivery.py:32-36` + `:78-95` (PERIODIC_ALERT_TYPES + time-bucketed dedupe_key).

**Tests required**:
- `test_alert_outbox_pending_only_unique_allows_sent_rerun` — same `dedupe_key`, row A `sent`, new enqueue returns a DIFFERENT id (payload B persisted)
- `test_non_periodic_alert_no_default_dedupe_key` — two `paper_order_filled` enqueues for the same symbol both persist; one `hourly_summary` still folds by its fixed key
- `test_alert_outbox_dedupe_key_prevents_duplicates` — two concurrent pending same-key enqueues collapse to one id (this test existed before)

**Audit**:
```bash
# Index must NOT contain 'sent':
grep -nP "idx_alert_outbox_dedupe_unique" plugins/crypto_guard/storage/schema.sql
# enqueue_alert SQL must filter status='pending' only:
grep -nP "alert_outbox WHERE dedupe_key=\?\s+AND status" plugins/crypto_guard/storage/repository.py
```

---

### Contract 5.4: Business idempotency keys MUST include the target value

**What**: A dedupe key that identifies "this kind of action on this entity" MUST also embed the action's target value (rounded to a stable precision). Keys that omit the target cause legitimate follow-up actions to be silently dropped.

**Why**: Using `system:paper:stop_adjust:{order_id}` made the SECOND breakeven adjustment on the same order (e.g. tightened further) get dropped as a duplicate of the first. The same defect appeared in the migration cleanup: grouping `agent_jobs` by `(order_id, event_type)` soft-marked valid follow-up adjustments as duplicates of the first.

**Required key shapes**:
```python
# Caller (paper_position_updater.py)
dedupe_session = f"system:paper:stop_adjust:breakeven:{order['id']}:{round(entry, 8)}"
```

```sql
-- Migration cleanup (migrations.py _apply_stop_loss_adjustment_dedup)
PARTITION BY json_extract(payload_json, '$.order_id'),
             json_extract(payload_json, '$.event_type'),
             ROUND(json_extract(payload_json, '$.new_stop_loss'), 8)
```

For `enqueue_alert` callers building their own `dedupe_key`: include `(order_id or symbol, alert_type, normalized_target_value)` — never `(symbol, alert_type)` alone.

**Reference**: `plugins/crypto_guard/paper/paper_position_updater.py:284`; `plugins/crypto_guard/storage/migrations.py` (agent_jobs cleanup, partition by 3-tuple including `ROUND(new_stop_loss, 8)`).

**Tests required**:
- `test_breakeven_dedupe_key_different_stops_allowed` — same order, breakeven to 1.0494 and then to 1.0500 → both jobs persist
- `test_agent_jobs_dedup_considers_new_stop` — same order, three event_alert jobs: stop=1.0494, stop=1.0500 (both kept pending), stop=1.0494 again (third soft-marked duplicate)

**Audit**:
```bash
# Caller must include `:breakeven:` and rounded entry:
grep -nP "system:paper:stop_adjust:breakeven:\{order" plugins/crypto_guard/paper/paper_position_updater.py
# Migration must partition by 3-tuple incl ROUND(_,8):
grep -nP "ROUND\(json_extract\(payload_json, '\\\$.new_stop_loss'\), 8\)" plugins/crypto_guard/storage/migrations.py
```

---

### Contract 5.5: One-shot migrations MUST be marker-guarded and idempotent

**What**: Any migration that does heavy table-scans, soft-marks rows, or rebuilds indexes MUST:
1. Be marker-guarded by `_migration_state(key TEXT PRIMARY KEY, applied_at TEXT)`.
2. Use `IF NOT EXISTS` for index creation (NOT `DROP INDEX` + `CREATE`).
3. Run **before** `executescript(schema.sql)` in `initialize_database()`, not after — otherwise `CREATE UNIQUE INDEX` inside schema can collide with dirty existing rows.
4. Early-return on missing required tables (brand-new DB case): the marker is NOT set, so the next call (after schema creates the tables) does the work.

**Why**: `initialize_database()` is called on every worker startup. Without a marker, the heavy dedup scan runs every time (33s extra startup latency per worker, and re-marking the same rows). Without `IF NOT EXISTS`, the index is dropped and rebuilt every boot, briefly removing the uniqueness guarantee. Without pre-schema ordering, a dirty production DB crashes the first `CREATE UNIQUE INDEX` with `UNIQUE constraint failed: alert_outbox.dedupe_key`.

**Required skeleton**:
```python
def _apply_stop_loss_adjustment_dedup(conn: sqlite3.Connection) -> None:
    # Brand-new DB: required tables don't exist yet → skip, leave marker unset
    required = ("alert_outbox", "paper_trade_logs", "agent_jobs")
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?)",
        required,
    ).fetchall()}
    if not required.issubset(have):
        return

    conn.execute("CREATE TABLE IF NOT EXISTS _migration_state(key TEXT PRIMARY KEY, applied_at TEXT)")
    if conn.execute("SELECT key FROM _migration_state WHERE key=?", ("stop_loss_adjustment_dedup_v1",)).fetchone():
        return  # already applied — fast path on every subsequent worker boot

    # ... one-shot cleanup, soft-mark duplicates, etc. ...

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_outbox_dedupe_unique ON alert_outbox(dedupe_key) WHERE dedupe_key IS NOT NULL AND status='pending'")
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state(key, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
        ("stop_loss_adjustment_dedup_v1",),
    )
    conn.commit()
```

**Wiring in `initialize_database`**:
```python
def initialize_database(config=None) -> dict[str, Any]:
    conn = ...
    _apply_stop_loss_adjustment_dedup(conn)  # BEFORE executescript
    with SCHEMA_PATH.open() as f:
        conn.executescript(f.read())
    ...
```

**Reference**: `plugins/crypto_guard/storage/migrations.py:26` (early call); `:1111-1131` (table-guard + marker-guard); `:1148-1151` (`IF NOT EXISTS`); `plugins/crypto_guard/storage/schema.sql:564-571` (`_migration_state` declaration).

**Tests required**:
- `test_initialize_database_idempotent_on_dirty_db` — populated dirty `alert_outbox` + `paper_trade_logs`, run `initialize_database()` twice — both succeed, marker set after first, second call scans 0 rows
- `test_migration_state_table_prevents_repeat_scan` — fresh temp DB: first call leaves marker unset (tables absent), second call does the work + sets marker, third call skips
- `test_dedup_migration_soft_marks_duplicates` — 72 duplicate rows → 71 marked `is_duplicate=true`, earliest one unmarked

**Audit**:
```bash
# Marker table declared in schema:
grep -nP "_migration_state" plugins/crypto_guard/storage/schema.sql
# Migration uses IF NOT EXISTS, not DROP INDEX:
grep -nP "DROP INDEX.*idx_alert_outbox_dedupe_unique" plugins/crypto_guard/storage/migrations.py  # must return 0
grep -nP "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_outbox_dedupe_unique" plugins/crypto_guard/storage/migrations.py  # must return 1
# Migration invoked before executescript in initialize_database:
sed -n '/def initialize_database/,/executescript/p' plugins/crypto_guard/storage/migrations.py | grep -n "_apply_stop_loss_adjustment_dedup"
```

---

### Contract 5.6: Each write operation MUST have exactly one scheduler entry

**What**: A function that mutates DB state (e.g. `update_paper_positions`, `update_shadow_virtual_trades`) MUST be triggered by exactly ONE scheduler job. A background `_loop` thread that calls the same function — even with minute-offset "avoidance" — is forbidden. Idle-path loops MAY exist as heartbeats but MUST NOT call write paths.

**Why**: A 180s `_paper_loop` calling `update_paper_positions` "when minute % 3 != 0" still races the 3min scheduler job — minute boundaries shift under load, GC pauses, sleep drift. Two concurrent calls on the same order enter `_maybe_adjust_stop_to_breakeven` together and both pass the candidate evaluation, both enqueue duplicate event_alerts and write duplicate stop_loss_adjustment logs. This was the proximate cause of the 72 duplicate rows for order #1.

**Required shape**:
```python
# scheduler.py — sole dispatch
if minute % 3 == 0:
    jobs.append("update_paper_positions_3m")

# service_manager.py — _paper_loop must NOT import or call the writer
def _paper_loop(_: Any = None) -> None:
    while True:
        try:
            time.sleep(180)  # heartbeat-only; no DB writes
        except Exception:
            ...
```

**Module import hygiene**: When a loop stops calling a function, that import should also be removed from the loop's module to prevent accidental reintroduction.

**Reference**: `plugins/crypto_guard/service_manager.py:118-126` (loop is heartbeat-only); `run_scheduler.py` (sole dispatch for `update_paper_positions_3m` and `update_shadow_virtual_trades_3m`).

**Tests required**:
- `test_paper_loop_does_not_call_update_paper_positions` — monkeypatch `update_paper_positions` to a sentinel; `_paper_loop` iteration must not invoke it

**Audit**:
```bash
# _paper_loop must not write through update_paper_positions/shadow_virtual_trades:
grep -nP "update_paper_positions|update_shadow_virtual_trades" plugins/crypto_guard/service_manager.py
```
The loop file should reference neither (only the scheduler entry point should).

---

### Production Recovery Runbook (01-jun-2026 incident)

When this contract is violated in production again, the recovery sequence is:

1. **Backup the DB before touching anything.** shutil.copy2 + record sha256.
2. **Restore mis-marked `alert_outbox` rows**: `UPDATE alert_outbox SET status='sent' WHERE dedupe_key LIKE '%hourly_summary' AND status='duplicate'` and `UPDATE alert_outbox SET status='pending' WHERE dedupe_key LIKE '%paper_order_filled' AND status='duplicate'`. (Order-fill alert should be re-dispatched, not silently archived.)
3. **DROP the over-broad unique index** if it predates the pending-only fix: `DROP INDEX IF EXISTS idx_alert_outbox_dedupe_unique`.
4. **DELETE the migration marker** so the new pending-only index + cleanup runs on the existing dirty DB: `DELETE FROM _migration_state WHERE key='stop_loss_adjustment_dedup_v1'`.
5. **Run `initialize_database(cfg)`** — the migration does the dirty-data scan + builds the pending-only index + re-sets the marker.
6. **`check_schema_health()`** to verify.

Reference execution (2026-06-27): 17 hourly_summary rows restored to `sent`, 1 `XRPUSDT:paper_order_filled` restored to `pending`, 72 spurious `paper_trade_logs` stop_loss_adjustment rows soft-marked `is_duplicate=true` (71 from the 72-row tick storm; the earliest was preserved), `agent_jobs` paper_event_alert stop-loss duplicates soft-marked. Backup at `data/crypto_guard/crypto_guard.sqlite3.bak.check_recover_20260627_140929` (sha256 `55042e9d631091fca390c4016060c3a478c67d60c60c6ffd225e565eabc7ee79`).

---

**Last updated**: 2026-06-27 (P1: Breakeven stop-loss idempotency — atomic conditional UPDATE, pending-only outbox dedupe, marker-guarded one-shot migration, single-scheduler enforcement)

---

## 22. Mark Price Contract

### Convention: All financial actions MUST use fresh Binance mark price, never candle close or entry_price

**What**: The module `paper/mark_price.py` provides the single source of truth for current price in all financial actions (breakeven stop adjustments, profit protection, conflict exits). It fetches from Binance USDⓈ-M Futures `/fapi/v1/premiumIndex` endpoint.

**Why**: Using 1h candle close as "current price" can be up to 60 minutes stale. Using `entry_price` as "current price" is semantically wrong and produces incorrect R-multiple calculations. The mark price from Binance is the fair value used for liquidations and funding rate calculations.

**Signatures**:
```python
def fetch_binance_mark_price(symbol: str, *, config, cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch live mark price from Binance USDⓈ-M Futures.
    Returns: {ok, symbol, mark_price, price_time, source: 'binance_mark'}
    """

def get_mark_price_with_fallback(symbol: str, *, repo, cache: dict[str, Any] | None = None,
                                  max_cache_age_seconds: float = 90.0) -> dict[str, Any]:
    """Get mark price with fallback cascade:
    1. Live Binance fetch (cached per cycle)
    2. paper_positions last_price (if <= 90s old)
    3. Error — fail-closed, never use candle close or entry_price
    Returns: {ok, symbol, mark_price, price_time, source}
    """

def clear_cycle_cache() -> None:
    """Clear the module-level mark price cache. Called at start of each
    update_paper_positions() cycle."""
```

**Fail-closed behavior**: If mark price is unavailable (API down, network error), the function returns `ok=False` and the caller must set the position to `needs_position_recheck`. Never fall back to candle close or entry_price as a substitute for current price.

**Cache semantics**: Per-cycle caching via a module-level dict + shared dict passed through call chain. Cache is cleared at the start of each `update_paper_positions()` cycle. Within a cycle, the same symbol's mark price is fetched once and reused.

**Module**: `plugins/crypto_guard/paper/mark_price.py`

**Forbidden**:
```python
# WRONG: Using candle close as current price
current_price = float(candle["close"])  # up to 60 min stale

# WRONG: Using entry_price as current price
current_price = order["entry_price"]  # semantically wrong

# CORRECT: Use mark price with fallback
result = get_mark_price_with_fallback(symbol, repo=repo, cache=mark_price_cache)
if not result["ok"]:
    return {"needs_position_recheck": True, "error": "mark_price_unavailable"}
current_price = result["mark_price"]
```

---

## 23. Profit Protection Contract

### Convention: Profit protection evaluates BEFORE routine breakeven, closes on strong reverse signals

**What**: When a paper position has accumulated significant MFE (Maximum Favorable Excursion) and then retraces against a strong S-grade reverse GA signal, the position is closed immediately to protect profits. This is evaluated BEFORE routine breakeven stop adjustments.

**Why**: Without profit protection, profitable positions can give back all gains during reversals. The XRPUSDT order #1 incident lost +1.44R MFE because no mechanism existed to close on reversal signals.

**Six-condition gate** (all must pass):
1. GA decision is actionable (not `monitor_only`, `no_edge`, `hold_position`)
2. Signal grade is `S`
3. Confidence >= 0.85
4. `mfe_r >= 1.00` (position has accumulated at least 1R of profit)
5. `current_r >= 0.30` (position is still profitable, not underwater)
6. `retracement_r >= 0.50` (position has given back at least 0.5R from MFE)

**R-multiple computation**:
```python
initial_risk_usdt = trade["initial_risk_usdt"]
mfe_r = (mfe_price - entry_price) / initial_risk_usdt  # for LONG
current_r = (mark_price - entry_price) / initial_risk_usdt  # for LONG
retracement_r = mfe_r - current_r
```

**Side effects on close**:
1. `close_paper_trade(repo, trade_id, mark_price, close_reason="strong_conflict_profit_protection")`
2. `update_paper_order_status(repo, order_id, "closed")`
3. `backfill_active_evaluation_pnl_r(repo, ga_decision_id, trade_id, pnl_r)`
4. `upsert_paper_position(repo, order_id, symbol, mark_price, ...)`
5. `log_paper_trade_event(repo, order_id, "profit_protection", ...)`
6. `enqueue_job("trade_review", ...)`
7. `enqueue_job("paper_event_alert", ...)`

**Idempotency**: Uses dedupe key `profit_protection:{trade_id}:{ga_decision_id}`. The same GA decision cannot trigger profit protection on the same trade twice.

**Integration point**: `position_conflict_revalidator.py` — because only the conflict revalidator has access to the GA decision with `signal_grade` and `confidence` fields. The routine breakeven in `update_paper_positions` runs without a GA decision.

**Config** (`trading_mode.yaml`):
```yaml
position_conflict:
  profit_protection:
    enabled: true
    min_grade: "S"
    min_confidence: 0.85
    min_mfe_r: 1.00
    min_current_r: 0.30
    min_retracement_r: 0.50
    action: "close_full"
```

**Functions**:
```python
# paper_position_updater.py
def _evaluate_profit_protection(repo, order, trade, ga_decision, config, *, mark_price_cache) -> dict:
    """Evaluate the 6-condition profit protection gate. Returns {triggered, ...}."""

def _execute_profit_protection_close(repo, order, trade, ga_decision, mark_price, ...) -> dict:
    """Execute full close with all side effects. Idempotent via dedupe key."""

# position_conflict_revalidator.py
def _evaluate_profit_protection_inline(repo, order, trade, ga_decision, config, *, mark_price_cache) -> dict:
    """Bridge function that delegates to _evaluate_profit_protection from paper_position_updater."""
```

---

## 24. Notification Time Contract

### Convention: All paper trading notifications MUST include UTC+8 event time via shared formatter

**What**: Every paper trading notification (fill, stop adjustment, close, expiry, profit protection, conflict) must contain an explicit UTC+8 event time. The shared formatter `format_event_time_cst` in `notify/time_utils.py` is the single source of truth.

**Why**: Before this contract, notifications had inconsistent time formatting:
- Some used inline `strftime + (UTC+8)`
- Some used `datetime.now(timezone(timedelta(hours=8)))` directly
- Some had no time at all
- The `hourly_report.py` and `run_ga_workers.py` each had their own `_format_time_utc8` / `_fmt_utc8` implementations

**Shared formatter** (`notify/time_utils.py`):
```python
CST = timezone(timedelta(hours=8))

def format_event_time_cst(dt: datetime | str | int | float | None) -> str:
    """Format to 'YYYY-MM-DD HH:mm:ss (UTC+8)'.
    - Naive datetimes treated as UTC
    - String inputs parsed as ISO8601
    - int/float treated as Unix milliseconds
    - Returns '不可用' if None or unparseable
    """

def format_event_time_cst_compact(dt: datetime | str | int | float | None) -> str:
    """Format to 'YYYY-MM-DD HH:MM (UTC+8)' (no seconds)."""

def format_event_time_cst_for_line(dt: Any) -> str:
    """Format for a notification detail line: '时间：YYYY-MM-DD HH:mm:ss (UTC+8)'."""

def now_cst_iso() -> str:
    """Return current UTC+8 time as ISO8601 string."""
```

**Price labels in notifications**:
| Event type | Price label | Price source |
|---|---|---|
| `paper_order_filled` | 成交价 | `entry_price` |
| `stop_loss_adjustment` | 当前 Mark Price | `mark_price` |
| `conflict_exit` / `strong_conflict_profit_protection` | 退出 Mark Price | `exit_price` (mark price) |
| `stop_loss_hit` / `take_profit_hit` | 退出价 | `exit_price` |
| Other close events | 退出价 | `exit_price` |

**Forbidden patterns**:
```python
# WRONG: Inline strftime + manual UTC+8
f"时间：{dt.strftime('%Y-%m-%d %H:%M')} (UTC+8)"

# WRONG: Direct CST construction
now_utc8 = datetime.now(timezone(timedelta(hours=8)))

# WRONG: Using entry_price as generic "价格"
f"- 价格：{order['entry_price']}"

# CORRECT: Use shared formatter
from plugins.crypto_guard.notify.time_utils import format_event_time_cst
event_time = format_event_time_cst(payload.get("event_time"))
```

**Coverage**: All notification paths in `run_ga_workers.py` (`handle_paper_event_alert`, `handle_paper_drawdown_alert`, auto-create order notification), `hourly_report.py` (`_format_time_utc8`), and `position_conflict_revalidator.py` (`_notify_action`) must use the shared formatter.

**Module**: `plugins/crypto_guard/notify/time_utils.py`

---

**Last updated**: 2026-06-27 (P0: Mark price contract, profit protection contract, notification time contract)

---

## 25. Hourly Report Accuracy Contracts

> **Trigger**: Production hourly report contained stale decisions, misclassified executable opportunities, and LLM summary text that contradicted structured execution state.

### Contract 25.1: Batch completion gate — report MUST wait for all symbols, degraded on failure

**What**: The scheduler registers an `analysis_batches` row with `batch_id = f"{primary_interval}:{analysis_time}"` at enqueue time. Each symbol's status is tracked atomically in `batch_symbol_status` (independent detail table, not JSON columns). The report renderer takes a single snapshot; if the batch is incomplete, the hourly report job is re-enqueued with a 30-second delay. **The worker never polls or sleeps** — it returns instantly and re-enqueues.

**Retry budget** (FR-3): `max_retries = ceil(timeout_seconds / poll_interval_seconds)`, derived from `scheduler.yaml` `batch_gate.timeout_seconds` and `batch_gate.poll_interval_seconds`. The `max_retries` key is NOT in the config — it is computed. `timeout_seconds=0` → immediate degraded report (no retries). Invalid values fall back with a warning.

**Degraded report** (FR-1): When the batch is absent, failed, or has zero completed symbols after exhausting retries, the report renders a deterministic degraded report with:
- Banner: "当前行情分析不可用，本报告未采用历史信号代替"
- System metadata (scheduler time, retry count, batch state)
- Risk state (from `AccountRiskGuard`)
- Position summary (from `paper_positions`)
- NO historical signals, NO `analysis_states`, NO `ga_decisions`, NO LLM commentary

**Why**: Without a batch gate, the report could render mid-cycle — some symbols had fresh decisions while others still showed stale rows from the previous cycle. Without a degraded path, the report either adopted stale previous-cycle decisions (phantom opportunities) or silently produced an empty report. The degraded path is deterministic and fail-closed: it never substitutes historical data for current analysis.

**Signatures**:
```python
# repository.py
def start_analysis_batch(self, batch_id, primary_interval, analysis_time, enabled_symbols) -> None:
def mark_batch_symbol_completed(self, batch_id, symbol, *, status="completed", failed=False) -> None:
    # Atomic INSERT OR REPLACE into batch_symbol_status
    # status: "completed" | "failed" | "pending" (for skipped-pending dedup)
def finish_analysis_batch(self, batch_id) -> None:
    # Called automatically by run_ga_workers when is_batch_complete=True
def is_batch_complete(self, batch_id) -> bool:
    # Checks: all enabled_symbols registered + no status='pending' rows remaining
def batch_symbol_counts(self, batch_id) -> dict:
    # Returns {completed: N, failed: N, pending: N, total: N}
def get_analysis_batch(self, batch_id) -> dict | None:
    # Populates completed/pending/failed counts from batch_symbol_status
def latest_ga_decisions_by_symbol(self, *, batch_id=None, min_analysis_time=None) -> dict:
    # When batch_id given, adds WHERE batch_id=? to SQL

# hourly_report.py
def _await_batch_completion(repo, *, primary_interval: str = "15m", expected_batch_id: str | None = None) -> dict:
    """Single snapshot of batch state — NO polling, NO sleep.
    Returns {complete, incomplete, completed_count, total_count, pending_symbols, ...}
    Caller (build_hourly_report) re-enqueues if incomplete.
    expected_batch_id: FR-2 retry identity — keeps original batch across retries."""

def _compute_retry_budget(gate_cfg: dict) -> int:
    """Derive max_retries from timeout_seconds / poll_interval_seconds.
    timeout_seconds=0 → 0 retries (immediate degraded).
    Invalid/missing values → fallback with warning."""

def _should_use_degraded_report(batch_state: dict) -> bool:
    """True if batch is absent, failed, or zero completed symbols."""

def _render_degraded_report(repo, now: int, batch_state: dict, report_hour_utc: str | None, expected_batch_id: str | None) -> dict:
    """Render deterministic degraded report with banner, system/risk/position only."""
```

**Retry identity chain** (FR-2): Re-enqueue carries `report_hour_utc`, `expected_batch_id`, `expected_analysis_time`, and `retry_count` through scheduler→worker→report→re-enqueue. Session ID pattern: `hourly_report_retry:{report_hour_utc}:{expected_batch_id}:{retry_count}`. Uses `enqueue_job_once()` to prevent duplicate retry jobs. Original delivery context is preserved across retries.

**Scheduler wiring**: `enqueue_market_analysis` in `cron_scheduler.py` creates the batch row + inserts `batch_symbol_status` rows for each enabled symbol (status="pending"). `run_ga_workers.py` marks each symbol completed/failed on job resolution, then checks `is_batch_complete` and calls `finish_analysis_batch`. When a symbol is skipped (already pending), `mark_batch_symbol_completed(batch_id, symbol, status="pending")` marks it as pending (not completed) — the existing job will change it to completed/failed when it resolves.

**Database**:
- `analysis_batches` table: `batch_id, primary_interval, analysis_time, status, enabled_symbols_json, started_at, finished_at`. No JSON status columns (completed/failed/pending tracked in detail table).
- `batch_symbol_status` table: `batch_id TEXT, symbol TEXT, status TEXT DEFAULT 'pending', updated_at TEXT, PRIMARY KEY (batch_id, symbol)`. This is the single source of truth for per-symbol completion state. Atomic `INSERT OR REPLACE` prevents concurrent write loss.

**Migration**: `_apply_hourly_report_accuracy_migration` runs BEFORE `executescript(schema.sql)` to add `batch_id/previous_grade/rendered_summary` columns and create `batch_symbol_status` table on old databases. `_migrate_batch_json_to_symbol_status` populates the new table from existing JSON columns. Schema indexes use `IF NOT EXISTS`.

**Forbidden**:
```python
# WRONG: Read-modify-write JSON for batch completion (concurrent loss)
completed = json.loads(row["completed_symbols_json"])
completed.add(symbol)
conn.execute("UPDATE ... SET completed_symbols_json=?", json.dumps(completed))

# CORRECT: Atomic INSERT OR REPLACE on detail table
conn.execute("INSERT OR REPLACE INTO batch_symbol_status (batch_id, symbol, status, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", ...)

# WRONG: Mark skipped-pending as completed (analysis hasn't finished)
mark_batch_symbol_completed(batch_id, symbol)  # default status="completed"

# CORRECT: Mark as pending (existing job will resolve it later)
mark_batch_symbol_completed(batch_id, symbol, status="pending")

# WRONG: Render report without batch_id filter (may mix batches)
decisions = repo.latest_ga_decisions_by_symbol(min_analysis_time=analysis_time)

# CORRECT: Filter by batch_id for precise report content
batch_state = _await_batch_completion(repo, batch_id)
decisions = repo.latest_ga_decisions_by_symbol(batch_id=batch_id, min_analysis_time=analysis_time)
```

---

### Contract 25.2: Opportunity classification — three tiers obeying execution gates

**What**: The report classifies each symbol into exactly one of three tiers:
1. **executable_opportunity**: grade ∈ {S,A,B}, confidence ≥ MIN_CONFIDENCE_FOR_PAPER_ORDER, has trade_plan, risk_check.ok, decision=create_paper_order or trade_plan_available, non-stale
2. **observation_candidate**: grade ∈ {S,A,B} but missing at least one execution gate
3. **no_edge**: grade ∈ {C,D} or decision ∈ {monitor_only, no_edge, hold_position}

**Why**: Previously, all S/A/B grades were labeled "executable" regardless of risk_check, trade_plan, or confidence. A B-grade with risk_check=false was shown as a trading opportunity.

**Classification function**:
```python
def _opportunity_classifier(decision: dict) -> str:
    """Returns 'executable_opportunity', 'observation_candidate', or 'no_edge'."""
    from plugins.crypto_guard.notify.report_consistency import execution_eligible, is_valid_trade_plan
    grade = str(decision.get("signal_grade") or "").upper()
    if grade in {"S", "A", "B"}:
        if execution_eligible(decision) and not _is_stale_decision(decision):
            return "executable_opportunity"
        return "observation_candidate"
    return "no_edge"
```

**Trade plan validation**: `is_valid_trade_plan(plan)` checks that the plan dict contains required fields: `side`, `entry_type`, `entry_price` or `trigger_price`, `stop_loss`, `take_profit` or `take_profits`. A placeholder dict like `{"note": "no plan"}` returns False. Used in both `execution_eligible` and `_opportunity_classifier`.

**Staleness check**: A decision is stale if `analysis_time` is older than one analysis cycle (15m) from the report's batch `analysis_time`.

**Report rendering**: Executable opportunities show in the "可执行机会" section; observation candidates show separately with blocking reasons; no_edge symbols show in the observation section without executable claims.

---

### Contract 25.3: Deterministic text override — structured state overrides LLM summary

**What**: The validator in `notify/report_consistency.py` rewrites `final_summary` when structured fields contradict executable claims in the text. When `execution_eligible(decision)` is False, all `FORBIDDEN_EXECUTABLE_PHRASES` are stripped and an override clause is appended.

**Why**: The LLM may generate summary text claiming "具备模拟盘条件" (eligible for paper trading) even when risk_check=false or trade_plan is missing. The structured fields are the ground truth; the summary must not contradict them.

**Canonical phrase list** (single source of truth in `report_consistency.py`):
```python
FORBIDDEN_EXECUTABLE_PHRASES: tuple[str, ...] = (
    "具备模拟盘条件", "具备模拟盘做多条件", "具备模拟盘做空条件",
    "风控全部满足", "风控指标全部满足",
    "可创建订单", "风控通过", "存在可执行",
    "建议设置 limit", "建议设置 trigger",
    "建议做多", "建议做空", "可开仓", "可入场",
)
```

**Override clause**: When phrases are stripped, `仅观察/未通过执行门禁：{gate_blockers}` is appended once, where `_gate_blockers` lists the specific failing conditions (invalid trade_plan, risk reasons, low confidence). `_gate_blockers` uses `is_valid_trade_plan` for plan validity, not just `bool(has_trade_plan)`.

**Integration points**:
1. `controller.py analyze_symbol()` — applies `rewrite_inconsistent_summary` before persistence
2. `llm_agent_judge.py _normalize_llm_decision` — applies for non-LLM path
3. `hourly_report.py render_ga_hourly_summary` — double-check at render time; `_decision_row` prefers `rendered_summary` over `final_summary`

**Forbidden**:
```python
# WRONG: Duplicating the forbidden phrases list
FORBIDDEN = ["具备模拟盘条件", "可创建订单"]  # drift risk

# CORRECT: Import from single source
from plugins.crypto_guard.notify.report_consistency import FORBIDDEN_EXECUTABLE_PHRASES

# WRONG: Using bare "可执行" as forbidden phrase
# This matches negative statements like "没有可执行机会" (no executable opportunity)
# Use "存在可执行" which only matches affirmative claims
```

---

### Contract 25.4: Grade hysteresis — dampen wild grade swings between cycles

**What**: `grade_with_hysteresis` in `strategy/grade_config.py` dampens large single-cycle grade jumps. It accepts the current grade (string, not numeric score) and the previous grade. When the grade delta is ≥ 2 tiers, the grade is clamped to one step above/below the previous grade. `emergency_down=True` bypasses hysteresis for genuine risk events (hard_risk_off, daily_loss_pause). `clamp_grade` prevents S/A-grade when 4H structure is range/transition/unknown without `independent_trend` evidence, and limits counter_evidence items to `SA_MAX_COUNTER_EVIDENCE` (default 3).

**Why**: An S→D→S oscillation within two cycles indicates instability, not a genuine signal change. Hysteresis prevents whipsaw-induced false opportunities from entering the executable tier. Without `emergency_down`, real risk deterioration could be masked by hysteresis dampening.

**Signatures**:
```python
def grade_with_hysteresis(current_grade: str, previous_grade: str | None, *,
                          emergency_down: bool = False) -> tuple[str, str]:
    """Dampen large jumps. D→S becomes D→C; S→D becomes S→B.
    emergency_down=True: bypass hysteresis (for hard_risk_off/daily_loss_pause)."""

def clamp_grade(grade: str, *,
                has_trade_plan: bool, risk_ok: bool,
                confidence: float | None = None,
                htf_conflict: bool = False,
                independent_trend: bool = False,
                counter_evidence_count: int = 0) -> tuple[str, str]:
    """Prevent S/A when 4H=range/transition/unknown without independent_trend.
    Cap counter_evidence to SA_MAX_COUNTER_EVIDENCE items."""

def grade_delta(current: str, previous: str | None) -> int:
    """Compute signed delta between grades. S=5, A=4, B=3, C=2, D=1."""
```

**Previous grade source**: `previous_ga_decision_grade(exclude_batch_id=)` skips current batch decisions to avoid same-batch contamination. The controller passes `exclude_batch_id=request.batch_id`.

**emergency_down activation**: `risk_gate.check()` runs BEFORE `grade_with_hysteresis`. When the risk gate result's `account_risk.hard_risk_off` or `account_risk.daily_loss_pause` is True, `emergency_down=True` is passed to `grade_with_hysteresis`. This allows immediate downgrade without hysteresis dampening.

**4H conflict**: When 4H market structure is `range`, `transition`, `unknown`, or empty, `htf_conflict=True` unless `independent_trend` is True. Only `bullish` is non-conflicting for LONG, only `bearish` for SHORT. This prevents S-grade on unconfirmed higher-timeframe direction.

**Database**: `ga_decisions.previous_grade` column stores the grade used for hysteresis calculation.

---

### Contract 25.5: Report diagnostics — 10 P2 checks for accuracy

**What**: `diagnose_report_accuracy(repo, *, batch_id=None)` in `diagnostics/report_diagnostics.py` runs 10 checks covering the known issue categories. It returns the standard `{ok, issues, summary, total_issues}` shape so it can be merged into `diagnose_state_consistency` output or rendered standalone.

**Issue codes**:
| Code | Severity | What it checks |
|------|----------|---------------|
| `hourly_report_incomplete_batch` | warning | Batches with pending symbols (uses `batch_symbol_status`, not JSON columns; checks all statuses, not just running) |
| `hourly_report_stale_decision` | warning | Decisions older than one 15m cycle (scoped to `batch_id` when provided) |
| `executable_opportunity_without_trade_plan` | warning | S/A/B grade + `create_paper_order`/`trade_plan_available` decision missing trade_plan |
| `executable_opportunity_risk_rejected` | warning | S/A/B grade + `create_paper_order`/`trade_plan_available` decision with risk_check=false |
| `opportunity_below_confidence_threshold` | warning | S/A/B grade below min_confidence |
| `summary_execution_state_conflict` | error | Forbidden phrases in summary despite gate failure |
| `excessive_grade_flip` | warning | S/A→D/C within 4 hours |
| `direction_flip_without_closed_candle` | warning | Direction flip without closed candle evidence or **structured BOS/CHoCH confirmation** (FR-5: text containing BOS/CHoCH keywords is NOT confirmation; requires event_type in structural break set, timeframe in supported set, closed status, parseable event time, direction matching) |
| `invalid_liquidity_sweep_semantics` | warning | sell_side paired with explicit bearish belief words ("看空"/"bearish"), or buy_side with explicit bullish belief words ("看多"/"bullish"). Neutral direction words like "向下"/"向上" are NOT flagged. |
| `negative_drawdown_display` | warning | Positive drawdown_percent when equity shows loss |

**Structured direction confirmation** (FR-5): `_has_structured_confirmation(repo, cur, new_side, *, prev_ts=0)` validates ALL fields of a snapshot event before accepting it as confirmation of a direction flip:
1. `event_type` must be in `_STRUCTURAL_BREAK_TYPES` (BOS, CHoCH, etc.)
2. `timeframe` must be in `_SUPPORTED_TIMEFRAMES` (15m, 1h, 4h, 1d)
3. `closed` must be truthy (not pending/None)
4. Event time must be parseable via `_parse_event_time()` (handles seconds <1e12, milliseconds >=1e12, ISO strings)
5. Event time must be after `prev_ts` and not after current decision time
6. Direction must match `new_side`

Text-only evidence (e.g. summary containing "BOS"/"CHoCH" keywords) is explicitly rejected — only structured event objects from `module_analysis_results` qualify.

**Production module shape** (FR-5): `_lookup_snapshot_events(repo, snapshot_id, symbol)` queries `module_analysis_results` with `module IN ('price_action', 'smc', 'smc_orderflow')`. Real production modules are `price_action` and `smc` (4,320 rows each); `smc_orderflow` is included for forward compatibility but currently has 0 rows. The `result_json` field is parsed; `structure_events` is a list of `{event, type, reference_high, reference_low, close}` objects. The normalizer maps `event` (e.g. `bullish_bos`, `bearish_choch`) to canonical `{event_type, direction, closed, time, timeframe}` — `event_type` is uppercased (`BOS`, `CHOCH`), direction is derived from the `bullish_*`/`bearish_*` prefix, `timeframe` and `analysis_time` come from the `module_analysis_results` row columns.

**Integration**: `run_for_report(repo)` wraps the diagnostic call with a never-raises guarantee for render-time use. On exception, returns `ok=False` (fail-closed, not fail-open). `_check_summary_execution_state_conflict` uses `is_valid_trade_plan()` for trade plan validation, not simple dict non-empty check.

**Drawdown sign convention**: Internal `_drawdown_percent` returns negative values for losses. External display must be non-negative (e.g. "回撤 0.50%"). The diagnostic uses `initial_balance` from `paper_accounts` for relative comparison, not a hardcoded threshold.

---

### Contract 25.6: Lossless batch_symbol_status migration (FR-4)

**What**: `_ensure_batch_symbol_status_check_constraint()` in `migrations.py` rebuilds the `batch_symbol_status` table when the CHECK constraint on `status` is missing or incorrect. The migration is idempotent and lossless.

**Why**: Production databases created before the CHECK constraint was added could have invalid statuses (`running`, `skipped`, etc.) that bypass the batch gate logic. Simply adding the constraint would fail on existing dirty data. The migration normalizes invalid rows before applying the constraint.

**Migration procedure**:
1. SAVEPOINT — all changes rollback on failure
2. Check for residual temp table `_batch_symbol_status_old` from prior failed migration; drop if exists
3. Copy table with explicit column list (NOT `SELECT *`) — guards against schema drift
4. Separate valid rows (`status IN ('pending', 'completed', 'failed')`) from invalid rows
5. Normalize invalid statuses to `'pending'` with audit entries in `_migration_state` (key encodes batch_id, symbol, original_status, normalized_to)
6. Verify row count matches after rebuild
7. Apply CHECK constraint with exact regex: `CHECK (status IN ('pending', 'completed', 'failed'))`
8. Validate constraint exists post-migration
9. RELEASE SAVEPOINT on success; ROLLBACK TO on failure

**Schema health check**: `check_schema_health()` validates the CHECK constraint pattern using exact regex, not substring matching.

**Forbidden**:
```python
# WRONG: SELECT * (fragile to schema changes)
INSERT INTO new_table SELECT * FROM old_table

# CORRECT: Explicit column list
INSERT INTO new_table (batch_id, symbol, status, updated_at) SELECT batch_id, symbol, status, updated_at FROM old_table

# WRONG: DROP TABLE without residual cleanup
DROP TABLE IF EXISTS _batch_symbol_status_old  # missed before rebuild starts

# CORRECT: Check and clean residual first
conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_batch_symbol_status_old'")
```

---

### Contract 25.7: Account risk guard ordering (FR-6)

**What**: `AccountRiskGuard.check(symbol, side)` in `risk/account_risk_guard.py` MUST compute `combo_avg_r` and `cooldown` blocks BEFORE any early return — including the "no risk-off territory" path and the "recovery eligible" path. If `blocked=True` was computed, the result MUST honor it (return a full result dict with `blocked=True`/`blocked_reason`/`cooldown_active`), NOT call `_ok_result()` which forces `blocked=False`.

**Why**: The recovery-eligible path previously returned `_ok_result()` immediately, ignoring a just-computed `blocked=True` from combo cooldown or negative `combo_avg_r`. This allowed new positions to open on combos with negative historical avg_r, bypassing the combo gate.

**Required ordering**:
1. Get account, compute `drawdown_pct`
2. Check daily loss pause, hard_risk_off, risk_off
3. Compute `cooldown_active` / `cooldown_until` from `cooldown_symbols` config + last loss time
4. Compute `combo_avg_r` from `paper_trades` (last 20 by symbol+side)
5. If `combo_avg_r < 0 and not blocked`: set `blocked=True`, `blocked_reason`
6. Early return paths (not risk_off / recovery_eligible): if `blocked`, return full result dict honoring `blocked`; otherwise `_ok_result()`

**Forbidden**:
```python
# WRONG: Recovery bypasses combo gate
if recovery_eligible:
    return _ok_result(drawdown_pct=drawdown_pct)  # ignores blocked=True

# CORRECT: Honor blocked flag even on recovery-eligible path
if recovery_eligible:
    if blocked:
        return {"ok": True, "risk_off": False, ..., "blocked": True, "blocked_reason": blocked_reason, ...}
    return _ok_result(drawdown_pct=drawdown_pct)
```

---

### Contract 25.8: Retry identity time pinning (FR-2)

**What**: `_await_batch_completion(repo, *, primary_interval, expected_batch_id, expected_analysis_time)` in `notify/hourly_report.py` MUST use `expected_analysis_time` as the `cutoff_ms` for filtering `ga_decisions` when provided. It MUST NOT recompute the cutoff from `utc_ms()` on each retry — that would shift the cutoff across 15-minute boundaries and silently filter out the original batch's decisions.

**Why**: A retry that fires after a 15-minute boundary recomputes `cutoff_ms = latest_closed_close_time_ms(primary_interval, utc_ms())`, which now points to the NEXT 15-minute slot. The original batch's decisions (with `analysis_time` from the previous slot) get filtered out, and the renderer falls back to stale or empty data.

**Retry identity chain**: `build_hourly_report` carries `expected_analysis_time` from the original `enqueue_market_analysis` call through every retry. The chain: `cron_scheduler.enqueue_market_analysis` → `agent_jobs.payload` → `run_ga_workers.build_hourly_report` → `_await_batch_completion(expected_analysis_time=...)`. On retry re-enqueue, the same `expected_analysis_time` is preserved in the new job's payload.

**Config-derived retry budget** (FR-3): `build_hourly_report` and `_await_batch_completion` use `_compute_retry_budget(gate_cfg)` to derive `timeout_seconds` and `poll_interval_seconds`, NOT raw `int(gate_cfg.get("timeout_seconds", 300))`. This ensures invalid config values (e.g. `"not-a-number"`) fall back gracefully instead of crashing the build.

**Forbidden**:
```python
# WRONG: Recompute cutoff on each retry
cutoff_ms = latest_closed_close_time_ms(primary_interval, utc_ms())

# CORRECT: Pin to expected_analysis_time from original batch
cutoff_ms = int(expected_analysis_time) if expected_analysis_time is not None else latest_closed_close_time_ms(primary_interval, utc_ms())

# WRONG: Direct int() on config value (crashes on invalid input)
timeout_seconds = int(gate_cfg.get("timeout_seconds", 300))

# CORRECT: Use normalizer with fallback
retry_budget = _compute_retry_budget(gate_cfg)
timeout_seconds = retry_budget["timeout_seconds"]
```

---

## 37. BTC#9 Trade Gate Contracts

### 37.1 Fallback LLM-Failed Must Not Create Paper Orders

**Contract**: When `llm_status` is `"failed"` or `"disabled"`, `apply_risk_to_decision` must force `has_trade_plan=False`, `decision="monitor_only"`, and filter `create_paper_order` from `suggested_actions`.

**Config gate**: `risk.fallback_llm_failed_blocks_paper_order` (default `true`).

**Audit fields**: `fallback_trade_plan_blocked`, `fallback_block_reason`, `original_decision`, `downgraded_decision`.

### 37.2 Entry Confirmation Hard-Blocks Without Structured Evidence

**Contract**: Bare strings (`"x"`, `"manual_close_above_60300"`, `"5m 突破确认"`) are rejected. Only structured dicts of type `closed_candle_confirmation` pass `_validate_entry_confirmation`.

**Config gate**: `risk.require_entry_confirmation_for_paper_order` (default `true`).

### 37.3 HTF Support Reason Must Be Self-Consistent

**Contract**: When `_htf_support` returns `ok=True`, `reason` must be empty (not contain "不支持"). When `ok=False`, `reason` explains why.

**Weak structure**: Two or more TFs in `{range, transition}` → `ok=False` unless a valid structured entry confirmation provides exemption.

### 37.4 Market Phase Chop Must Not Boost Confidence

**Contract**: `apply_regime_gate` `aligned` branch: only `risk_on`, `rebound`, `risk_off`, `selloff` phases may boost. `chop`, `transition`, `unknown` → `effective_delta=0.0` with `confidence_boost_suppressed_reason`.

### 37.5 Fill Must Revalidate Latest GA Before Execution

**Contract**: `_revalidate_pending_before_fill` queries `_latest_ga_decision` with `ORDER BY analysis_time DESC, id DESC`. Exception or missing GA → `ga_recheck_unavailable` (fail-closed). GA conflict (LONG vs bearish S/A/B or SHORT vs bullish S/A/B) → cancel with SAVEPOINT/CAS, audit log on success only.

### 37.6 Limit Fill Must Validate Candle Health

**Contract**: `_validate_limit_fill_candle` requires entry zone reclaim AND no adverse momentum. `_is_unhealthy_pullback_bar` blocks doji-free adverse-body candles with excessive wick ratio.

### 37.7 Invalid Condition Must Have Buffer From Stop Loss

**Contract**: LONG: `stop < invalid_condition_price <= entry`. SHORT: `entry <= invalid_condition_price < stop`. Buffer ratio: `_invalid_condition_price(invalid, side, entry)` uses `buffer_ratio` from `invalid_condition_buffer_ratio` config (default 0.3), clamped [0.1, 0.5].

### 37.8 Per-Candle 1m Closed Candle Processing

**Contract**: `paper_position_updater.update_paper_positions()` processes only fully closed 1m candles. `last_processed_candle_time` cursor advances only on confirmed fills. Unclosed candles are never used for fill decisions.

### 37.9 Independent Contract Marker

**Contract**: BTC#9 diagnostics use `btc9_trade_gate_contract_v1` marker (NOT the R4 marker). Written by `_ensure_btc9_trade_gate_contract_marker` during `initialize_database`. `INSERT OR IGNORE` ensures idempotency.

### 37.10 Diagnostic Severity Semantics

**Contract**: `state_consistency.ok` is `False` only when `error_count > 0`. Severities: `error`, `warning`, `legacy_info`. Six BTC#9 diagnostic types with contract-marker cutoff gating.

### 37.11 Event-Time Threading (Round 2)

**Contract**: `fill_order_if_triggered(event_time)` accepts candle.close_time (ms). All timestamps — `paper_orders.filled_at`, `paper_trades.created_at`, `paper_positions.opened_at/updated_at`, `paper_trade_logs` ts, `stop_take_path_json` filled ts, `paper_event_alert` payload `filled_at` — use `iso_utc_from_ms(event_time)`. Missing/unparseable event_time → fail-closed (`missing_event_time`). Repository APIs (`create_paper_trade`, `update_paper_order_status`, `log_paper_trade_event`) accept explicit `event_time`/`filled_at` params; no internal `utc_iso()`.

### 37.12 Post-Fill Candle Continuation (Round 2)

**Contract**: After fill on candle N, `update_paper_positions` continues processing remaining closed candles through the open-order path (SL/TP, path metrics). The fill candle itself only creates the trade; SL/TP deferred to next candle (conservative). Same-candle SL+TP ambiguity: SL priority, `ambiguous_intrabar` recorded. Cursor advances per-candle; never advances past a failed candle. Restart resumes from last successful cursor without duplicate fills.

### 37.13 Structured Confirmation Real-Source Verification (Round 2)

**Contract**: `_validate_entry_confirmation` accepts `repo`/`snapshot`/`module_analysis_results` kwargs. When provided, confirmation must match a real event in module output (matched via `_find_matching_real_event`): source/module, timeframe, event_type, direction, candle_close_time, price (within 0.01%), closed=True. LLM self-reported closed/source/direction not trusted. `source="deterministic_rule"` must trace to deterministic rule output. Price must be finite positive; NaN/Infinity/0/negative rejected. `candle_close_time` must be positive int ms, closed, and <= analysis_time.

### 37.14 PA structure_events Traversal (Round 2)

**Contract**: `_extract_structured_entry_confirmation` traverses `price_action.structure_events` and `smc.structure_events`. Forbids defaulting `timeframe`/`closed`/`direction` — missing fields reject the event. Generic BOS/CHoCH requires explicit `direction` field. Selection: direction matches trade side, closed=True, `close_time <= analysis_time`, price finite positive. Returns newest valid event (sorted by close_time descending). None if no valid event (deterministic plans blocked by `require_ec` gate).

### 37.15 Strict invalid_condition Ordering (Round 2)

**Contract**: `_invalid_condition_price(invalid, side, entry)` returns None when entry is None (fail-closed, no old fallback). Uses `buffer_ratio` from config (default 0.3, clamped [0.1, 0.5]). LONG: `stop < invalid_condition_price < entry`. SHORT: `entry < invalid_condition_price < stop`. `validate_trade_plan` enforces strict `<` (not `<=`). Rounding re-validates strict ordering. Config anomalies/NaN/越界 use safe defaults with audit.

### 37.16 GA Recheck Fail-Closed with Idempotent Audit (Round 2)

**Contract**: `_revalidate_pending_before_fill` requires `event_time` for limit orders (fail-closed `missing_event_time`). Calls `_latest_ga_decision(repo, symbol, max_analysis_time=event_time)` — SQL adds `AND analysis_time <= ?` upper bound. Only cancels if latest GA `analysis_time` > order's GA `analysis_time`. Exception → `ga_recheck_unavailable` return, order stays pending, cursor not advanced. `_log_ga_recheck_unavailable` writes idempotent audit (dedupe_key). Missing latest GA → fail-closed.

### 37.17 Conflict Cancel SAVEPOINT/CAS Audit (Round 2)

**Contract**: SAVEPOINT wraps `UPDATE paper_orders SET status='revalidator_cancelled' WHERE id=? AND status IN ('pending','needs_recheck')`. `cur.rowcount == 1` checked before audit log. `position_id=NULL` for pending orders (no trade yet). `event_json` includes: `order_id`, `original_ga_decision_id`, `invalidated_by_ga_decision_id`, `order_side`, `latest_bias`, `latest_grade`, `event_time`, `dedupe_key`. CAS failure (rowcount=0) → rollback, no log, no notification, cursor not advanced. Duplicate calls retain one successful audit.

### 37.18 Network Error vs No-Data Distinction (Round 2)

**Contract**: `_fetch_unprocessed_closed_candles` returns `{"ok": False, "error": "network_error", "candles": []}` on exception, `{"ok": True, "error": None, "candles": [...]}` on success. Filters to only closed candles (`close_time < now_ms`), sorts by `open_time` ascending. Network error on first page: skip order with `candle_fetch_network_error` skip_reason, preserve cursor, no fill. Success with no candles: use mark_price for equity only, no fill trigger.

### 37.19 Paged Backfill (Round 2)

**Contract**: `update_paper_positions` fetches multiple pages until: no more closed candles, config cap reached (`max_candles_per_page=500`, `max_pages_per_batch=10`, `max_candles_per_batch=500`), error, or order closed/filled. Each page `startTime` strictly > prev page last `close_time`. Cursor persisted per-candle after successful processing. Network error preserves last successful cursor. Multi-page test (1200 candles) verifies order, no duplicates, no gaps.

### 37.20 Cutoff-Gated Diagnostics (Round 2)

**Contract**: `_check_fallback_llm_failed_created_paper_order` and `_check_missing_entry_confirmation_paper_order` use `_btc9_contract_cutoff(repo)` as SQL WHERE filter. Pre-marker data: `legacy_info` or excluded (never error). Post-marker: `error`. `missing_entry_confirmation` calls unified `_validate_entry_confirmation` (not just non-empty check) — bare strings and fabricated objects are invalid. Aggregate COUNT detects LIMIT truncation. `_check_chop_regime_boosted` checks `transition`/`unknown` (not just `chop`) for abnormal boosts.

### 37.21 Marker Missing Diagnostic (Round 2)

**Contract**: `_check_btc9_contract_marker_missing` emits `severity=error` when `btc9_trade_gate_contract_v1` marker absent from `_migration_state`. Marker absent → diagnostics must not silently skip all BTC#9 checks and report healthy. `initialize_database()` writes marker via `INSERT OR IGNORE` after all schema + migration steps succeed. Fresh DB after `initialize_database()` must have marker. Marker is written LAST (only after `check_schema_health()` passes).

**Verified on production DB (2026-07-01, READ-ONLY)**: Production DB at `data/crypto_guard/crypto_guard.sqlite3` (674MB, 44 tables) has 3 markers (`stop_loss_adjustment_dedup_v1`, `profit_protection_mark_price_contract_v1`, `hourly_report_accuracy_r4_contract_v1`) but `btc9_trade_gate_contract_v1` is MISSING. `diagnose_state_consistency()` correctly emits `btc9_contract_marker_missing` with `severity=error`, `ok=False`, `error_count=1`.

**Verified on fresh DB (2026-07-01)**: Temp DB after `initialize_database()` has both `hourly_report_accuracy_r4_contract_v1` and `btc9_trade_gate_contract_v1` markers. `check_schema_health(conn=conn)` returns `ok=True`. `diagnose_state_consistency()` returns `ok=True`, `error_count=0`, `btc9_contract_marker_missing=0`.

### 37.22 R3-A: Structured Confirmation Provenance-Aware Validation

**Contract**: `_validate_entry_confirmation` (`risk_engine.py:437`) accepts `repo`/`snapshot`/`module_analysis_results` kwargs. When provided, confirmation must match a real event in module output via `_find_matching_real_event` (`risk_engine.py:494`): source/module, timeframe, event_type, direction, candle_close_time, price (within 0.01%), closed=True. LLM self-reported closed/source/direction not trusted. `source="deterministic_rule"` must trace to deterministic rule output. Price must be finite positive; NaN/Infinity/0/negative rejected. `candle_close_time` must be positive int ms, closed, and <= analysis_time. No match → fail-closed.

**Tests**: `test_r3a_fabricated_confirmation_rejected_by_validate_trade_plan`, `test_r3a_fabricated_confirmation_cannot_activate_weak_structure_exemption`, `test_r3a_matching_pa_event_passes`, `test_r3a_matching_smc_event_passes`, `test_r3a_price_mismatch_rejected`, `test_r3a_event_time_mismatch_rejected`, `test_r3a_source_mismatch_rejected`, `test_r3a_direction_mismatch_rejected`, `test_r3a_closed_none_rejected`, `test_r3a_closed_missing_rejected`, `test_r3a_closed_false_rejected`, `test_r3a_deterministic_rule_without_rule_id_rejected`, `test_r3a_deterministic_rule_with_rule_id_shape_passes`.

### 37.23 R3-B: Event-Time Threading and Post-Fill Continuation

**Contract**: `fill_order_if_triggered(repo, order, price, *, event_time=None)` (`paper_broker.py:809`) accepts candle.close_time (ms). All timestamps — `paper_orders.filled_at`, `paper_trades.created_at`, `paper_positions.opened_at/updated_at`, `paper_trade_logs` ts, `stop_take_path_json` filled ts, `paper_event_alert` payload `filled_at` — use `iso_utc_from_ms(event_time)`. Missing/unparseable event_time → fail-closed (`missing_event_time`). `allow_wall_clock=False` in repository APIs for replay paths.

Post-fill: After fill on candle N, `update_paper_positions` (`paper_position_updater.py:109`) continues processing remaining closed candles through the open-order path (SL/TP, path metrics). The fill candle only creates the trade; SL/TP deferred to next candle (conservative). Same-candle SL+TP ambiguity: SL priority, `ambiguous_intrabar` recorded. Cursor advances per-candle; never advances past a failed candle.

**Tests**: `test_r3b_historical_fill_writes_event_time_everywhere`, `test_r3b_historical_sl_writes_closing_candle_time_everywhere`, `test_r3b_historical_tp_writes_closing_candle_time_everywhere`, `test_r3b_missing_replay_event_time_creates_no_trade`, `test_r3b_live_mode_supports_wall_clock_execution`, `test_r3b_replay_crossing_utc_day_boundary_attributed_to_candle_day`.

### 37.24 R3-C: Cursor Preservation on Retryable GA Failure

**Contract**: `_revalidate_pending_before_fill` (`paper_broker.py:593`) requires `event_time` for limit orders (fail-closed `missing_event_time`). Calls `_latest_ga_decision(repo, symbol, max_analysis_time=event_time)` — SQL adds `AND analysis_time <= ?` upper bound. Exception → `ga_recheck_unavailable` return, order stays pending, cursor not advanced. `_log_ga_recheck_unavailable` writes idempotent audit (dedupe_key). Missing latest GA → fail-closed. Retryable skip stops processing of later candles (cursor preserved at failed candle).

**Tests**: `test_r3c_ga_recheck_unavailable_preserves_cursor`, `test_r3c_retryable_skip_stops_processing_later_candles`, `test_r3c_idempotent_audit_single_record_per_order_candle_reason`, `test_r3c_cancel_race_lost_preserves_cursor`.

### 37.25 R3-D: GA Recheck Time Semantics

**Contract**: GA recheck baseline time: if `ga_decision_id` exists, baseline = that GA's integer `analysis_time`; if no `ga_decision_id`, baseline = `order.created_at` converted to ms (`paper_broker.py:593-685`). Only GA decisions with `analysis_time` > baseline AND `analysis_time <= event_time` may invalidate the order. Conflict cancel uses `event_time` (not wall clock) for audit timestamp. SAVEPOINT rollback on cancel exception. Latest GA analysis_time read exception returns `ga_recheck_unavailable`.

**Tests**: `test_r3d_created_at_baseline_when_no_ga_decision_id`, `test_r3d_baseline_unavailable_when_no_ga_id_and_no_created_at`, `test_r3d_conflict_cancel_uses_event_time_not_wall_clock`, `test_r3d_ga_recheck_unavailable_distinct_from_baseline_unavailable`, `test_r3d_savepoint_rollback_on_cancel_exception`, `test_r3d_latest_ga_analysis_time_read_exception_returns_unavailable`.

### 37.26 R3-E: Cutoff-Gated Diagnostics with Aggregate Count

**Contract**: `_check_fallback_llm_failed_created_paper_order` (`state_consistency.py:1543`) and `_check_missing_entry_confirmation_paper_order` (`state_consistency.py:1627`) use `_btc9_contract_cutoff(repo)` as SQL WHERE filter. Pre-marker data: `legacy_info` or excluded (never error). Post-marker: `error`. `missing_entry_confirmation` calls unified `_validate_entry_confirmation` (not just non-empty check) — bare strings and fabricated objects are invalid. Aggregate `COUNT(*)` detects LIMIT truncation (more than 500 candidate rows cannot produce false clean). `_check_chop_regime_boosted` (`state_consistency.py:1821`) checks `chop`, `transition`, AND `unknown` phases for abnormal boosts (not just `chop`).

**Tests**: `test_r3e_persisted_fabricated_confirmation_diagnosed_post_marker`, `test_r3e_equivalent_pre_marker_row_excluded_or_legacy`, `test_r3e_confirmation_matching_persisted_module_evidence_not_reported`, `test_r3e_more_than_500_candidate_rows_cannot_produce_false_clean`.

### 37.27 R3-F: Strict invalid_condition Ordering

**Contract**: `_invalid_condition_price(invalid, side, entry)` (`ga_judge.py:43`) returns `None` when `entry` is `None` (fail-closed, no old fallback). Uses `buffer_ratio` from config (default 0.3, clamped [0.1, 0.5]). LONG: `stop < invalid_condition_price < entry`. SHORT: `entry < invalid_condition_price < stop`. `validate_trade_plan` (`risk_engine.py:183`) enforces strict `<` (not `<=`). Rounding re-validates strict ordering. Missing `closed` field rejected; `closed=False` rejected; equality with entry or stop rejected.

**Tests**: `test_r3f_missing_closed_rejected`, `test_r3f_closed_false_rejected`, `test_r3f_equality_with_entry_rejected_long`, `test_r3f_equality_with_entry_rejected_short`, `test_r3f_equality_with_stop_rejected_long`, `test_r3f_equality_with_stop_rejected_short`, `test_r3f_missing_entry_cannot_generate_invalidation_level`.

### 37.28 R3-G: Paged Backfill Production Config

**Contract**: `update_paper_positions` (`paper_position_updater.py:109`) fetches multiple pages until: no more closed candles, config cap reached, error, or order closed/filled. Each page `startTime` strictly > prev page last `close_time`. Cursor persisted per-candle after successful processing. Network error preserves last successful cursor. Production config (`trading_mode.yaml:48-51`): `max_candles_per_batch=1500`, `max_candles_per_page=500`, `max_pages_per_batch=10`. The invariant `max_candles_per_batch >= 3 * max_candles_per_page` (1500 >= 3*500=1500) ensures at least 3 pages of downtime recovery capacity.

**Tests**: `test_r3g_production_config_processes_1200_candles_over_three_pages`, `test_r3g_page_two_failure_stops_at_last_page_one_candle`, `test_r3g_deduplicates_page_boundary_candles`, `test_r3g_malformed_data_stops_and_preserves_cursor`.

---

## 38. Hourly Analysis Semantic Accuracy Contracts (07-03)

> **Trigger**: Production hourly report contained multi-timeframe semantic contradictions — neutral bias paired with middle trend stage, countertrend rebounds labeled as bullish-middle, HTF conflicts not capping confidence, canonical summary diverging from structured fields, and observation reasons lacking market context. The legacy diagnostic `htf_countertrend_overconfidence` was a no-op in production because it read `snapshot.profiles` (which never exists in the production `ga_decision` shape).

### Contract 38.1: Multi-Timeframe Direction Transparency (FR-1)

**What**: `market_semantics.build_timeframe_context(snapshot, config)` produces a `timeframe_context` dict mapping each of `{1d, 4h, 1h, 15m}` to `{bias, structure, closed}`. `compute_alignment(timeframe_context)` derives an `alignment` enum in `{aligned, partial, countertrend_rebound, neutral, unknown}`. `htf_conflict` is True when 1D bias opposes 1H/15M bias AND 4H does not confirm the lower-timeframe direction.

**Why**: Without structured per-timeframe context, the hourly report could claim "bullish middle" based on a 15m rebound while 1D/4H were bearish — a countertrend rebound disguised as a trend-following opportunity. Persisting the structured context in `ga_decisions.raw_decision_json.timeframe_context` makes the multi-TF state inspectable by diagnostics and report renderers without re-deriving from the snapshot.

**Propagation contract**: `controller_decision_from_legacy` (`ga_master/decision_schema.py:27`) MUST propagate `timeframe_context`, `alignment`, `htf_conflict`, and `market_reason_codes` from the legacy judge output to the top-level `ga_decision` dict. The test helper must not bypass this path — production and test must exercise the same normalizer.

**Forbidden**:
```python
# WRONG: test helper constructs ga_decision dict directly, bypassing controller_decision_from_legacy
ga_decision = {"symbol": s, "signal_grade": g, "timeframe_context": tc, ...}

# CORRECT: test exercises the production controller path
ga_decision = controller_decision_from_legacy(legacy=legacy_judge_output, ...)
```

### Contract 38.2: Bias-Stage Semantic Contract (FR-2)

**What**: `normalize_market_semantics(decision, snapshot, config)` (`reasoning/market_semantics.py`) applies a 5-step pipeline. Step 2b enforces the bias-stage contract: `neutral`/`mixed`/`unknown` bias MUST pair with `range`, `transition`, or `unknown` stage; `bullish`/`bearish` bias MUST pair with `early`, `middle`, or `late`. Illegal combinations are normalized to the closest legal pairing (non-directional bias → `transition`; directional bias unchanged), and `market_reason_codes` records the original contradiction.

**Why**: A `neutral` bias with a `middle` stage is semantically meaningless — middle stages require directional bias. Previously the report accepted this contradiction, producing labels like "中性-中段" that mislead traders into expecting a continuation that the bias does not support. R1-9 (07-03 final review): the original spec text listed `early/accumulation` as legal stages for `neutral`, which contradicted the PRD/contract used by `market_semantics.py` Step 2b (`range/transition/unknown`). This misalignment allowed tests to assert `neutral+early` as legal while the code demoted it.

**Legal combinations** (R1-9 aligned with `market_semantics.py` + `ga_decision.schema.json`):
| Bias | Allowed stages |
|------|----------------|
| bullish | early, middle, late |
| bearish | early, middle, late |
| neutral | range, transition, unknown |
| mixed | range, transition, unknown |
| unknown | range, transition, unknown |

### Contract 38.3: HTF Conflict Confidence Cap (FR-3)

**What**: When `htf_conflict=True`, `normalize_market_semantics` Step 3 caps `confidence` to `htf_conflict_confidence_cap` (default 0.70, config-driven). Step 4 then checks `non_executable = signal_grade not in {S,A} OR capped_conf < MIN_CONFIDENCE_FOR_PAPER_ORDER`. When `non_executable=True`, the pipeline collapses `decision` to `monitor_only`, sets `has_trade_plan=False`/`trade_plan=None`, and strips `create_paper_order` from `suggested_actions`.

**Why**: Previously, an S-grade countertrend rebound with `htf_conflict=True` could retain confidence 0.78 and trigger `create_paper_order` — executable against the higher-timeframe direction. The cap plus the action-collapse guarantees that HTF-conflicting decisions never cross the execution threshold.

**Config validation at startup**: `htf_conflict_confidence_cap` MUST be < `MIN_CONFIDENCE_FOR_PAPER_ORDER` (0.72). The config loader validates this; invalid values raise at startup (fail-closed, not silent fallback).

**Why both conditions in Step 4**: The S→A grade downgrade alone (Step 3) keeps the grade in `{S,A}`, so without the `capped_conf < threshold` check, action-collapse would not trigger and the decision would remain executable with sub-threshold confidence. Both conditions are required.

### Contract 38.4: Canonical Summary Single Source (FR-5)

**What**: `summary_builder.build_canonical_market_summary(decision)` produces a deterministic Chinese summary string from the structured fields (bias, stage, alignment, htf_conflict, market_reason_codes, evidence). The controller ALWAYS sets `rendered_summary` on the persisted ga_decision. `legacy_decision_from_ga_decision` (`ga_master/decision_schema.py:81`) prefers `rendered_summary` over `final_summary` for the legacy `summary` field so downstream signal/brief consumers read the canonical text. The original LLM text is preserved in `raw["raw_llm_summary"]`.

**Why**: The LLM summary could contradict the structured fields — claiming "具备做多条件" while `alignment=countertrend_rebound` and `htf_conflict=True`. The canonical summary is deterministic: it cannot contradict the structured fields because it is derived from them. Persisting it as `rendered_summary` makes the canonical text available to report renderers without re-deriving from raw fields.

**Round-trip contract**: `ga_decisions.raw_decision_json` MUST preserve both `rendered_summary` (canonical) and `raw_llm_summary` (original LLM text). The DB round-trip test verifies both are intact after insert+read.

### Contract 38.5: Observation Reason Explains Market (FR-4)

**What**: `hourly_report._format_market_reason_text(decision)` produces a market-context string from `timeframe_context`, `alignment`, `htf_conflict`, and `market_reason_codes`. `_format_observation_market_and_gate_reasons(decision)` combines the market reason with gate blockers (invalid trade_plan, risk reasons, low confidence) so the observation line explains BOTH the market state AND why execution is blocked.

**Why**: Previously the observation reason only listed gate blockers ("confidence 0.65 below threshold") without explaining the market state. A trader reading "confidence too low" had no context for WHY — was it an HTF conflict? A countertrend rebound? Data incomplete? The market reason provides that context.

**Market context phrases** (`_MARKET_CONTEXT_PHRASES`): maps `market_reason_codes` to Chinese phrases. `htf_conflict` → "高周期冲突", `countertrend_rebound` → "逆势反弹", `overextended` → "过度延伸", `data_incomplete` → "数据不完整", `bias_stage_contradiction` → "bias-stage 矛盾".

### Contract 38.6: C/D List Top-N Labeling (FR-6)

**What**: `hourly_report._format_cd_reasons(decisions)` labels the C/D list with "重点原因（前 N 项，另有 M 项）" where N is the top-N shown (default 3) and M is the count of additional reasons. Both the legacy C/D path and the new hourly-report path call the shared `_compact_items` helper so the labeling is identical.

**Why**: Previously the C/D list could show all reasons (verbose) or a truncated list without indicating truncation (misleading). The "前 N 项，另有 M 项" label makes truncation explicit and preserves auditability.

### Contract 38.7: Five Semantic Diagnostics + Marker (FR-7)

**What**: `diagnostics/report_diagnostics.py` registers 5 semantic checks plus a marker-missing check, all gated by the `hourly_market_semantic_accuracy_contract_v1` marker:

| Code | Severity | What it checks |
|------|----------|----------------|
| `timeframe_context_missing` | error | ga_decision lacks `timeframe_context` or it's empty |
| `bias_stage_contradiction` | error | bias/stage pairing violates Contract 38.2 |
| `htf_conflict_not_capped` | error | `htf_conflict=True` but `confidence > htf_conflict_confidence_cap` |
| `canonical_summary_missing` | error | ga_decision lacks `rendered_summary` |
| `htf_countertrend_overconfidence` | error | LONG/SHORT with bullish/bearish S/A/B grade while `htf_conflict=True` AND confidence not capped (production-shape: reads `raw_decision_json.timeframe_context` first, falls back to legacy `snapshot.profiles` and `raw_legacy_decision.snapshot.profiles`) |
| `hourly_market_semantic_accuracy_contract_marker_missing` | error | `hourly_market_semantic_accuracy_contract_v1` marker absent from `_migration_state` |

**Production-shape reading** (P0-2 fix): `_check_htf_countertrend_overconfidence` reads `raw_decision_json.timeframe_context` (the production path) FIRST, then falls back to `snapshot.profiles` (legacy shape) and `raw_legacy_decision.snapshot.profiles` (nested legacy). The `_tf_ctx_bias(entry)` helper reads the `bias` field, falling back to `structure` for directional values (`bullish`/`bearish`/`range`/`transition`/`unknown`). This ensures the diagnostic fires on production data, not just legacy test fixtures.

**Marker**: `hourly_market_semantic_accuracy_contract_v1` is written by `_ensure_hourly_market_semantic_accuracy_contract_marker` during `initialize_database()`, AFTER `check_schema_health()` passes. `INSERT OR IGNORE` ensures idempotency. The marker is the cutoff gate: pre-marker data is `legacy_info` or excluded (never error); post-marker data is `error`.

**Marker cutoff**: `_apply_semantic_marker_cutoff` filters diagnostic candidates by `created_at >= marker_timestamp`. Pre-marker rows are excluded or downgraded to `legacy_info` (sev `warning`), never `error`.

### Contract 38.8: Data/Time Contract (FR-8)

**What**: `build_timeframe_context(snapshot, config)` requires `closed=True` for each timeframe. When `closed=False` or missing, the timeframe is marked `data_incomplete` and `market_reason_codes` includes `data_incomplete`. The degraded path (`_is_data_degraded`) is exercised indirectly via config-loading tests.

**Why**: Acting on a partially-closed timeframe means acting on a candle that hasn't confirmed — the bias could flip on the next 1m tick. The `closed=True` requirement is the multi-TF analogue of the single-TF closed-candle contract (§37.1).

### Contract 38.9: Acceptance Test Coverage

**Required tests** (all in `plugins/crypto_guard/tests/test_smoke.py::TestHourlyAnalysisSemanticAccuracy07_03`):

| Test | Contract |
|------|----------|
| `test_doge_countertrend_rebound_not_bullish_middle` | 38.1 |
| `test_bnb_neutral_does_not_pair_with_middle_stage` | 38.1, 38.2 |
| `test_controller_decision_from_legacy_propagates_structured_fields` | 38.1 (propagation) |
| `test_ltc_neutral_does_not_pair_with_early_stage` | 38.2 |
| `test_bias_stage_combinations_all_legal` | 38.2 |
| `test_htf_conflict_confidence_capped` | 38.3 |
| `test_fault_injection_htf_countertrend_overconfidence_detected` | 38.7 (both legacy `FAULTHTF` and production `FAULTHTF2` shapes) |
| `test_observation_reason_includes_market_context` | 38.5 |
| `test_fault_injection_observation_reason_missing_market_context_detected` | 38.7 |
| `test_canonical_summary_matches_structured_fields` | 38.4 |
| `test_db_roundtrip_preserves_canonical_summary_and_raw_llm` | 38.4 |
| `test_cd_list_with_six_symbols_shows_top3_label` | 38.6 |
| `test_both_report_paths_use_same_helper` | 38.6 |
| `test_marker_missing_diagnosed` | 38.7 (marker) |
| `test_semantic_accuracy_marker_missing_diagnosed` | 38.7 (semantic-accuracy marker specifically) |
| `test_marker_pre_legacy_info_classification` | 38.7 (cutoff) |

**Test authenticity requirements**: tests MUST call the production `controller_decision_from_legacy` path (not bypass it), MUST NOT mock the function under test, MUST NOT use env-var bypasses, MUST NOT loosen assertions to make tests pass.

### Contract 38.10: Migration Idempotency

**What**: `_ensure_hourly_market_semantic_accuracy_contract_marker` in `storage/migrations.py` writes the marker via `INSERT OR IGNORE` into `_migration_state` after `check_schema_health()` passes. The migration is idempotent: running it twice produces no duplicate rows and no errors. It is dirty-DB compatible: an existing dirty DB (with prior markers) receives the new marker without affecting existing rows.

**Why**: A non-idempotent migration could fail on restart, leaving the schema in an inconsistent state. The marker is the diagnostic cutoff gate — its presence MUST be guaranteed after `initialize_database()` succeeds.

---

**Last updated**: 2026-07-03 (07-03 hourly analysis semantic accuracy final seal — Sections 38.1-38.10 added for multi-TF semantics, bias-stage contract, HTF conflict cap, canonical summary, observation reason, C/D top-N labeling, 5 diagnostics + marker, data/time contract, acceptance tests, migration idempotency)

---

## 39. Hourly Decision Context Continuity Contracts (07-05)

Task `07-05-hourly-decision-context-continuity` introduces the decision-context-continuity contract: every hourly decision must carry a bounded multi-timeframe feature pack, the previous-round analysis state and structured delta, a separated candidate/effective plan lifecycle, schema-valid LLM-failure fallback, and batch-pinned data-quality. Pre-marker rows are demoted to `legacy_info`; post-marker rows are evaluated against the full contract.

### Contract 39.1: MultiTimeframeFeaturePack (Phase C)

**What**: The controller persists a `multi_timeframe_feature_pack` block on `raw_decision_json` containing per-TF compact modules (sample_count, data_as_of, bias, structure, momentum, key_levels). Raw candle arrays / OHLCV / full swing histories / skill prompt text / logs are forbidden. The serialized pack MUST fit within `FEATURE_PACK_SIZE_BUDGET_BYTES` (24 KiB default).

**Why**: Sending raw K lines to the LLM violates the highest constraint. The feature pack is the bounded-facts carrier — the LLM receives structure events, momentum scores, and key levels, never geometry. Without a size budget, downstream consumers (or a builder regression) could attach verbose text and blow past the prompt token limit.

**Diagnostic**: `_check_oversized_feature_pack` flags any row whose serialized pack exceeds the budget (`OVERSIZED_FEATURE_PACK`, severity=error post-marker).

### Contract 39.2: Analysis Continuity (Phase D)

**What**: Every decision carries an `analysis_continuity` block with the previous-round compact state and a structured delta (grade/bias/stage change, trigger_progress, new/cleared reason codes). The controller calls `build_analysis_continuity` before persistence. The deterministic continuity gate consumes `confirmed`/`invalidated` trigger status — never just records the previous id.

**Why**: Without continuity, each analysis is an island. The LLM cannot see prior thesis, and the deterministic gate cannot detect a confirmed/invalidated trigger. Recording only the previous id (as before Phase D) is audit-only and provides no decision value.

**Diagnostic**: `_check_missing_analysis_continuity` flags any row lacking the block (`MISSING_ANALYSIS_CONTINUITY`, severity=warning post-marker).

### Contract 39.3: Plan Lifecycle Separation (Phase E)

**What**: Decisions carry `candidate_trade_plan` (deterministic geometry output), `trade_plan` (post-gate executable plan), `plan_status` (`not_generated` / `candidate` / `executable` / `withheld` / `invalidated`), and `plan_blockers` (structured reason codes). On LLM failure, the candidate is preserved; `trade_plan=None`; `plan_status="withheld"`; `plan_blockers` includes `llm_parse_failed`.

**Why**: Before Phase E, an LLM parse failure collapsed to "缺交易计划" — the audit trail was lost. The report could not distinguish "no candidate generated" from "candidate withheld by gate". PRD Fact 4 requires the report to surface "候选计划已生成但被 LLM failure + grade hysteresis 阻断".

**Diagnostics**:
- `_check_missing_candidate_on_llm_failure` flags `llm_status=failed` rows without `candidate_trade_plan` (`MISSING_CANDIDATE_ON_LLM_FAILURE`, severity=error).
- `_check_withheld_without_blockers` flags `plan_status=withheld` rows with empty `plan_blockers` (`WITHHELD_WITHOUT_BLOCKERS`, severity=warning).
- `_check_candidate_effective_plan_mismatch` flags candidate/effective plan disagreements on side/entry/stop (`CANDIDATE_EFFECTIVE_PLAN_MISMATCH`, severity=error).

### Contract 39.4: LLM Fallback Contract (Phase G)

**What**: `no_edge_decision(symbol, reason, *, analysis_time_utc, timeframe_context=None)` requires keyword-only strict-positive-int `analysis_time_utc`. The bounded JSON extractor (`_parse_json_object`) strips Markdown fences and extracts exactly one JSON object; control characters intentionally FAIL parse so the deterministic candidate plan is preserved. `_llm_parse_meta` (retry_count, error_category, fenced, extracted) propagates to the persisted decision for diagnostics.

**Why**: PRD Fact 5 — the no_edge fallback must be schema-valid even when triggered by a schema-invalid upstream payload. PRD Fact 4 — control-character failures must NOT be repaired away, otherwise the candidate plan is silently discarded.

**AST guard**: `_check_no_2_arg_no_edge_decision_in_production` (in test_smoke.py) walks the production AST and fails if any production caller passes `analysis_time_utc` positionally.

### Contract 39.5: Batch-Pinned Health (Phase B)

**What**: The hourly report's market-data-quality rendering uses `batch.analysis_time`, never the wall-clock. A success batch must have all completed symbols `ready=True` at the batch time. Stale/gap/insufficient failures still surface fail-closed.

**Why**: PRD Fact 1 — a batch ending at 19:59:59 reported at 20:15 was incorrectly flagged stale because the report compared the batch close to the wall-clock 20:14:59.

**Diagnostic**: `_check_batch_time_health_mismatch` flags success batches whose symbols are not `ready=True` pinned to `batch.analysis_time` (`BATCH_TIME_HEALTH_MISMATCH`, severity=error post-marker).

### Contract 39.6: Recent-Failed-Jobs Window (PRD FR-8)

**What**: Failed `analysis_batches` older than `FAILED_JOBS_RECENT_WINDOW_DAYS` (default 7) are surfaced as `legacy_info`, not `error`. The diagnostic distinguishes current errors, warnings, and legacy history.

**Why**: PRD FR-8 — historical failures must not permanently repeat as "current risk" in the hourly report. The window forces aging-out.

**Diagnostic**: `_check_failed_jobs_outside_window` (`FAILED_JOBS_OUTSIDE_WINDOW`, severity=legacy_info always).

### Contract 39.7: Marker and Cutoff (Phase H deployment)

**What**: `_ensure_hourly_decision_context_continuity_contract_marker` writes `hourly_decision_context_continuity_contract_v1` into `_migration_state` after `check_schema_health()` passes. The marker is independent of the R4 and semantic-accuracy markers. `_apply_continuity_marker_cutoff` demotes pre-marker findings to `legacy_info`. The marker-missing check (`PLAN_LIFECYCLE_CONTRACT_MARKER_MISSING`) surfaces the absence as an error.

**Why**: A single cutoff for all seven Phase A-G contract diagnostics avoids cross-contamination with prior fix windows. Without the marker, every legacy row would fail every Phase A-G check, drowning the signal.

**Test coverage**: `TestPhaseH07_05DiagnosticsAndReportUX` (13 tests) covers each diagnostic's positive and negative cases plus the marker deployment and pre-marker demotion.

### Contract 39.8: Report UX — Explicit Plan Blockers and LLM Status (Phase E + Phase H)

**What**: The hourly report renders explicit plan blocker summaries from `plan_blockers` (LLM 解析失败 / 风控未通过（…）/ 前次触发已被反转 / etc.) and surfaces LLM status as "候选计划已生成但被 LLM 失败阻断执行。" rather than collapsing to "缺交易计划".

**Why**: PRD FR-8 — observation items must not uniformly degrade to "交易计划尚未形成". The operator must see the real blocking stage to act.

**Implementation**: `plugins/crypto_guard/notify/hourly_report.py:_format_decision_card_for_audit` (and the `_format_opportunity_row` path) consume `candidate_trade_plan` + `plan_blockers` + `llm_status` directly from `raw_decision_json`.

## 40. LLM Retry + Hourly Analysis Accuracy Contracts (07-07)

### Contract 40.1: LLM Error Taxonomy (FR-1)

**What**: `ga_decisions.raw_decision_json` must persist the following fields (never API keys/headers/secrets):
- `llm_status`: `ok | failed | disabled`
- `llm_error_category`: `llm_config_error | llm_transport_error | llm_rate_limited | llm_empty_response | llm_json_parse_failed | llm_schema_validation_failed | llm_semantic_validation_failed`
- `llm_error_stage`: `call | parse | schema | semantic | retry_exhausted`
- `llm_error`: error summary (<=300 chars)
- `llm_attempt_count`: `0-3`. `0` only when LLM not called (`disabled`, `circuit_breaker_open`, `skipped_by_policy`); `1-3` for actual call count.
- `llm_retry_round`: batch-level retry round (1-3) where applicable.
- `llm_config_name`, `llm_model`, `llm_fallback_reason`.

**Why**: 2026-07-07 production showed 56% LLM failure rate with coarse classification (HTTP 422 / malformed_json / empty / etc.). Without a stable taxonomy, retry policy and diagnostics can't distinguish non-retryable config errors from retryable transport errors.

**Implementation**: `plugins/crypto_guard/reasoning/llm_agent_judge.py:_classify_llm_failure` is a pure function mapping exception/raw/stage to category. The classifier inspects both the raw response and the exception message (so `RuntimeError("!!!Error: ... model not found ...")` raised by `_call_ga_llm` still classifies as `llm_config_error`). Schema validation is NOT modified; new top-level fields pass under draft 2020-12 (root has no `additionalProperties: false`). `controller_decision_from_legacy` in `plugins/crypto_guard/ga_master/decision_schema.py` propagates the fields into `raw_decision_json`.

### Contract 40.2: Bounded Retry + Wall-Clock Budget (FR-2)

**What**: Per symbol max 3 attempts; per batch max 9 retry recovery calls; **batch wall-clock budget default 90s (configurable 60-180s)** covers Attempt 1 + all retries + jitter. **Before every LLM call, including Attempt 1**, the wrapper checks: (1) breaker `should_call()`; (2) wall-clock remaining > estimated call + jitter; (3) for retry only, retry budget remaining > 0. Exits:
- `circuit_breaker_open` (skip LLM, `attempt_count=0`)
- `wall_clock_budget_exhausted` (primary scheduler safeguard)
- `retry_budget_exhausted` (9-call quota exhausted)
- `non_retryable_error` (config/schema/semantic error - does NOT consume retry quota)
- `retry_exhausted` (3 attempts all failed)
- `schema_validation_failed` (post-parse schema reject)

Non-retryable: `llm_config_error`, `llm_schema_validation_failed`, `llm_semantic_validation_failed`. Retryable: `llm_empty_response`, `llm_transport_error`, `llm_rate_limited`, `llm_json_parse_failed` (strict JSON prompt may repair).

**Why**: Without the wall-clock check on Attempt 1, a batch arriving with budget already exhausted could still fire N symbols x 3 attempts and stall the 15m scheduler (900s cycle). The 90s hard cap is the PRIMARY scheduler safeguard; retry budget is secondary.

**Implementation**: `plugins/crypto_guard/reasoning/llm_agent_judge.py:_call_ga_llm_with_retry` returns `(candidate_or_None, attempt_meta)`. Three prompt builders: `build_llm_decision_prompt` (normal), `build_llm_strict_json_prompt` (Attempt 2, SYSTEM_PROMPT_STRICT_JSON), `build_llm_minimal_safe_prompt` (Attempt 3, safe_payload). Config under `llm.retry` in `config/trading_mode.yaml`.

### Contract 40.3: Batch Circuit Breaker (FR-3)

**What**: Batch-scoped breaker (one per batch, lifetime = one batch). States: `closed | open | half_open`. Open conditions:
- `llm_config_error`: open IMMEDIATELY (any count).
- 3 consecutive `llm_transport_error` / `llm_empty_response`: open.
- Failure rate >= 50% over latest 10 LLM calls in this batch: open.

Breaker open: remaining symbols skip LLM, `llm_fallback_reason="circuit_breaker_open"`, `llm_attempt_count=0`.

**Why**: A single broken LLM endpoint (e.g., `model not found: xopglm52`) would otherwise burn all 9 retry budget calls + 90s wall-clock per batch before failing closed. Immediate open on config error stops the bleeding.

**Implementation**: `plugins/crypto_guard/reasoning/llm_breaker.py:CircuitBreaker`. `_NullBreaker` for tests/non-controller callers. The controller wires the breaker into `context["llm_breaker"]` per-batch via a module-level cache in `plugins/crypto_guard/run_ga_workers.py` (so the same breaker persists across per-job controller instances). `breaker.snapshot()` is merged into `analysis_batches.summary_json.llm_health` at `finish_analysis_batch`.

### Contract 40.4: Plan State Model (FR-4)

**What**: Two orthogonal fields distinguish "where the plan came from" vs "what state it's in":
- `plan_origin`: `llm_confirmed | deterministic_fallback | deterministic_sop | none`
- `plan_execution_state`: `confirmed | unconfirmed | risk_rejected | invalidated | no_candidate`

LLM-failed candidates stay in `candidate_trade_plan` (preserved for audit), `has_trade_plan=False`, `trade_plan=None`, `plan_execution_state="unconfirmed"`. Controller overrides `plan_execution_state` AFTER all gates:
- `risk_rejected` when candidate was preserved but plan cleared (risk gate failed).
- `invalidated` when continuity trigger invalidated the candidate.
- `no_candidate` when neither plan nor candidate exists.

**Why**: 2026-07-07 production showed hourly reports misreading deterministic fallback candidates as "候选计划已生成" (implying LLM-confirmed and executable). The two-field model separates origin from execution eligibility.

**Implementation**: `run_agent_sop_decision` sets the fields per path (success/breaker/failed/disabled). `controller.analyze_symbol` overrides only the state label, not the decision outcome. Risk gates are NOT weakened.

### Contract 40.5: Raw Grade Caps (FR-5)

**What**: `normalize_market_semantics` applies HTF alignment caps to `signal_grade` (idempotent via `_htf_cap_original_grade` marker):
- **Step 4b**: 1D AND 4H both opposite to candidate direction -> max B, reason `htf_countertrend_cap`.
- **Step 4c**: 4H in {range, transition, mixed, unknown} -> max B, reason `htf_4h_nondirectional_cap`.
- **Step 4d**: 1H AND 15M both not aligned with candidate direction -> max B, reason `mtf_misalignment_cap`. Only 5M supports while 4H/1H don't -> max C, reason `low_tf_rebound_only_cap`, map `trend_stage` `early -> transition`, add `low_tf_rebound_only` to `market_reason_codes`.
- **Step 4e (controller, after clamp)**: no structured entry confirmation -> executable grade max B, reason `no_entry_confirmation_cap`; LLM failed -> executable grade max B + `plan_execution_state="unconfirmed"`, reason `llm_failed_executable_cap`.

**Why**: 2026-07-07 production showed DOGEUSDT raw `S/0.84` despite 1D/1H/15M bearish + 5M range; AVAXUSDT raw `S/0.84` despite 4H transition + 1H/15M range; BTCUSDT raw `A/0.79` despite 4H recovering + 1H mixed + 15M/5M range. Raw grade overheated on weak multi-TF alignment.

**Implementation**: `plugins/crypto_guard/reasoning/market_semantics.py:_apply_htf_alignment_caps` + `_cap_grade` helper. Executable caps in `ga_master/controller.py:analyze_symbol` after `clamp_grade`, before `effective_signal_grade`. Config gate `htf_alignment_cap_enabled` (default True) for rollback.

### Contract 40.6: Prompt Strategy (FR-6)

**What**: Three prompt tiers, all bounded by `MAX_PROMPT_BYTES = 48 * 1024`:
1. **Normal** (Attempt 1): `build_llm_decision_prompt` with full `multi_timeframe_feature_pack` (24 KiB budget) + modules + historical_memory + open_positions.
2. **Strict JSON** (Attempt 2): `build_llm_strict_json_prompt` overrides SYSTEM_PROMPT with `SYSTEM_PROMPT_STRICT_JSON` to force pure JSON output (no prose, no markdown fences).
3. **Minimal Safe** (Attempt 3): `build_llm_minimal_safe_prompt` drops market_snapshot/modules/feature_pack; keeps symbol + analysis_time + hard_rules + deterministic_reference + minimal output_requirements.

**What is preserved across all tiers**: `symbol`, `analysis_time_utc`, `deterministic_reference`, `timeframe_context`, `data_health`, previous compact state, risk thresholds.

**Why**: 2026-07-07 production showed `malformed_json` (39 failures) and `invalid_control_character` (9 failures). Strict JSON prompt forces the LLM to output parseable JSON. Minimal safe payload (smallest) avoids token-limit truncation on attempt 3.

**Implementation**: `plugins/crypto_guard/reasoning/llm_agent_judge.py`. No raw K-line arrays in any prompt tier.

### Contract 40.7: Hourly Report Wording (FR-7)

**What**: Hourly report renders 5 candidate-state branches via `_render_plan_state_label(decision)`:
1. "候选计划已生成（LLM 已确认）" - `plan_origin=llm_confirmed`, `plan_execution_state=confirmed`
2. "规则候选计划已生成，LLM 未确认，禁止执行" - `plan_execution_state=unconfirmed`
3. "候选计划已生成，但风控未通过" - `plan_execution_state=risk_rejected`
4. "候选计划已生成，但前次触发已反转" - `plan_execution_state=invalidated`
5. "无候选计划，本轮仅观察" - `plan_execution_state=no_candidate` or fallback

Plus:
- **LLM health summary line** (`_render_llm_health_line`): `LLM：N 个品种，成功 X，失败 Y，重试 Z；主要原因：cat=N, ...`. Breaker open: `LLM：配置/网关异常，已熔断；本批使用规则 SOP，禁止自动执行候选计划`.
- **Recent failures 24h window** (`_render_recent_failures`): only decisions with `llm_status=failed` AND `analysis_time >= now_ms - 24h`. Old failures (>24h) hidden from hourly report (AC17). The existing `_check_failed_jobs_outside_window` diagnostic (7-day legacy_info) covers the audit trail.
- **Latest complete batch selection** (`_select_latest_complete_batch`): only `status='success'` AND `enabled_count > 0` AND `completed_count == enabled_count` AND matching GA decision count == enabled_count. Running/partial batches rejected.
- **Opportunity rows**: show raw grade only when it differs from executable grade.

**Why**: 2026-07-07 production showed old failures (>7 days) permanently repeating in "最近失败", and `state_consistency` warnings (`deterministic_direction_from_failed_llm`) were misread as "候选计划已生成".

**Implementation**: `plugins/crypto_guard/notify/hourly_report.py`. `_render_plan_state_label` is the single authoritative source for candidate-state wording; `_trade_plan_summary` uses "候选计划详情" prefix to avoid duplication.

### Contract 40.8: Batch Completion Consistency (FR-8)

**What**: `finish_analysis_batch(batch_id, status, summary)` materializes `completed_symbols_json` / `failed_symbols_json` from `batch_symbol_status` INSIDE the repo method, in the same UPDATE statement. Read path (`get_analysis_batch`) prefers the materialized raw columns; `batch_symbol_status` is only used for real-time checks.

Selection criteria for hourly report: `status='success'` AND `enabled_count > 0` AND `completed_count == enabled_count` AND matching GA decision count == enabled_count. Running/partial batches do not render.

**Why**: 2026-07-07 production showed `analysis_batches.status='success'` + `completed_symbols_json=[]` inconsistency. Root cause: `finish_analysis_batch` only wrote `status` + `summary_json`, never the raw columns. `get_analysis_batch` compensated at read time, but the raw column stayed empty, breaking the `SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS` diagnostic.

**Implementation**: `plugins/crypto_guard/storage/repository.py:finish_analysis_batch` queries `batch_symbol_status` for completed/failed symbols, writes sorted JSON arrays into both raw columns. Callers in `run_ga_workers.py` need no change. The `SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS` diagnostic reads the raw column (not the read-time compensation) to catch the inconsistency.

### Contract 40.9: Diagnostics and Test Coverage (FR-7 + FR-8)

**What**: 8 new diagnostic codes in `plugins/crypto_guard/diagnostics/report_diagnostics.py`:
- `LLM_FAILURE_RATE_HIGH` - latest batch `failed / total_attempts >= 0.5` over latest 10 calls.
- `LLM_CONFIG_ERROR_DETECTED` - any 24h `ga_decisions.raw_decision_json.llm_error_category == "llm_config_error"`.
- `LLM_RETRY_EXHAUSTED` - any 24h `ga_decisions.raw_decision_json.llm_fallback_reason == "retry_exhausted"`.
- `LLM_CIRCUIT_BREAKER_OPEN` - latest batch `summary_json.llm_health.breaker_state == "open"`.
- `DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN` - any 24h `ga_decisions` with `candidate_trade_plan` non-empty AND `has_trade_plan=False` AND `plan_execution_state` not in {`confirmed`, `no_candidate`}. **Source is `raw_decision_json` data fields, NOT rendered text.**
- `RAW_GRADE_EXCEEDS_HTF_CAP` - recomputes Step 4b/4c/4d caps from `raw_decision_json.timeframe_context`; fires when `raw_signal_grade` exceeds the cap.
- `SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS` - reads raw `completed_symbols_json` column; fires when `status='success'` + raw column empty/malformed + live `batch_symbol_status` has completed rows.
- `HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH` - latest batch `status='running'` + recent `alert_outbox` `alert_type='hourly_summary'` row in last hour.

7 new fault seeds in `plugins/crypto_guard/tools/_phase_h_fault_inject.py` cover each diagnostic. Total fault injection: 16/16 verified (9 Phase H + 7 Phase I).

Test coverage: 21 targeted tests across `TestPhaseB07_07LLMRetryAndBreaker` (9), `TestPhaseC07_07PlanStateLabel` (3), `TestPhaseD07_07RawGradeCaps` (5), `TestPhaseE07_07HourlyReportAndBatchConsistency` (4). AC18-AC20 are meta/fault-injection validation (covered by `_phase_h_fault_inject.py` + `_phase_i_fresh_verify.py`), not counted in the 21 targeted tests.

**Why**: Diagnostics must be data-driven (reading `raw_decision_json` fields) not text-driven (parsing rendered report text), so renderer wording changes don't silently break the diagnostic.

---

## 41. PostgreSQL Greenfield Persistence Contract (07-16)

### 41.1 Engine And Runtime Identity

CryptoGuard is PostgreSQL-only. Runtime accepts only
`crypto_guard_app@crypto_guard`; tests accept only
`crypto_guard_test_app@crypto_guard_test`. The DSN comes from
`CRYPTO_GUARD_DATABASE_URL`, is never emitted verbatim, and has no SQLite
fallback or dual-write path.

### 41.2 Transaction Boundary

`pg_db.get_conn()` commits a clean pending transaction on scope exit and rolls
back exceptional/aborted work before returning the connection to the pool.
Repository writes use `conn.transaction()`; nested use is a savepoint and the
outer unit remains owned by the caller. PostgreSQL statement failures must not
be swallowed without rollback.

### 41.3 Schema And Initialization

`schema_postgres.sql` is the greenfield source for all 46 tables.
`initialize_database()` uses a transaction-scoped advisory lock and atomically
applies DDL, seeds, health checks, and contract markers. Healthy re-entry skips
DDL and remains idempotent. `check_schema_health()` resolves
`current_schema()` and verifies every required table plus contract columns,
indexes, and constraints.

### 41.4 Claims And Ownership

Job and batch claims use PostgreSQL row locks and `FOR UPDATE SKIP LOCKED`.
Batch claim re-validates the authoritative payload symbol exact set and stamps
one claim token/lease in the same transaction. Service ownership and attempt
allocation remain CAS/atomic operations.

### 41.5 Test Isolation And Delivery

Every real-PG test uses a unique scratch schema; tests never drop shared
`public`. Concurrency tests use independent backends in the same scratch
schema. Production cutover remains a separately authorized release: archive
SQLite, create the dedicated role/database, initialize the empty PostgreSQL
database, run Schema/State/Report diagnostics, restart, and observe three full
batches.

---

**Last updated**: 2026-07-19 (Section 41 added for the PostgreSQL greenfield cutover; Section 40 covers LLM retry and hourly analysis accuracy; Sections 39.1-39.8 cover decision-context continuity.)
