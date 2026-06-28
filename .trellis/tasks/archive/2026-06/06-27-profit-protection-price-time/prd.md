# Profit Protection, Mark Price, and UTC+8 Notifications

## Goal

Prevent profitable paper positions from giving back material unrealized gains
when a strong opposite GA signal appears, ensure all displayed market prices
are fresh Binance USD-M Futures mark prices, and include UTC+8 event times in
all paper-trading notifications.

## Confirmed Findings

1. XRPUSDT order #1 entered SHORT at 1.0494, reached approximately +1.44R MFE,
   was around +0.52R when the conflict review fired, then moved its stop only
   to breakeven and finally closed at 0R.
2. The current early-exit policy only exits an S-grade conflict after two
   actionable reverse confirmations, an adverse move of at least -0.30R, or
   signal decay of at least 0.70. A profitable conflict has no direct
   profit-protection exit rule.
3. The routine stop-adjustment payload does not contain current mark price.
   `handle_paper_event_alert()` therefore falls back to `entry_price`, causing
   the notification to label 1.0494 as generic "price" even though it is the
   entry/new-stop price.
4. Position conflict pricing accepts `paper_positions.current_price` up to
   15 minutes old and falls back to a 1h candle close. Neither is sufficiently
   fresh for a simulated close or stop adjustment.
5. Generic paper-event notifications include UTC+8 time, but conflict
   stop-adjusted and needs-recheck cards omit it.

## Required Changes

### P0: Fresh Mark Price Contract

- Financial actions (conflict exit and stop adjustment) must use Binance
  USD-M Futures mark price fetched at action time.
- Record `mark_price`, `price_source`, `price_as_of`, and `price_age_seconds`.
- If live mark fetch fails, allow a fresh cached paper-position mark only
  within a strict configurable window; otherwise fail closed to recheck.
- Do not use 1h candle close as an execution price.

### P0: Profit Protection

- Add a deterministic strong-conflict profit-protection path before the
  breakeven-only path.
- Evaluate current R, MFE/R, and retracement from MFE using initial risk.
- Close or lock profit only when the configured thresholds are met.
- Preserve the existing conservative behavior for weak or passive signals.
- Persist close reason, triggering GA decision, mark-price audit, jobs,
  shadow-PnL backfill, and trade review.

### P1: Correct Price Rendering

- Paper-event payloads must distinguish `entry_price`, `mark_price`,
  `new_stop_loss`, and `exit_price`.
- Stop-adjustment notifications must display current mark price as
  "当前 Mark Price", not label entry price as generic "价格".
- Fill notifications continue to display fill price.
- Close notifications display exit mark price.

### P1: UTC+8 Coverage

- Every paper-trading notification must contain a UTC+8 event timestamp.
- Apply a shared formatter to fills, closes, stop changes, conflict exit,
  conflict stop tightening, conflict recheck, pending cancellation/recheck,
  drawdown alerts, and daily review windows.
- Event time must come from the action/event payload where available; fallback
  to current UTC time only when no event timestamp exists.

### P2: Diagnostics and Tests

- Add diagnostics for stale/missing execution price metadata.
- Add end-to-end tests for profit protection, fresh mark failure, price
  rendering, and UTC+8 coverage across all paper notification types.

## Out of Scope

- Real orders or exchange account operations.
- Closing every profitable position merely because PnL is positive.
- Changing entry strategy or signal scoring.

## Acceptance Criteria

- The XRPUSDT #1 scenario would protect material profit instead of returning
  from +1.44R MFE to 0R.
- No simulated financial action uses a 1h candle close as execution price.
- Notifications never present entry price as current market price.
- Every paper-trading notification contains an explicit UTC+8 event time.
- Full test suite, schema health, and state consistency pass.
