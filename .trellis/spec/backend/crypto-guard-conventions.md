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

**Last updated**: 2026-06-01
