# Verification and operational baseline

Supported Python versions are 3.11 through 3.13 on Linux/Unix. Production runs
in the provided unprivileged container with a writable data directory; the
local development mode is for empty/test data only, as described in the README.

Install the locked dependencies, then run the complete regression suite from
the repository root:

```bash
python -m pip install -r requirements.txt -c constraints.txt -e '.[test]'
npm ci
npx playwright install --with-deps chromium
./scripts/verify.sh
```

The verification entry point runs the same compile, Ruff, configured mypy,
coverage-enforced pytest, dependency-audit, and Playwright checks as CI. Each
check is reported as `PASS`, `FAIL`, or `SKIP`. A skip is reserved for an
unavailable external dependency and includes the exact command needed to run
that check; repository or test failures still produce a non-zero exit status.

The coverage floor is currently **54.7% branch coverage**, raised on 2026-07-19
after the suite reached 67 passing tests. Treat it as a ratchet: raise it when
coverage improves and do not lower it for unrelated changes.

The Playwright suite starts the application with a fresh temporary data store.
It covers setup/login, preserved device state on a failed refresh, client
filtering and bulk actions, keyboard modal/hash navigation, and the offline
service-worker shell.

The pytest command includes each retained `_verify/*_test.py` mock-server
scenario as a discoverable test. Before a production refactor, capture these
environment-specific baseline values alongside the command output: full test
runtime and coverage, `poller.poll_once()` duration for a representative device
set, `du -sh $HLHQ_DATA_DIR`, document write count per poll cycle, and p50/p95
latency for `/api/session`, `/api/devices`, and `/api/clients`. They are
deployment measurements rather than portable repository constants.

Record those values with the release or deployment evidence. Include the
release identifier, deployment date, device count, poll interval, and the
following values so later capacity decisions have comparable context:

| Measurement | Value | Collection point |
|---|---|---|
| poll duration |  | representative `poller.poll_once()` cycle |
| main-document bytes and writes |  | `store.metrics()` after a representative cycle |
| data-directory bytes |  | `du -sh $HLHQ_DATA_DIR` |
| `/api/session`, `/api/devices`, `/api/clients` p50/p95 |  | authenticated production-like request sample |

These are deployment observations, not portable repository constants; do not
hard-code their values in this document.
