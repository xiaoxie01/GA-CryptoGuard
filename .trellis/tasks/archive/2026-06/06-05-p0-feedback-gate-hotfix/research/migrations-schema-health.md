# Research: Migrations and Schema Health

- **Query**: Find opportunity_watches migrations, check_schema_health signature, unique indexes
- **Scope**: internal
- **Date**: 2025-06-05

## Findings

### Files Found

| File Path | Description |
|---|---|
| `plugins/crypto_guard/storage/migrations.py` | All migration functions |
| `plugins/crypto_guard/storage/schema.sql` | Base table DDL |

### opportunity_watches CREATE TABLE

The base CREATE TABLE is in `schema.sql` lines 214-233 (not in migrations.py). See the opportunity-watcher research file for the full column list.

### Migration Additions to opportunity_watches

From `migrations.py` lines 385-387:

```python
_add_column(conn, "opportunity_watches", "ga_decision_id", "INTEGER")
_add_column(conn, "opportunity_watches", "created_by_user_action", "INTEGER DEFAULT 0")
_add_column(conn, "opportunity_watches", "source_button_action", "TEXT")
```

These are in the `_apply_ga_decision_schema_migration()` function (the function name is inferred from context around line 350-407).

### check_schema_health Signature and Implementation

**Location**: `migrations.py` lines 480-528

**Signature**:
```python
def check_schema_health(config: CryptoGuardConfig | None = None) -> dict[str, Any]:
```

**How it gets the connection**:
```python
cfg = config or load_config()
conn = connect_db(cfg.database_path)
```

It creates its own connection from the config's `database_path`, checks the required columns, and closes the connection in a `finally` block.

**Return structure**:
```python
{
    "ok": bool,                    # True if no missing columns
    "missing_columns": [           # List of {table, column} dicts
        {"table": str, "column": str}
    ],
    "tables_checked": [str],       # List of table names checked
}
```

**Currently checked tables/columns** (lines 494-497):
```python
required_columns = {
    "skill_feedback_memory": ["pattern_type", "affected_symbols", "affected_sides"],
    "ga_decisions": ["account_feedback_gate_json"],
}
```

### Existing Unique Indexes on opportunity_watches

**None.** The only index on opportunity_watches is:
```sql
CREATE INDEX IF NOT EXISTS idx_opportunity_status_symbol ON opportunity_watches(status, symbol);
```
(schema.sql line 234)

This is a non-unique composite index. There are no UNIQUE indexes or constraints on this table.

### Other Unique Indexes in the Schema (for reference)

- `paper_accounts.account_name` -- `UNIQUE` constraint (schema.sql line 238)
- `daily_review_reports.review_date` -- `UNIQUE` constraint (schema.sql line 329)
- `idx_paper_orders_ga_decision_unique` -- partial unique index `ON paper_orders(ga_decision_id) WHERE ga_decision_id IS NOT NULL` (migrations.py line 391)

## Caveats / Not Found

- The `_add_column` function (used for migrations) is defined earlier in migrations.py and handles `ALTER TABLE ... ADD COLUMN` with error tolerance for already-existing columns.
- `check_schema_health` does NOT check opportunity_watches columns at all -- it only checks `skill_feedback_memory` and `ga_decisions`.
