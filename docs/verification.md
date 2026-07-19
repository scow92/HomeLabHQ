# Verification and operational baseline

Supported Python versions are 3.11 through 3.13 on Linux/Unix. Production runs
in the provided unprivileged container with a writable data directory; the
local development mode is for empty/test data only, as described in the README.

Run the complete regression suite from the repository root:

```bash
python -m pip install -r requirements.txt -c constraints.txt -e '.[test]'
python -m compileall -q backend _verify tests
python -m ruff check backend _verify tests
python -m mypy
python -m pytest --cov=backend --cov-report=term-missing
python -m pip_audit
npm ci
npx playwright install --with-deps chromium
npm run test:e2e
```

The coverage floor is currently **47.9% branch coverage**, measured on 2026-07-19
with 50 passing tests. Treat it as a ratchet: raise it when coverage improves
and do not lower it for unrelated changes.

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
