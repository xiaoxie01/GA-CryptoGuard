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

### Contract 25.1: Batch completion gate — report MUST wait for all symbols

**What**: The scheduler registers an `analysis_batches` row with `batch_id = f"{primary_interval}:{analysis_time}"` at enqueue time. Each symbol is tracked as `completed` or `failed`. The report renderer MUST poll until all enabled symbols are resolved or a timeout (default 300s) is reached. Incomplete reports are marked with `incomplete=true`.

**Why**: Without a batch gate, the report could render mid-cycle — some symbols had fresh decisions while others still showed stale rows from the previous cycle. This produced "phantom opportunities" that no longer existed.

**Signatures**:
```python
# repository.py
def start_analysis_batch(self, batch_id, primary_interval, analysis_time, enabled_symbols) -> None:
def mark_batch_symbol_completed(self, batch_id, symbol) -> None:
def mark_batch_symbol_failed(self, batch_id, symbol) -> None:
def finish_analysis_batch(self, batch_id) -> None:
def get_analysis_batch(self, batch_id) -> dict | None:
def latest_analysis_batch_id(self, primary_interval) -> str | None:

# hourly_report.py
def _await_batch_completion(repo, batch_id, *, timeout_seconds=300) -> dict:
    """Polls analysis_batches until all symbols completed/failed or timeout.
    Returns {complete: bool, incomplete: bool, missing_symbols: [...]}"""
```

**Scheduler wiring**: `enqueue_market_analysis` in `cron_scheduler.py` creates the batch row. `run_ga_workers.py` marks each symbol completed/failed on job resolution. When a symbol is skipped (already pending), `mark_batch_symbol_completed` is called immediately to prevent the batch from hanging in "running" state forever.

**Database**: `analysis_batches` table with columns `batch_id, primary_interval, analysis_time, status, enabled_symbols_json, completed_symbols_json, failed_symbols_json, started_at, completed_at`. `ga_decisions` references `batch_id`.

**Forbidden**:
```python
# WRONG: Render report without checking batch completion
decisions = repo.latest_ga_decisions_by_symbol()

# CORRECT: Wait for batch, then filter
batch_state = _await_batch_completion(repo, batch_id)
decisions = repo.latest_ga_decisions_by_symbol(min_analysis_time=analysis_time)
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
    from plugins.crypto_guard.notify.report_consistency import execution_eligible
    grade = str(decision.get("signal_grade") or "").upper()
    if grade in {"S", "A", "B"}:
        if execution_eligible(decision) and not _is_stale_decision(decision):
            return "executable_opportunity"
        return "observation_candidate"
    return "no_edge"
```

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
)
```

**Override clause**: When phrases are stripped, `仅观察/未通过执行门禁：{gate_blockers}` is appended once, where `_gate_blockers` lists the specific failing conditions (missing trade_plan, risk reasons, low confidence).

**Integration points**:
1. `controller.py analyze_symbol()` — applies `rewrite_inconsistent_summary` before persistence
2. `llm_agent_judge.py _normalize_llm_decision` — applies for non-LLM path
3. `hourly_report.py render_ga_hourly_summary` — double-check at render time

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

**What**: `grade_with_hysteresis` in `strategy/grade_config.py` dampens large single-cycle grade jumps. When `grade_delta >= GRADE_UP_BUFFER` (default 2, e.g. D→S), the grade is clamped to one step above the previous grade (D→C instead of D→S). `clamp_grade` prevents S-grade when 4H=range/transition without `independent_trend` evidence, and limits counter_evidence items to `SA_MAX_COUNTER_EVIDENCE` (default 3).

**Why**: An S→D→S oscillation within two cycles indicates instability, not a genuine signal change. Hysteresis prevents whipsaw-induced false opportunities from entering the executable tier.

**Signatures**:
```python
def grade_with_hysteresis(current_grade: str, previous_grade: str | None) -> str:
    """Dampen large jumps. D→S becomes D→C; S→D becomes S→B."""

def clamp_grade(grade: str, market_bias_4h: str | None,
                counter_evidence: list | None,
                independent_trend: bool | None) -> str:
    """Prevent S when 4H=range/transition without independent_trend.
    Cap counter_evidence to SA_MAX_COUNTER_EVIDENCE items."""

def grade_delta(current: str, previous: str | None) -> int:
    """Compute signed delta between grades. S=5, A=4, B=3, C=2, D=1."""
```

**Previous grade source**: `previous_ga_decision_grade(exclude_batch_id=)` skips current batch decisions to avoid same-batch contamination. The controller passes `exclude_batch_id=request.batch_id`.

**Database**: `ga_decisions.previous_grade` column stores the grade used for hysteresis calculation.

---

### Contract 25.5: Report diagnostics — 10 P2 checks for accuracy

**What**: `diagnose_report_accuracy(repo)` in `diagnostics/report_diagnostics.py` runs 10 checks covering the known issue categories. It returns the standard `{ok, issues, summary, total_issues}` shape so it can be merged into `diagnose_state_consistency` output or rendered standalone.

**Issue codes**:
| Code | Severity | What it checks |
|------|----------|---------------|
| `hourly_report_incomplete_batch` | warning | Running batches with missing symbols |
| `hourly_report_stale_decision` | warning | Decisions older than one 15m cycle |
| `executable_opportunity_without_trade_plan` | warning | S/A/B grade missing trade_plan |
| `executable_opportunity_risk_rejected` | warning | S/A/B grade with risk_check=false |
| `opportunity_below_confidence_threshold` | warning | S/A/B grade below min_confidence |
| `summary_execution_state_conflict` | error | Forbidden phrases in summary despite gate failure |
| `excessive_grade_flip` | warning | S/A→D/C within 4 hours |
| `direction_flip_without_closed_candle` | warning | Direction flip without closed candle evidence |
| `invalid_liquidity_sweep_semantics` | warning | sell_side paired with bearish or buy_side with bullish |
| `negative_drawdown_display` | warning | Positive drawdown_percent when equity shows loss |

**Integration**: `run_for_report(repo)` wraps the diagnostic call with a never-raises guarantee for render-time use.

**Drawdown sign convention**: Internal `_drawdown_percent` returns negative values for losses. External display must be non-negative (e.g. "回撤 0.50%"). The diagnostic uses `initial_balance` from `paper_accounts` for relative comparison, not a hardcoded threshold.

---

**Last updated**: 2026-06-28 (P0: Hourly report accuracy — batch completion gate, opportunity classification, deterministic text override, grade hysteresis, report diagnostics)
