---
description: Execute the user-confirmed CryptoGuard release with read-only preflight, guarded mutation and independent post-release verification.
argument-hint: [production-storage-identity]
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# CryptoGuard Production Release

Production storage override: `$ARGUMENTS`

Read `.trellis/spec/guides/crypto-guard-delivery.md` and the active task's `final-seal.md`.

## Hard Gates

1. Require `final_seal_complete: true`.
2. Require committed code and a clean working tree.
3. Dispatch `crypto-guard-ops-auditor` with `Active task: <resolved task path>`
   as the first prompt line. Require `release_plan_ready: true` and close every
   blocker.
4. Identify the active storage engine, every writer process, the password-free
   production identity, and any legacy SQLite archive source. Do not assume the
   release is still SQLite-shaped.
5. Present every archive/backup, role/database creation, secret injection,
   initialization/migration, verification, rollback, restart and observation
   command. Label each command read-only, database mutation, service control,
   or both. Redact passwords and raw DSNs.
6. Ask the user for explicit confirmation before any production mutation or service control.

## PostgreSQL Greenfield Contract

When the committed code is PostgreSQL-only and the production database does
not exist:

1. Stop all writers.
2. Archive the old SQLite file read-only; record path, bytes and SHA256. Do not
   import its business rows when the task explicitly chose a fresh start.
3. Generate independent strong passwords for `crypto_guard_migrator` and
   `crypto_guard_app`; never reuse the administrator password. Keep values out
   of commands, logs, source, YAML, task docs and Git.
4. Create the two dedicated non-dangerous roles and empty `crypto_guard`
   database under the user-confirmed administrator session.
5. Inject runtime and migration DSNs only through approved environment/secret
   channels. Never echo them.
6. Run explicit migrator initialization with `allow_ddl=True` before service
   startup. Verify grants, schema fingerprint, expected seeds and diagnostics
   through the runtime role.
7. Start the service, revoke unused approval immediately, and observe three
   complete batches.

## Guarded Execution

After confirmation, authorize only the operations and number of uses present in
the approved command list. Choose the shortest practical TTL; a greenfield
PostgreSQL setup may require up to 60 minutes, but do not grant unused scope:

```powershell
python .claude/hooks/crypto-guard-command-guard.py authorize --operation database-mutation --operation service-control --task <task-path> --ttl-minutes <reviewed-ttl> --uses <exact-command-count>
```

Include `# crypto-guard-approval:<token>` in each sensitive command. Execute
only the approved sequence. On the first failed invariant, stop, preserve the
scene and follow the reviewed rollback. Revoke unused authorization before
long-running observation:

```powershell
python .claude/hooks/crypto-guard-command-guard.py revoke
```

## Post-Release Gate

Dispatch `crypto-guard-postgres-release-verifier` with
`Active task: <resolved task path>` after startup. The main session may set
`production_ready: true` and `production_recovered: true` only when it returns
`post_release_verification: pass` and all three new complete batches pass.

Do not run `/trellis:finish-work` from this command. If any gate fails, stop.
Do not "continue cautiously".
