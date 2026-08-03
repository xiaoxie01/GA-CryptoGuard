# Closure Matrix

## Contents

1. Scope and requirement traceability
2. Temporal and market-data integrity
3. Trading state machine
4. Persistence and concurrency
5. Evolution and statistics
6. Reports and notifications
7. Migration and recovery
8. Test authenticity

## 1. Scope And Requirement Traceability

- Map every PRD acceptance criterion to code and at least one behavioral assertion.
- Search all producers and consumers when a field, enum, status, config, timestamp or function signature changes.
- Inspect untracked files and generated schema/migration artifacts.
- Confirm no unrelated dirty files were overwritten.

## 2. Temporal And Market-Data Integrity

- Distinguish open time, close time, analysis time, event time, exchange time and local display time.
- Require closed candles for decisions and fills unless a contract explicitly says otherwise.
- Check aligned boundaries, future candles, stale data, restart backfill, pagination and cursor advancement.
- Verify every timeframe receives the configured minimum bars and downstream engines consume the full intended window.
- Reject missing, stale, future or partial data fail-closed on financial actions.

## 3. Trading State Machine

- Trace signal -> decision -> risk -> order -> pending revalidation -> fill -> position -> exit.
- Validate direction, entry type, entry confirmation, invalidation, stop, targets and reward/risk ordering.
- Revalidate latest time-pinned GA and market data immediately before fill.
- Verify legal transitions and closed-row guards.
- Ensure one financial action emits one audit event and one user notification.

## 4. Persistence And Concurrency

- Use transactions/savepoints around cross-table mutations.
- Check CAS predicates and every required rowcount.
- Fault-inject failures after each write and verify rollback.
- Test duplicate schedulers, retries, restarts and concurrent closes/adjustments.
- Verify dedupe constraints tolerate dirty historical databases before index creation.

## 5. Evolution And Statistics

- Trace real trade outcome to active baseline and paired shadow sample.
- Keep `real_pnl`, `pending_outcome`, ambiguous, duplicate and legacy sources distinct.
- Require real-PnL sample gates; never treat `None` as zero or pseudo outcomes as wins.
- Verify candidate caps, three-table status consistency and report-only write protection.

## 6. Reports And Notifications

- Derive PnL, counts, win rate and R deterministically.
- Label data source, batch identity, window, freshness and degraded state.
- Keep analysis text subordinate to structured execution gates.
- Format all paper-trading event times in UTC+8 exactly once.
- Test dedupe across pending, sent, retries and concurrent workers.

## 7. Migration And Recovery

- Test fresh DB, current DB and intentionally dirty legacy DB.
- Write contract markers only after successful migration.
- Make migrations idempotent and validate index definitions, not only marker presence.
- Verify backup hash/integrity, key row counts, schema health, state consistency and restart behavior.

## 8. Test Authenticity

- Prefer public API/entry-point tests over manually reproducing internal loops.
- Avoid mocking the unit under test.
- Use exchange-aligned timestamps and deterministic clocks.
- Include failure injection, restart, concurrency and historical replay.
- Run the complete suite twice for high-risk changes.
