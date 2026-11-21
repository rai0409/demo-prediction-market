# V2 baseline — PR #26 verification record

This is evidence for **existing** V2 collateral, schema, migration-fixture, invariant and balance-history baseline integration. It does not establish that V2 matching or production migration is ready.

- Repository: `rai0409/demo-prediction-market`
- PR: https://github.com/rai0409/demo-prediction-market/pull/26
- Base commit: `aaea5800d6a73e6e53bb6ab8b0d9d46b7678b51f`
- Feature commit: `108ed022e7e19772251ac97c1b85bb33997e77e7` (2 commits ahead of base)
- Original integration: 20 changed files
- Evidence captured at (UTC): `2026-11-17T16:53:37+00:00`

## Verification

Local targeted tests and post-commit targeted tests passed, according to the operator's execution logs. The first local full suite had 2 failures in the request-observability tests because V2 write permission was not enabled by those tests. A subsequent targeted test change resolved those failures; the final full suite passed. No claim is made here that a production DB or live trading path was tested.

GitHub Actions checks at capture time:

- `full-pytest`: `SUCCESS`
- `focused-collateral-ledger`: `SUCCESS`
- `compileall`: `SUCCESS`
- `git-diff-check`: `SUCCESS`

Relevant local test summaries (full raw logs are **not** committed):

- `resume/targeted-tests-with-venv.log`: 472 passed in 36.29s (local private copy; SHA256 recorded in JSON)
- `resume/post-commit-tests-with-venv.log`: 472 passed in 62.66s (0:01:02) (local private copy; SHA256 recorded in JSON)
- `resume/full-suite-with-venv.log`: 2 failed, 1008 passed in 52.95s (local private copy; SHA256 recorded in JSON)
- `fix/targeted-tests.log`: 107 passed in 3.12s (local private copy; SHA256 recorded in JSON)
- `fix/post-commit-targeted-tests.log`: 107 passed in 2.90s (local private copy; SHA256 recorded in JSON)
- `fix/post-commit-full-suite.log`: 1010 passed in 70.11s (0:01:10) (local private copy; SHA256 recorded in JSON)

## Evidence retention

- Machine-readable verification manifest: `docs/verification/v2-baseline-pr26-evidence.json`.
- Raw selected logs and integration JSON reports: local ignored `runtime/v2-private-evidence/pr26/` (not uploaded to GitHub).
- Original uncommitted-worktree backup remains outside the repository, in the operator's home directory; it is not GitHub-safe for automatic publication.
- The pre-PR review patch is redundant with the versioned Git diff and is not copied.
- The disposable Python virtual environment and throwaway `/tmp` worktrees are not copied.
- Original source files and temporary artifacts were **not** removed.

## Known limitations

- Matching engine NORMAL/MINT/MERGE: not implemented in this PR.
- Production V2 migration and settlement: not validated by this evidence.
- This record is not a substitute for a security or financial-conservation review.
- GitHub CI results reflect the capture time; check the PR again before merge.
