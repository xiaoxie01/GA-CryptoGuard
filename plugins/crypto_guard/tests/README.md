# CryptoGuard Test Gates

The former `test_smoke.py` monolith is split into domain collectors backed by
`_smoke_suite.py`. The complete suite still collects every legacy regression;
the compatibility `test_smoke.py` only supports old runtime imports.

## Environment

Tests use only the dedicated `crypto_guard_test` PostgreSQL database and the
`crypto_guard_test_app` role. Supply the local bootstrap password through
`CRYPTO_GUARD_DB_ADMIN_PASSWORD`; never put it in source or command output.

## Fast development gate

Run the domains most commonly affected by application changes:

```powershell
python -m pytest `
  plugins/crypto_guard/tests/test_core_regressions.py `
  plugins/crypto_guard/tests/test_paper_orders_regressions.py `
  plugins/crypto_guard/tests/test_hourly_report_regressions.py `
  plugins/crypto_guard/tests/test_market_data_contracts.py `
  plugins/crypto_guard/tests/test_llm_decision_regressions.py `
  -q --maxfail=1
```

For a narrow change, select the owning file, class, or `-k` expression first.

The marker-based unit/structure gate is network- and PostgreSQL-free:

```powershell
python -m pytest plugins/crypto_guard/tests -q -m unit
```

## PostgreSQL integration gate

Run the explicitly marked PostgreSQL production-path coverage:

```powershell
python -m pytest plugins/crypto_guard/tests -q -m pg
```

## Schema, concurrency, subprocess, and slow gates

These tiers are marked on their owning modules or exact test methods:

```powershell
python -m pytest plugins/crypto_guard/tests -q -m schema_mutation
python -m pytest plugins/crypto_guard/tests -q -m concurrency
python -m pytest plugins/crypto_guard/tests -q -m subprocess
python -m pytest plugins/crypto_guard/tests -q -m slow
```

Targeted marker gates intentionally deselect their complement. The complete
runner does not: it passes the pre-validated parallel and serial node IDs via
pytest argfiles and fails if either stage reports a deselection.

## Complete optimized gate

The complete gate has two stages. The parallel stage runs every worker-safe
test; the serial stage runs the small set of tests that intentionally measure
global PostgreSQL timing or ownership. Their collected node sets must be
disjoint and their union must equal the unfiltered suite.

Explicit plugin loading keeps the environment deterministic when plugin
autoload is disabled:

```powershell
$env:CRYPTO_GUARD_DB_ADMIN_PASSWORD = "<local test bootstrap password>"
python plugins/crypto_guard/tests/run_complete_suite.py --workers 8
Remove-Item Env:CRYPTO_GUARD_DB_ADMIN_PASSWORD
```

The runner proves the partition before executing it and returns non-zero for
missing/overlapping nodes, a failed parallel stage, or a failed serial stage.
Use `--verify-partition-only` to audit collection without running test bodies.

Every worker creates its own reusable PostgreSQL schema. Data is reset before
each method, so `worksteal` can balance large classes across workers, while
explicitly destructive methods retain a fresh per-test schema.

## Serial diagnostic gate

When investigating ordering or concurrency failures, remove xdist and stop on
the first failure:

```powershell
python -m pytest plugins/crypto_guard/tests -x -q --durations=100
```

Final-seal evidence must prove `parallel nodes + serial nodes = all nodes`,
with no overlap, then execute both exact-node stages with zero failures, skips,
or deselections. Editing files during either stage invalidates that complete
run.
