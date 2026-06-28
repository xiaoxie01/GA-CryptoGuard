# P0: Stabilize Feedback Gate and Configuration

## Goal

Fix 7 confirmed defects in the account feedback gate and configuration before adding
any new strategy behavior. These defects block controlled execution, produce false
symbol-side matches, lose observability data, and create schema drift.

## Confirmed Defects (verified by code inspection 2026-06-05)

### D1: trading_mode.yaml hierarchy broken

**File**: `config/trading_mode.yaml`

`min_r_count_for_performance_gate`, `online_shadow`, and `stale_cleanup` are nested
under `account_feedback_rules.actions` / `account_feedback_rules`. They must live under
`evolution`.

Current (wrong):
```yaml
account_feedback_rules:
  actions:
    require_stronger_confirmation: ...
    min_r_count_for_performance_gate: 5
  online_shadow: ...
  stale_cleanup: ...
```

Required:
```yaml
evolution:
  min_r_count_for_performance_gate: 5
  online_shadow: ...
  stale_cleanup: ...
account_feedback_rules:
  actions:
    require_stronger_confirmation: ...
```

### D2: Not every gate check persisted

**File**: `paper/paper_broker.py` line 65

`if feedback_gate.get("active"):` skips persistence for `disabled`, `action_disabled`,
`no_recent_pattern`, `not_affected` results. Every broker entry must persist a gate
result for accurate shadow reporting.

### D3: Missing entry_quality passes quality threshold

**File**: `risk/account_feedback_gate.py` line 144

```python
quality_ok = entry_quality is None or entry_quality >= min_entry_quality
```

When `entry_quality` is None, the check passes. In shadow mode this is acceptable
(log as `data_quality_insufficient`). In controlled mode, missing required evidence
must fail closed.

### D4: Symbol-side cross-product false positives

**File**: `risk/account_feedback_gate.py` lines 108-119

`affected_symbols` and `affected_sides` are independent sets. A trade for BTCUSDT/LONG
and another for ETHUSDT/SHORT produce `{BTCUSDT, ETHUSDT}` × `{LONG, SHORT}`, matching
BTCUSDT/SHORT incorrectly.

Must use paired `[{symbol, side}]` records.

### D5: Controlled decisions logged but not enforced

**File**: `paper/paper_broker.py`

When `mode=controlled` and gate fails:
- `downgrade_to_watch` must prevent order creation and create/update opportunity watch
- `block_order` must prevent order creation with explicit audit reason
- Shadow mode always records `would_decide` for comparison

### D6: schema.sql missing account_feedback_gate_json

**File**: `storage/schema.sql`

`ga_decisions` table in `schema.sql` does not include `account_feedback_gate_json TEXT`.
Migration adds it, but fresh schema.sql databases won't have it. Schema-health check
must also cover it.

### D7: No broker integration tests

Current tests only verify `check_account_feedback_gate()` in isolation. Need tests
proving the real broker path (`create_paper_order_from_signal`,
`create_paper_order_from_ga_decision`) persists and applies gate results.

## Requirements

### R1: Config hierarchy

- Move `min_r_count_for_performance_gate`, `online_shadow`, `stale_cleanup` under
  `evolution` in `trading_mode.yaml`
- Add config-shape regression test that parses YAML and asserts key locations
- Existing tests referencing these keys must still pass with correct thresholds

### R2: Persist every gate check

- `paper_broker.py`: always call `_save_gate_result_to_ga_decision()` regardless of
  `active` status
- Gate result JSON must include `decision`, `active`, `passed`, `mode`, `events_matched`
- Report statistics must reflect all check types

### R3: Missing entry_quality fails closed

- In shadow mode: `quality_ok = True` but annotate as `data_quality_insufficient`
- In controlled mode: `quality_ok = False` (missing required evidence fails closed)
- Log when entry_quality is None

### R4: Paired symbol-side records

- Replace `_get_affected_symbols_sides()` return type from
  `tuple[list[str], list[str]]` to `list[dict[str, str]]` (each dict is
  `{"symbol": ..., "side": ...}`)
- Gate membership check uses exact pair match, not cross-product
- Persist paired records in gate result JSON for audit

### R5: Enforce controlled decisions

- `annotate_only`: no behavior change (current shadow behavior)
- `downgate_to_watch`: return `{"ok": False, "error": "gate_blocked", "decision":
  "downgrade_to_watch"}` and create opportunity watch
- `block_order`: return `{"ok": False, "error": "gate_blocked", "decision":
  "block_order"}`
- Shadow mode always records `would_decide` field in gate result

### R6: Schema synchronization

- Add `account_feedback_gate_json TEXT` to `schema.sql` `ga_decisions` table
- Add to `check_schema_health()` required columns
- Migration remains idempotent (existing `_add_column` handles already-present column)

### R7: Broker integration tests

- Test `create_paper_order_from_signal()` with gate active → result persisted
- Test `create_paper_order_from_ga_decision()` with gate active → result persisted
- Test controlled mode `downgrade_to_watch` → order blocked, watch created
- Test controlled mode `block_order` → order blocked with audit reason
- Test shadow mode → order proceeds, gate result persisted

## Files to Change

| File | Changes |
|------|---------|
| `config/trading_mode.yaml` | Fix hierarchy, add `affected_scope` |
| `risk/account_feedback_gate.py` | Paired symbol-side, entry_quality logic, controlled decisions, persist all |
| `paper/paper_broker.py` | Always persist gate, enforce controlled decisions |
| `storage/schema.sql` | Add `account_feedback_gate_json` to `ga_decisions` |
| `storage/migrations.py` | Add to `check_schema_health()` |
| `tests/test_smoke.py` | Config shape test, controlled mode tests, broker integration tests |

## Acceptance Criteria

- [ ] Parsed config proves all evolution keys remain under `evolution`
- [ ] 100% of attempted broker checks persist a gate result
- [ ] Missing entry quality never counts as passing stronger confirmation in controlled mode
- [ ] Symbol-side cross-product false positives are impossible
- [ ] Controlled-mode tests prove order suppression / watch conversion
- [ ] Existing evolution and stale-cleanup tests retain configured thresholds
- [ ] Full test suite passes (149+ tests)
- [ ] `diagnose_state_consistency = 0`
- [ ] `check_schema_health()` passes
- [ ] `schema.sql` fresh database parity with migration

## Implementation Order

1. Fix `trading_mode.yaml` hierarchy + config shape test
2. Fix `account_feedback_gate.py` (paired symbol-side, entry_quality, controlled)
3. Fix `paper_broker.py` (always persist, enforce controlled decisions)
4. Fix `schema.sql` + `check_schema_health()`
5. Add broker integration tests
6. Run full test suite + diagnostics

## Rollback

All changes are additive or corrective. Revert by checking out the previous commit.
No data migration that requires rollback — only schema additions.
