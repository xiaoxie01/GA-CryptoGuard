# P1 Shadow Failure + Daily Review + LONG Gate

## Goal

Transform the self-evolution loop from "少犯错" to "越学越好" by:
1. Shadow failure reflection: force failure analysis when candidates underperform
2. Daily review structured feedback: convert losses into actionable rule candidates
3. LONG gate: block low-quality LONG entries based on historical performance

## What I already know

- P0 hard gates are in place (late stage, RSI, order flow, chanlun, account risk)
- Shadow testing uses pseudo_r when real pnl_r is unavailable
- Daily review writes to skill_feedback_memory but feedback is too generic
- LONG direction has historically dragged account performance
- Current feedback_rules.yaml exists but is never programmatically executed
- Context builder injects skill_feedback_memory into GA context

## Decision (ADR-lite)

### Context
Need to transform self-evolution from "少犯错" to "越学越好". Current system blocks bad trades (P0) but doesn't learn from failures.

### Decisions

1. **Schema approach**: Hybrid — add `pattern_type` column to skill_feedback_memory, keep detailed info in `suggested_adjustment_json`

2. **LONG gate behavior**: Soft downgrade to watch_only (not hard block). Preserves signal for manual review.

3. **Shadow failure handling**: Auto-generate draft patches, require human approval before activation. Rate-limited (max 2 retries per trigger, 24h cooldown).

### Consequences

- Hybrid schema requires migration but enables future rule matching
- Soft LONG gate may create many watches but avoids missing valid opportunities
- Draft patches require human review cycle but prevent runaway evolution

## Open Questions

- What specific failure patterns should trigger reflection?
- How many samples before shadow failure is conclusive?
- Should LONG gate apply to all symbols or only historically bad ones?

## Requirements (evolving)

### P1-A: Shadow Failure Reflection

**Trigger conditions** (any):
- avg_r < 0 AND sample_count >= 20
- win_rate < 45% AND sample_count >= 20
- drawdown恶化 > 20% vs active version
- pseudo_only + samples >= 20 (blocked from promotion, should trigger reflection)

**Actions**:
1. Generate failure report with:
   - `failure_pattern`: classified from loss_classifier patterns
   - `affected_symbols`: symbols with losses
   - `affected_sides`: LONG/SHORT breakdown
   - `regime_context`: market regime during failures
   - `suggested_rule_change`: from feedback_rules.yaml matching

2. Write structured entry to skill_feedback_memory:
   - `pattern_type`: e.g., "late_stage_long_loss", "overextension_chase"
   - `suggested_adjustment_json`: detailed failure context

3. Mark old candidate:
   - `strategy_versions.status = 'rejected'`
   - `change_reason = 'shadow_failure_reflection'`

4. Generate draft candidate patch:
   - `strategy_patches.status = 'draft'`
   - `requires_human_approval = true`
   - Rate-limited: max 2 drafts per original trigger

### P1-B: Daily Review Structured Feedback

**Schema change**: Add to skill_feedback_memory:
- `pattern_type TEXT` — matches feedback_rules.yaml `when` conditions
- `affected_symbols TEXT` — JSON array of symbols
- `affected_sides TEXT` — JSON array of sides

**Writing path changes**:
1. `_write_skill_memory_updates()` in daily_reviewer.py:
   - Classify losses by failure_pattern
   - Write one entry per pattern_type (not per skill)
   - Include affected_symbols and affected_sides

2. `_maybe_write_skill_feedback()` in runner.py:
   - Use pattern_type when detecting anomalies
   - Link to feedback_rules.yaml conditions

3. Shadow testing rejection:
   - Write feedback entry with pattern_type from verdict

**Context builder changes**:
- `_build_memory_section()`: Weight entries by relevance (symbol match, recency)
- Limit to 5 most relevant entries per skill (not 3 arbitrary)

### P1-C: LONG Gate

**Conditions for soft downgrade** (any triggers watch_only):
- HTF bias not in {bullish, neutral_bullish}
- trend_stage in {late, exhausted}
- momentum in {exhausted, overextended}
- range/chop market structure with trend-type LONG entry
- BTC context risk_off
- symbol+side historical avg_r < 0 (last 20 trades)
- BTCUSDT/LTCUSDT/ETHUSDT LONG with cooldown active

**Implementation location**: risk_engine.py
- New function: `long_quality_gate(decision, snapshot, repo)`
- Called from `validate_trade_plan()` when side == "LONG"
- Returns: `{ok, reasons, downgrade_to_watch: true}`

