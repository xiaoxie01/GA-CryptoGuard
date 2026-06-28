# P2 Bug Fixes: Schema Migration, Time Comparison, Diagnostics Coverage, TTL Protection

## Goal

Fix 6 critical bugs in P2 implementation before enabling controlled rule execution. These bugs affect production data integrity, diagnostic accuracy, and feedback protection.

## Issues Found (from production audit)

### Issue 1: Missing Schema Migration in Production DB
**Problem**: Production `skill_feedback_memory` lacks `pattern_type / affected_symbols / affected_sides` columns, causing dry-run to fail with `no such column: pattern_type`.

**Impact**: 130k+ feedback entries cannot enter structured rule matching. Self-evolution "memory → rules" chain is broken.

**Fix**: Ensure `initialize_database()` is called at service startup / tool entry. Add "production schema health check" that verifies all required columns exist.

### Issue 2: State Diagnostics Coverage Gaps
**Problem**: `diagnose_state_consistency()` doesn't detect:
- `active` patch with `deprecated` strategy_version (v2-trigger-4 case)
- 1719 duplicate patches in production

**Impact**: State machine diagnostic misses critical inconsistencies. Auto-rules may run on incorrect state.

**Fix**: Expand diagnostics to cover patch/version/trigger three-table state matrix. Add duplicate patch detection.

### Issue 3: Time Field String Comparison Mismatch
**Problem**: Code compares ISO `2026-...T...Z` with SQLite `CURRENT_TIMESTAMP` format `YYYY-MM-DD HH:MM:SS`. Same-day data may be incorrectly excluded.

**Affected locations**:
- `feedback_rules_dry_run.py:47`
- `hourly_report.py:269, 310`

**Fix**: Use `datetime(column) >= datetime(?)` in SQL or parse both sides with Python datetime.

### Issue 4: Feedback TTL Protection Scope Too Narrow
**Problem**: Only parses `evidence_json.feedback_ids`. Doesn't parse `patch_json` or recognize `feedback_id / source_feedback_ids` references.

**Impact**: Active experiment references may still be archived.

**Fix**: Extend `_get_protected_feedback_ids()` to parse both `evidence_json` and `patch_json`, looking for multiple reference patterns.

### Issue 5: Shadow Data Quality Misclassifies 0R as Pseudo-R
**Problem**: `pnl_r = 0` is counted as pseudo-R, but 0R could be a real breakeven trade.

**Impact**: Hourly report underestimates real shadow sample quality.

**Fix**: Change query to only count as pseudo-R when `pnl_r IS NULL` (not when `pnl_r = 0`).

### Issue 6: Feedback Rules Loading Overwrites
**Problem**: Both `momentum` and `momentum_skill` normalize to `momentum`. Last loaded overwrites first.

**Fix**: Either:
- Merge rules when same skill name encountered
- Or skip if skill name already loaded (first wins)

## Acceptance Criteria

- [ ] Production schema health check passes (all required columns exist)
- [ ] `diagnose_state_consistency()` detects active patch + deprecated version mismatch
- [ ] `diagnose_state_consistency()` detects duplicate patches
- [ ] All time comparisons use consistent format (SQL `datetime()` or Python parsing)
- [ ] TTL protection parses both `evidence_json` and `patch_json` for feedback references
- [ ] Shadow data quality counts `pnl_r = 0` as real data, only `NULL` as pseudo
- [ ] Feedback rules loading doesn't overwrite when duplicate skill names exist
- [ ] All 131 tests still pass
- [ ] Production dry-run succeeds without column errors

## Out of Scope

- Enabling controlled rule execution (deferred to next phase)
- Changing trading behavior based on diagnostics
- Real-time alerting on state issues

## Technical Notes

- Production DB path: `data/crypto_guard.sqlite3`
- Migration code: `plugins/crypto_guard/storage/migrations.py`
- Diagnostic code: `plugins/crypto_guard/diagnostics/state_consistency.py`
- TTL code: `plugins/crypto_guard/diagnostics/feedback_ttl.py`
- Rules loading: `plugins/crypto_guard/diagnostics/feedback_rules_dry_run.py`
- Report code: `plugins/crypto_guard/notify/hourly_report.py`
