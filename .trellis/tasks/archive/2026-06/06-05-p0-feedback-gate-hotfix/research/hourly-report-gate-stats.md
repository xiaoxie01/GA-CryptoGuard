# Research: Hourly Report Gate Stats

- **Query**: Understand _fetch_account_feedback_gate_stats return structure and gate rendering in both report functions
- **Scope**: internal
- **Date**: 2025-06-05

## Findings

### Files Found

| File Path | Description |
|---|---|
| `plugins/crypto_guard/notify/hourly_report.py` | Full hourly report module (952 lines) |

### _fetch_account_feedback_gate_stats Return Structure

**Location**: `hourly_report.py` lines 391-444

**Return structure** (success case):
```python
{
    "ok": True,
    "total_checks": int,              # Number of GA decisions with gate JSON in last 24h
    "active_checks": int,             # Count where gate["active"] is truthy
    "not_passed": int,                # Count where gate["passed"] is False
    "decision_counts": dict[str, int], # e.g. {"allow_order": 5, "downgrade_to_watch": 2, ...}
    "controlled_blocked": int,        # Count where controlled_projection.would_pass is False
    "controlled_gating_factors": dict[str, int],  # e.g. {"low_confidence": 3, ...}
}
```

**Empty/error case**:
```python
{"ok": True, "total_checks": 0, "active_checks": 0, "not_passed": 0, "decision_counts": {}}
# or
{"error": str(exc), "total_checks": 0}
```

**Query** (lines 395-402): Selects `account_feedback_gate_json` from `ga_decisions` where `created_at` is within the last 24 hours and the JSON column is not NULL.

**Processing** (lines 414-432): For each row, parses the JSON and extracts:
- `gate["active"]` -> increments `active_checks`
- `gate["passed"] is False` -> increments `not_passed`
- `gate["decision"]` -> increments `decision_counts[decision]`
- `gate["controlled_projection"]` -> if `would_pass` is False, increments `controlled_blocked` and `controlled_gating_factors[gating_factor]`

### render_ga_hourly_summary Gate Section

**Location**: lines 216-235

Rendered when `account_feedback_gate` is present, has no error, and `total_checks > 0`:

```
**账户反馈门禁（近 24 小时）**
- 总检查：{total_checks}；门禁激活：{active_checks}；未通过：{not_passed}
- 决策分布：{decision_text}
- 受控模式预判会被阻止：{controlled_blocked} 次   (only if > 0)
  - 受阻因素：{factor_text}                        (only if controlled_gating_factors present)
```

### render_hourly_report_text Gate Section

**Location**: lines 574-594

Identical rendering logic to `render_ga_hourly_summary`. Same fields, same conditional display.

### Both Functions Accept the Same Parameter

Both `render_ga_hourly_summary()` (line 99) and `render_hourly_report_text()` (line 461) accept:
```python
account_feedback_gate: dict[str, Any] | None = None,
```

And both check `account_feedback_gate and not account_feedback_gate.get("error")` before rendering.

### How Gate Stats Are Fetched

In `build_hourly_report()` (line 56):
```python
account_feedback_gate = _fetch_account_feedback_gate_stats(repo)
```

The result is passed to both render functions (lines 78 and 80) and also included in the raw return dict (line 75).

## Caveats / Not Found

- The gate section only renders if `total_checks > 0`. If there are no GA decisions with gate JSON in the last 24 hours, the section is silently omitted.
- The `controlled_blocked` and `controlled_gating_factors` fields are only populated when `controlled_projection.would_pass` is False -- these represent shadow mode "would have blocked" statistics.