**Integration with risk_engine**:
- If gate fails: set `has_trade_plan = False`, `decision = "monitor_only"`
- Add to `risk_notes`: "LONG 质量门禁未通过：{reasons}"
- Create opportunity_watch with LONG conditions

## Acceptance Criteria (evolving)

### P1-A: Shadow Failure Reflection
- [ ] Shadow candidates with avg_r < 0 AND samples >= 20 trigger failure reflection
- [ ] Failure reflection writes structured entry with pattern_type to skill_feedback_memory
- [ ] Old candidate marked as rejected with change_reason
- [ ] Draft candidate patch generated with status='draft'
- [ ] Max 2 drafts per original trigger (rate-limiting works)

### P1-B: Daily Review Structured Feedback
- [ ] skill_feedback_memory table has pattern_type column
- [ ] Daily review writes entries with pattern_type matching feedback_rules.yaml
- [ ] Context builder weights entries by symbol relevance
- [ ] LLM prompt includes structured historical memory

### P1-C: LONG Gate
- [ ] LONG entries with HTF bias not bullish are downgraded to watch_only
- [ ] LONG entries with trend_stage=late are downgraded to watch_only
- [ ] LONG entries with historical avg_r < 0 are downgraded to watch_only
- [ ] BTCUSDT/LTCUSDT/ETHUSDT LONG with cooldown are downgraded to watch_only

### General
- [ ] All new tests pass
- [ ] No breaking changes to existing flows
- [ ] Config parameters added to trading_mode.yaml

## Definition of Done

- Tests added/updated
- Config parameters added to trading_mode.yaml
- Conventions.md updated if new patterns
- No breaking changes to existing flows

## Out of Scope (explicit)

- Full rule engine execution (just structured storage for now)
- Automatic parameter tuning (manual confirmation required)
- Real-time performance dashboard

## Technical Notes

### Current Implementation State

- **Shadow testing**: `plugins/crypto_guard/strategy/shadow_testing.py`
  - `_stats()` returns `data_source: "real_pnl"` or `"pseudo_r_from_score"`
  - P0 hard gate blocks promotion when `data_source == "pseudo_r_from_score"`
  - Verdict cascade: sample check → data quality gate → performance comparison → LLM review
  - **Gap**: No feedback memory written on rejection/promotion

- **Daily review**: `plugins/crypto_guard/review/daily_reviewer.py`
  - `_write_skill_memory_updates()` writes one entry per skill (5 entries)
  - Finding is generic: "每日复盘：发现 X 笔亏损..."
  - **Gap**: No structured pattern_type, affected_symbols, or rule matching

- **Skill feedback memory**: `skill_feedback_memory` table
  - Schema: id, skill_name, skill_version, feedback_type, source_type, source_id, finding, suggested_adjustment_json, status
  - Status never transitions from 'candidate'
  - **Gap**: No structured fields for rule matching (pattern_type, affected_symbols, etc.)

- **Context builder**: `ga_master/context_builder.py`
  - `_skill_feedback_memory()` returns top 50 candidate/active entries
  - `_build_memory_section()` groups by skill_name, extracts top 3 per skill
  - **Gap**: Treats all feedback types uniformly, no weighting by relevance

- **Feedback rules**: `skills/<skill_name>/feedback_rules.yaml`
  - Loaded but never evaluated programmatically
  - Rules like `false_breakout_loss -> increase_confirmation_requirement`
  - **Gap**: No matching engine, no action binding

### Key Design Decisions Needed

1. **Schema change vs JSON encoding**: Add structured columns to skill_feedback_memory or encode structure in suggested_adjustment_json?
2. **LONG gate scope**: Block (hard) vs downgrade (soft) vs watch_only?
3. **Shadow failure auto-reject**: Should failures auto-generate new candidate patches?

### Files to Modify

| File | Change |
|------|--------|
| `strategy/shadow_testing.py` | Add failure reflection + feedback memory writes |
| `review/daily_reviewer.py` | Structured feedback with pattern_type |
| `storage/schema.sql` | Add structured columns to skill_feedback_memory |
| `storage/repository.py` | New methods for structured feedback queries |
| `risk/risk_engine.py` | LONG gate integration |
| `config/trading_mode.yaml` | New config parameters |
| `tests/test_smoke.py` | New test cases |
