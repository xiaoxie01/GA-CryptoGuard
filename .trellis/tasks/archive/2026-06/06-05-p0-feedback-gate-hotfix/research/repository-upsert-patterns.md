# Research: Repository Upsert and Dedupe Patterns

- **Query**: Find create_opportunity_watch, upsert patterns, dedupe_key usage
- **Scope**: internal
- **Date**: 2025-06-05

## Findings

### Files Found

| File Path | Description |
|---|---|
| `plugins/crypto_guard/storage/repository.py` | All repository methods |
| `plugins/crypto_guard/storage/schema.sql` | Table DDL |

### create_opportunity_watch (repository.py lines 563-603)

```python
def create_opportunity_watch(
    self,
    symbol: str,
    watch: dict[str, Any],
    source_signal_id: int | None = None,
    expires_at: str | None = None,
    *,
    ga_decision_id: int | None = None,
    created_by_user_action: bool = False,
    source_button_action: str | None = None,
) -> int:
```

This is a **plain INSERT** -- no deduplication, no ON CONFLICT, no upsert. It always creates a new row. The columns inserted are:

```
symbol, direction, watch_reason, watch_condition_json, invalid_condition_json,
source_signal_id, expires_at, ga_decision_id, created_by_user_action, source_button_action
```

Returns the new row ID.

### ensure_paper_account (repository.py lines 1126-1135)

Upsert pattern using `ON CONFLICT ... DO NOTHING`:

```sql
INSERT INTO paper_accounts(account_name, initial_balance, current_balance, equity)
VALUES (?, ?, ?, ?)
ON CONFLICT(account_name) DO NOTHING
```

Then fetches the existing row by `account_name`. This is the "insert if not exists, then read" pattern.

### upsert_symbol (repository.py lines 19-44)

Upsert pattern using `ON CONFLICT ... DO UPDATE`:

```sql
INSERT INTO symbols(symbol, base_asset, quote_asset, category, enabled, source, default_timeframes, notes)
VALUES (?, ?, 'USDT', ?, ?, ?, ?, ?)
ON CONFLICT(symbol) DO UPDATE SET
    category=excluded.category,
    enabled=excluded.enabled,
    source=excluded.source,
    default_timeframes=COALESCE(excluded.default_timeframes, symbols.default_timeframes),
    notes=COALESCE(excluded.notes, symbols.notes),
    updated_at=CURRENT_TIMESTAMP
```

This requires a UNIQUE constraint on `symbol` to work.

### upsert_candles (repository.py lines 81-91)

Upsert pattern using `ON CONFLICT(symbol, interval, open_time) DO UPDATE SET` -- requires a composite UNIQUE index.

### dedupe_key in alert_outbox (repository.py lines 1421-1450)

The `enqueue_alert` method accepts `dedupe_key: str | None = None` and inserts it into `alert_outbox.dedupe_key`. This is a soft deduplication field -- the caller is responsible for generating a unique key, and the consumer is responsible for checking it. No database-level UNIQUE constraint on `dedupe_key` was found.

### upsert_paper_position_from_trade (repository.py lines 1159-1193)

Manual upsert pattern: SELECT first, then UPDATE if exists, else INSERT. No ON CONFLICT clause used. Uses `trade["id"]` as the position ID.

### Key Finding: No Existing Dedupe on opportunity_watches

- `create_opportunity_watch()` in repository.py is a plain INSERT with no deduplication
- `_create_opportunity_watch_from_gate()` in paper_broker.py (line 420) implements its own deduplication via SELECT-then-INSERT in a transaction, checking `(ga_decision_id, watch_reason, status='active')`
- There is no UNIQUE index or constraint on `opportunity_watches` that would enable `ON CONFLICT` upsert
- The only index is `idx_opportunity_status_symbol ON opportunity_watches(status, symbol)` (non-unique)

## Caveats / Not Found

- No `ON CONFLICT`-based upsert exists for opportunity_watches anywhere in the codebase
- The `dedupe_key` pattern in alert_outbox is a soft pattern (no DB constraint), not a hard deduplication
