# CryptoGuard Delivery Guide

> **Purpose**: Define the mandatory closure and production-readiness contract for high-risk CryptoGuard changes.

## Risk Classification

Treat a task as high risk when it changes any of these areas:

- market data ingestion, candle completeness, aggregation, freshness, or event time
- GA decisions, trade plans, risk gates, order fills, cancellations, positions, or exits
- shadow evaluations, evolution candidates, statistics, or `outcome_source`
- schema, migrations, repairs, production database access, scheduler, or workers
- idempotency, notifications, hourly/daily reports, or financial audit fields

High-risk tasks require `prd.md`, `design.md`, and `implement.md`. A passing unit test is evidence, not closure.

## Four Completion States

Never collapse these states into a single "done" claim:

1. **Implementation complete**: requested code exists and targeted tests pass.
2. **Final seal complete**: independent cross-layer review has zero unresolved findings.
3. **Production ready**: committed clean tree, backup/rollback plan, migration and diagnostic commands reviewed.
4. **Production recovered**: migration applied, row-count invariants checked, diagnostics green, services restarted and stable.

## Mandatory Closure Matrix

| Surface | Required evidence |
|---|---|
| Requirements | Every acceptance criterion maps to code and a behavioral test |
| Time | UTC/event-time semantics, closed-candle boundaries, stale/future data rejection |
| Market data | Required bars per timeframe, pagination, restart backfill, no partial-candle advancement |
| Decision/risk | Generation, normalization, risk validation, pre-fill revalidation, fail-closed behavior |
| State | Legal transitions, CAS/rowcount checks, transaction or savepoint rollback |
| Cross-table | Orders, trades, positions, evaluations, logs and alerts remain consistent |
| Idempotency | Duplicate workers/retries/restarts do not repeat financial actions or notifications |
| Reporting | Financial facts deterministic; analysis identifies source, window, freshness and blockers |
| Diagnostics | New invariant has a diagnostic, cutoff/marker policy, and no historical false positives |
| Migration | Dirty-DB compatibility, idempotency, schema health and rollback path |
| Recovery | Fault injection, process restart and cursor/progress preservation |

All findings are mandatory. P2, "recommended", documentation drift, weak tests, and missing diagnostics are not optional unless the user changes the requirement.

## Verification Standard

For high-risk changes:

1. Run focused behavioral and fault-injection tests.
2. Run the complete CryptoGuard suite twice consecutively with zero failures and zero skips.
3. Run Schema Health and State Consistency.
4. Run Report Accuracy when reports, decisions, market data, notifications, or diagnostics changed.
5. Run `git diff --check`.
6. Record exact commands, counts, failures, skips, database path, and whether production data was mutated.

Never label an existing failure "unrelated" without reproducing it against the pre-change baseline or proving the changed path cannot affect it.

## Production Contract

Production mutation is a separate, user-confirmed phase:

1. Stop all writers.
2. Create a timestamped backup outside the live database path.
3. Record byte size, SHA256 and `PRAGMA integrity_check`.
4. Record key-table row counts.
5. Apply the reviewed migration/repair exactly once.
6. Re-run integrity, schema, state and report diagnostics.
7. Compare row counts and explain every allowed delta.
8. Restart services and verify stable scheduler/workers and notification delivery.

Use `/trellis:crypto-guard-release`. Do not start `fsapp.py` or `hub.pyw` merely because tests passed.

For an isolated reproduction, set `CRYPTO_GUARD_DB` to a temporary path outside project production-data directories and append `# crypto-guard-non-production-db:<same-path>` to the command. This exemption never applies to service control, destructive Git, or databases under project data directories.

## Spec Marketplace Decision

Do not create a Spec Template Marketplace for this repository alone. Extract this guide into a `type: "spec"` marketplace template only after a second repository needs the same sanitized conventions. Marketplace content must exclude tasks, workspace state, platform prompts, private incident data and production paths.

## Review Report Template

```markdown
# Final Seal

## Scope
- Task:
- Commit/diff:
- Risk surfaces:

## Findings
- P0:
- P1:
- P2:
- Recommended:

## Acceptance Matrix
| Requirement | Code | Test | Result |

## Verification
| Command | Run 1 | Run 2 |

## Diagnostics
- Schema Health:
- State Consistency:
- Report Accuracy:

## Production Readiness
- Clean committed tree:
- Backup/rollback reviewed:
- Migration required:
- Service restart required:

## Verdict
- implementation_complete:
- final_seal_complete:
- production_ready:
- production_recovered:
```
