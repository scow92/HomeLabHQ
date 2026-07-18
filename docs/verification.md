# Verification and operational baseline

Supported Python versions are 3.11 through 3.13 on Linux/Unix. Production runs
in the provided root-owned container with a writable data directory; the local
development mode is for empty/test data only, as described in the README.

Run the complete regression suite from the repository root:

```bash
python -m pip install -r requirements.txt -c constraints.txt -e '.[test]'
python -m compileall -q backend _verify tests
python -m ruff check backend _verify tests
python -m pytest --cov=backend --cov-report=term-missing
python -m pip_audit
```

The pytest command includes each retained `_verify/*_test.py` mock-server
scenario as a discoverable test. Before a production refactor, capture these
environment-specific baseline values alongside the command output: full test
runtime and coverage, `poller.poll_once()` duration for a representative device
set, `du -sh $HLHQ_DATA_DIR`, document write count per poll cycle, and p50/p95
latency for `/api/session`, `/api/devices`, and `/api/clients`. They are
deployment measurements rather than portable repository constants.

This workspace cannot record those values because it has no Python interpreter
or Docker runtime; the failed baseline command results are noted in the change
summary.
