---
description: Run a read-only CryptoGuard diagnosis and report evidence without modifying production state.
argument-hint: [scope]
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# CryptoGuard Read-Only Diagnosis

Scope: `$ARGUMENTS`

1. Resolve the active Trellis task and read `.trellis/spec/guides/crypto-guard-delivery.md`.
2. Inspect the requested scope, current diff, logs and database only through read-only queries.
3. Never call migrations, repairs, mutating repository methods, service controls or Git mutation commands.
4. Dispatch `crypto-guard-ops-auditor` when the scope includes production state, schema, workers, reports or startup.
5. Report observed facts, exact sources, current versus expected state, severity-ranked findings, and an unexecuted repair plan.

Do not say "all normal" when a diagnostic was skipped, limited, run on an empty DB, or used stale data.
