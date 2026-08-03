---
description: Run the mandatory cross-layer final review for the active CryptoGuard Trellis task and close every finding.
argument-hint: [task-path]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, Skill
---

# CryptoGuard Final Seal

Task override: `$ARGUMENTS`

Load the `crypto-guard-final-seal` skill and execute it completely.

1. Use the active task unless a task path was supplied.
2. Dispatch `crypto-guard-reviewer` with `Active task: <resolved task path>` as the first prompt line.
3. Fix every finding, including P2 and recommended items, then re-run review.
4. Run focused tests, fault injection, two consecutive full suites, required diagnostics and `git diff --check`.
5. Write `final-seal.md` in the task directory.
6. Do not commit, migrate production, repair production data or restart services.

Finish with the four separate verdict flags. A single "tests passed" verdict is invalid.
