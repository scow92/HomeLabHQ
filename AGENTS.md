# HomelabHQ Codex Instructions

## Mission

Maintain HomelabHQ as a reliable, tested and clearly documented application.

Work autonomously when the repository and environment provide enough evidence.
Never invent requirements, test results, benchmarks or production measurements.

## Standard workflow

For each assignment:

1. Read the relevant code, tests, documentation and Git history.
2. Check `git status` before editing.
3. Classify work as:
   - already complete;
   - actionable repository work;
   - intentionally deferred;
   - externally blocked.
4. Write a concise execution plan for substantial work.
5. Implement one logical task at a time.
6. Add or update tests for changed behaviour.
7. Update documentation when behaviour or operation changes.
8. Run focused checks during development.
9. Run `./scripts/verify.sh` before completion.
10. Review the final diff.
11. Create one atomic Conventional Commit per logical task.
12. Report commits, checks, skips, blockers and deferred work.

Do not stop merely because an initial check fails. Diagnose and fix failures caused
by the current work when it is safe and within the repository.

## Commit policy

Use Conventional Commits:

- `feat:` new behaviour
- `fix:` corrected behaviour
- `refactor:` structural change without intended behaviour change
- `test:` tests only
- `docs:` documentation only
- `ci:` continuous-integration changes
- `build:` packaging or dependencies
- `chore:` maintenance

Before committing:

1. Inspect `git diff`.
2. Stage only files belonging to the logical task.
3. Run relevant checks.
4. Confirm no unrelated or sensitive files are staged.
5. Use a concise and accurate message.

Never use force push, hard reset, wholesale Git clean or published-history rewriting.

## Architecture constraints

Do not implement these deferred changes unless their documented trigger is
objectively met:

- SQLite migration.
- Splitting the main CSS file solely for aesthetic reasons.
- Redesigning or separating history storage.

When evaluating a deferred change:

1. Measure the current repository state.
2. Identify the documented trigger.
3. Compare evidence with the trigger.
4. Record the evidence.
5. Leave the architecture unchanged when the trigger is not met.

## Verification rules

Use `./scripts/verify.sh` as the full verification entry point.

Do not:

- claim a check passed unless it actually ran successfully;
- weaken tests or coverage thresholds just to obtain a pass;
- hide failed or skipped checks;
- fabricate browser or deployment results.

A missing external tool is not automatically a code failure. Report the missing
tool and the exact command required to complete that check.

## Production measurements

Production-like poll duration, write rate, API latency and similar values must
come from real instrumentation or logs.

When the current environment cannot collect them:

1. Inspect existing instrumentation.
2. Add safe collection tooling when appropriate.
3. document the deployment-side collection command;
4. leave actual values unrecorded until measured.

## Completion standard

Work is complete when applicable implementation, tests, documentation,
verification and atomic commits are present.

When blocked, include the exact command, error and supporting evidence.
