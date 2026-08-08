# Contributing to HomelabHQ

Contributions are welcome, particularly new device drivers and compatibility
fixes validated against real hardware.

## Development setup

HomelabHQ supports Python 3.11–3.13 on Linux and Unix-like systems. From the
repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt -e '.[test]'
npm ci
npx playwright install --with-deps chromium
```

Local development must use an empty or test-only data directory:

```bash
HLHQ_DATA_DIR=./data python3 backend/app.py
```

Do not use a local process for a store containing real device credentials. The
container deployment provides an OS process boundary that local development
does not.

## Workflow

1. Create a focused branch from the current default branch.
2. Keep each change self-contained and avoid unrelated formatting or cleanup.
3. Add or update tests for every behaviour change.
4. Update public documentation when behaviour, configuration, or operation
   changes.
5. Activate `.venv` and run focused checks while developing.
6. Run the complete verification entry point before submitting:

   ```bash
   source .venv/bin/activate
   ./scripts/verify.sh
   ```

7. Use a concise [Conventional Commit](https://www.conventionalcommits.org/)
   message such as `feat: add example driver` or `fix: handle revised API field`.
8. Open a pull request explaining the behaviour, tests, and any hardware model
   and firmware used for validation.

The verification entry point runs compile checks, Ruff, configured mypy,
coverage-enforced pytest, dependency auditing, and Playwright. Missing external
tools are reported with the command needed to complete the check. See
[docs/verification.md](docs/verification.md) for details.

## Adding a driver

A driver subclasses `Driver` from `backend/drivers/base.py` and implements:

- `probe(conn)` to return a confidence score used during detection;
- `entities()` to describe sensors and opt-in controls; and
- optionally, `detail(conn)` to return structured information and tables.

Add the driver under `backend/drivers/`, import it from
`backend/drivers/__init__.py`, and add a mock server modelled on the vendor's
documented endpoints. Existing drivers and `_verify/` mock scenarios are the
best templates. Add normal pytest coverage under `tests/` for shared contracts
or application behaviour.

## Sensitive data

Never commit real hostnames or addresses, credentials, API tokens, private
keys, certificates, browser subscriptions, or a populated `data/` directory.
Use documentation ranges and fake fixtures in tests.

By contributing, you agree that your contribution is licensed under the
project's [MIT License](LICENSE).
