# Safely Sync Upstream GenericAgent

## Goal

Merge `origin/main` from `lsdefine/GenericAgent` into the local repository without
overwriting the local CryptoGuard product, Trellis metadata, runtime data, or secrets.

## Baseline

- Local HEAD before sync: `eb7d7cb`
- Upstream HEAD: `0101419`
- Merge base: `a941a6d`
- Local-only commits: 42
- Upstream-only commits: 160
- Changed-path overlap: `.gitignore` only

## Requirements

1. Create a recovery branch at the pre-sync local HEAD.
2. Merge upstream history rather than replacing or resetting local history.
3. Accept upstream changes for GenericAgent core and frontend files.
4. Preserve all local-only CryptoGuard, Trellis, documentation, data-ignore, and
   secret-ignore behavior.
5. Resolve `.gitignore` as a union of upstream and local requirements.
6. Do not add databases, runtime data, credentials, logs, or temporary files.
7. Verify imports/compilation and run the CryptoGuard test suite after the merge.
8. Record the resulting merge commit and keep the recovery branch available.

## Acceptance

- `git merge-base --is-ancestor origin/main HEAD` succeeds.
- `plugins/crypto_guard` remains present and its tests pass.
- Working tree is clean after the merge commit.
- `.gitignore` includes both upstream additions and local `data/` / `.ace-tool/`
  exclusions.
- No tracked database, credential, log, cache, or runtime data is introduced.

