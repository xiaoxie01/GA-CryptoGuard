# P2 State Diagnostics + Reports + Rules Dry-Run + Feedback TTL

## Goal

Make CryptoGuard system state observable, diagnosable, and maintainable without changing trading behavior. This enables long-term reliability and prevents state inconsistencies from silently breaking the evolution loop.

## What I already know

- P0: Hard gates prevent high-risk trades (account risk, late stage, RSI, order flow, chanlun)
- P1: Shadow failure reflection, structured feedback with pattern_type, LONG quality gate
- Current state inconsistencies can silently break evolution loop
- Reports don't expose risk_off, shadow data quality, or feedback patterns
- feedback_rules.yaml is loaded but never evaluated
- skill_feedback_memory grows unboundedly with no TTL

## Requirements

### P2-A: State Consistency Diagnostics

Function: `diagnose_state_consistency(repo) -> dict[str, Any]`

Checks:
1. **Orphan patches**: strategy_patches with no matching strategy_version
2. **Status mismatches**: evolution_triggers 'pending' but patch 'rejected', or version 'active' but trigger 'pending'
3. **Stale shadows**: candidates in 'shadow_testing' >7 days with no new samples
4. **Draft limbo**: patches in 'draft' >72 hours (human approval timeout)

Output: `{ok, issues: [{type, severity, details, suggested_action}]}`

### P2-B: Report Enhancements

Add to hourly report:
- Current risk_off / daily_loss_pause state
- Shadow data quality (real_pnl vs pseudo_r counts)
- Top 3 failure patterns this week (from skill_feedback_memory)
- Most active feedback skill
- LONG vs SHORT performance breakdown

### P2-C: feedback_rules.yaml Dry-Run

Function: `evaluate_feedback_rules_dry_run(repo) -> dict[str, Any]`

- Load all feedback_rules.yaml from skill directories
- Match recent feedback entries (30 days) against `when` conditions via `pattern_type`
- Output matches with `would_execute` action, but do NOT execute
- No strategy changes, no parameter modifications

### P2-D: Feedback TTL/Decay

States: `fresh` (0-30 days) → `decayed` (30-90 days) → `archived` (>90 days)

Protection:
- Feedback referenced by active strategy_patches never archived
- Recent 30 days: full weight in context builder
- 30-90 days: decayed weight (0.5x)
- >90 days: archived, excluded from context unless referenced

## Acceptance Criteria

- [ ] diagnose_state_consistency() detects orphans, mismatches, stale shadows, draft limbo
- [ ] Hourly report shows risk_off state and top failure patterns
- [ ] feedback_rules dry-run outputs matches without executing
- [ ] Feedback entries transition through fresh/decayed/archived states
- [ ] Active patch references prevent archival
- [ ] All tests pass

## Out of Scope

- Actually executing feedback rules (dry-run only)
- Changing trading behavior based on diagnostics
- Real-time alerting on state issues (just logging for now)

## Technical Notes

- State tables: strategy_versions, strategy_patches, evolution_triggers, shadow_test_results
- Feedback table: skill_feedback_memory (has pattern_type from P1)
- Report: plugins/crypto_guard/notify/hourly_report.py
- Context builder: plugins/crypto_guard/ga_master/context_builder.py
- feedback_rules.yaml: plugins/crypto_guard/skills/*/feedback_rules.yaml
