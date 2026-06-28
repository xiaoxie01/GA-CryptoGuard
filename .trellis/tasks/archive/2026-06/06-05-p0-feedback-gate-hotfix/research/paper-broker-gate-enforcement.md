# Research: Paper Broker Gate Enforcement

- **Query**: Understand _create_opportunity_watch_from_gate and controlled enforcement blocks
- **Scope**: internal
- **Date**: 2025-06-05

## Findings

### Files Found

| File Path | Description |
|---|---|
| `plugins/crypto_guard/paper/paper_broker.py` | Full paper broker module (516 lines) |

### _create_opportunity_watch_from_gate

**Location**: `paper_broker.py` lines 420-495

**Signature**:
```python
def _create_opportunity_watch_from_gate(
    repo: CryptoGuardRepository,
    symbol: str,
    side: str,
    ga_decision_id: int | None,
    gate_result: dict[str, Any],
) -> int | None:
```

**Behavior**:
1. Returns None immediately if `ga_decision_id` is None (line 438)
2. Constructs `watch_reason` as `"account_feedback_gate: {reason}"` (line 441)
3. Builds a structured `watch_condition_json` with `type: "manual_review"` (lines 447-458):
   ```python
   {
       "type": "manual_review",
       "source": "account_feedback_gate",
       "gate_decision": gate_result.get("would_decide", ""),
       "gate_reason": gate_result.get("reason", ""),
       "gate_mode": gate_result.get("mode", ""),
       "entry_quality_status": gate_result.get("entry_quality_status", ""),
       "actual_confidence": gate_result.get("actual", {}).get("confidence"),
       "actual_entry_quality": gate_result.get("actual", {}).get("entry_quality"),
       "required_min_confidence": gate_result.get("required", {}).get("min_confidence"),
       "required_min_entry_quality": gate_result.get("required", {}).get("min_entry_quality"),
   }
   ```
4. Sets 24-hour TTL on `expires_at` (line 461)
5. Uses `BEGIN IMMEDIATE` transaction for concurrent safety (line 465)
6. Deduplicates via SELECT on `(ga_decision_id, watch_reason, status='active')` (lines 466-471)
7. If existing watch found, returns its ID (line 475)
8. Otherwise INSERTs and returns new ID (lines 477-484)
9. On any exception, attempts ROLLBACK and logs warning (lines 485-495)

**Important**: This function does NOT use `repo.create_opportunity_watch()`. It does its own direct INSERT with a subset of columns:
```sql
INSERT INTO opportunity_watches
(symbol, direction, watch_reason, watch_condition_json, status, ga_decision_id, expires_at)
VALUES (?, ?, ?, ?, 'active', ?, ?)
```

### Controlled Enforcement Block -- Entry Point 1: create_paper_order_from_signal

**Location**: `paper_broker.py` lines 38-71

Flow:
1. Check account risk guard (line 29)
2. Call `check_account_feedback_gate()` (line 44)
3. **Gate enforcement** (lines 47-71):
   - Only enforces if `feedback_gate["mode"] != "shadow"` (line 47)
   - Gets decision from `would_decide` or `decision` (line 48)
   - If decision is `"downgrade_to_watch"` or `"block_order"`:
     - Resolves `ga_decision_id` (creates pending GA decision if needed, lines 51-57)
     - Saves gate result to GA decision (line 59)
     - If `downgrade_to_watch`: calls `_create_opportunity_watch_from_gate()` (line 62)
     - Returns error with `"gate_blocked"` (lines 63-71)
4. If gate passes (or shadow mode), proceeds to risk validation and order creation

### Controlled Enforcement Block -- Entry Point 2: create_paper_order_from_ga_decision

**Location**: `paper_broker.py` lines 114-209

Flow:
1. Validate GA decision exists and has `create_paper_order` action (lines 115-120)
2. Validate trade_plan completeness (lines 121-127)
3. Check account risk guard (line 130)
4. Call `check_account_feedback_gate()` (line 146)
5. **Gate enforcement** (lines 149-164):
   - Only enforces if `feedback_gate["mode"] != "shadow"` (line 149)
   - Gets decision from `would_decide` or `decision` (line 150)
   - If decision is `"downgrade_to_watch"` or `"block_order"`:
     - If `downgrade_to_watch`: calls `_create_opportunity_watch_from_gate()` (line 154)
     - Saves gate result to GA decision (line 156)
     - Returns error with `"gate_blocked"` (lines 157-164)
6. If gate passes (or shadow mode), persists gate result, validates risk, creates order

### Key Difference Between Entry Points

- `create_paper_order_from_signal`: May need to create a pending GA decision if `ga_decision_id` is not set on the signal (line 53-57). This is for legacy signal compatibility.
- `create_paper_order_from_ga_decision`: Already has a `ga_decision_id` (it's the function parameter).

### Gate Decision Values

The gate can produce these decisions (from the enforcement blocks):
- `"allow_order"` -- proceed to risk validation
- `"downgrade_to_watch"` -- block order, create opportunity watch
- `"block_order"` -- block order, no watch created

### Shadow Mode Behavior

When `feedback_gate["mode"] == "shadow"`:
- Gate enforcement is **skipped entirely** (lines 47, 149)
- The order proceeds to risk validation regardless of gate result
- Gate result is still persisted to GA decision after passing (line 92 for signal, line 167 for GA decision)
- Shadow mode statistics are reported via `controlled_projection` in the hourly report

## Caveats / Not Found

- The `_create_opportunity_watch_from_gate` function does its own INSERT rather than calling `repo.create_opportunity_watch()`. This means it bypasses the `invalid_condition_json` and `source_signal_id` columns that the repository method would set.
- The deduplication uses `(ga_decision_id, watch_reason, status='active')` -- if a watch is expired/invalidated and the same gate fires again, a new watch will be created (no cross-status dedup).
