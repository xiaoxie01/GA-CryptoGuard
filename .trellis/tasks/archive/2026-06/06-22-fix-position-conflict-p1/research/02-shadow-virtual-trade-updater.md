# Research: shadow_virtual_trade_updater.py Audit (untracked)

- **Query**: Audit new untracked file against spec requirements 1-10
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Key Diff Summary

This is a completely new file (597 lines) that implements per-candle sequential replay for shadow virtual trades. It is the core engine for tracking candidate strategy virtual trades independently from paper trades.

### Major Components

1. **`update_shadow_virtual_trades`** (main entry): Iterates all open/pending shadow_virtual_trades, fetches candles from `last_processed_candle_time` (or opened_at/created_at), and processes each candle individually in time order.

2. **`activate_pending_entry`**: Checks if a pending_entry trade's entry condition is met (limit/trigger/stop) and transitions to open.

3. **`check_sl_tp`**: Checks stop-loss and take-profit on a single candle. Conservative rule for same-candle SL+TP: SL wins.

4. **`_process_candle_for_trade`**: Processes a single candle for a single trade, handling:
   - pending_entry: expiry check, activation, same-candle SL/TP after activation
   - open: unrealized PnL update, SL/TP check, max hold time check

5. **`_persist_cursor`**: Stores `open_time + 60000ms` (start of NEXT 1m candle) to prevent re-processing.

6. **`_get_replay_start`**: Priority: last_processed_candle_time > opened_at > created_at.

7. **`_fetch_candles_from`**: Fetches 1m candles with pagination for gap catch-up (up to 500 candles).

### Spec Compliance

| Spec | Status | Notes |
|------|--------|-------|
| #1 1 GA decision -> >=1 shadow eval + >=1 shadow VT per candidate | PASS | Creates VT per candidate, independent lifecycle |
| #2 Shared compute_fill_price + compute_position_size | PASS | Uses shared functions from paper_broker.py |
| #3 Per-candle replay: only is_closed=True, pagination for gaps >500, network error cursor preservation | PASS | is_closed filter at line 528, pagination at line 475, cursor preserved on fetch failure at line 457 |
| #4 Activation candle path ambiguity: TP-only -> activation_ambiguous_path, SL+TP -> conservative SL | PASS | Lines 320-327 implement this exactly |
| #5 TTL and event time consistency (no datetime.now() in historical replay) | PASS | Uses candle event time (`candle_dt`) for expiry/hold checks, falls back to wall clock only when candle_dt is None |
| #10 close_shadow_virtual_trade sets correct outcome_source | PASS | In repository.py, close_shadow_virtual_trade sets outcome_source='real_pnl' or 'ambiguous_path' |

### Gaps / Issues

- **Minor**: The `_single_candle_from_mark` function uses `datetime.now(timezone.utc)` for the mark price snapshot timestamp (line 75). This is acceptable because mark price snapshots are point-in-time and have `is_closed=False`, so they never advance the cursor. The spec says "no datetime.now() in historical replay" -- this is not historical replay, it's a live mark price snapshot.

### Related Specs

- `.trellis/spec/backend/crypto-guard-conventions.md` -- #1 Analysis Cycle, #10 Strategy Evolution Pipeline, #14 Shadow Testing Data Quality
