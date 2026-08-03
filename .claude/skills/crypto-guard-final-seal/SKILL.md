---
name: crypto-guard-final-seal
description: Run the mandatory CryptoGuard closure workflow for changes involving market data, GA decisions, risk gates, paper orders, positions, shadow evaluations, evolution, persistence, migrations, schedulers, notifications, or hourly/daily reports. Use when implementation is claimed complete, the user asks for a final review, repeated review rounds have occurred, or production readiness must be assessed.
---

# CryptoGuard Final Seal

Use this skill after implementation and before commit or production migration. Treat every finding as mandatory unless the user explicitly changes the requirement.

## Workflow

1. Resolve the active task with `python ./.trellis/scripts/task.py current --source`.
2. Read, in order:
   - `.trellis/spec/guides/crypto-guard-delivery.md`
   - task `prd.md`, `design.md`, and `implement.md`
   - task `check.jsonl` entries
   - the complete task diff, including untracked files
3. Establish scope ownership. Do not modify unrelated dirty files. If another agent is editing overlapping files, stop and report the collision.
4. Read [closure-matrix.md](references/closure-matrix.md) and audit every applicable row.
5. Dispatch `crypto-guard-reviewer` with `Active task: <resolved task path>` as the first prompt line for an independent evidence-based pass.
6. Fix every P0, P1, P2, recommended item, weak test, diagnostic gap, and documentation inconsistency. Re-run the reviewer until zero findings remain.
7. Run focused tests, fault injection, the complete suite twice, required diagnostics, and `git diff --check`.
8. Write `{TASK_DIR}/final-seal.md` using the template in the delivery guide. Keep the four completion states separate.

## Evidence Rules

- Verify claims from code, tests, database queries, or command output.
- A test that mocks the function under test is not behavioral evidence.
- A passing test does not prove migration safety, restart safety, data freshness, or notification idempotency.
- Do not accept "pre-existing failure" without a baseline reproduction.
- Do not infer production readiness from a clean diagnostic on a fresh or empty database.

## Boundaries

- Do not commit, migrate production, repair production data, or restart services during final seal.
- Use `/trellis:crypto-guard-release` only after code is committed and the working tree is clean.
- If verification cannot run, mark the corresponding state false. Never replace evidence with confidence language.
