# Refactor implementation plan

Reviewed: 2026-08-30

Repository baseline: `147ffbb`

## Objective

Reduce the cost and risk of changing HomelabHQ without changing its public
behaviour. The refactor must preserve HTTP routes and response contracts,
authorization, persisted data, device protocol behaviour, scheduler semantics,
PWA/offline behaviour, and the supported single-worker deployment model.

This plan supersedes no implemented architecture. The refactor program completed
in July 2026 established the current FastAPI, service, persistence, client-state,
verification, and deployment boundaries. The work below addresses growth since
that program, especially Compute/Ansible, VPN endpoint management, and background
monitoring.

## Review scope and evidence

The review covered every tracked backend, frontend, test, verification,
deployment, and documentation area; the current Git status and recent history;
and the earlier refactor plan and implementation commits.

| Evidence | Current state | Consequence |
|---|---:|---|
| Backend source | 80 Python files, 18,906 lines | The repository is already decomposed by broad domain; a rewrite is not justified. |
| `compute_maintenance.py` | 1,422 lines, 59 functions | Parsing, state reduction, persistence, and execution should be separated behind its current public boundary. |
| `compute.js` | 1,864 lines, 13 mutable state collections | Compute state, Proxmox polling, bulk jobs, Docker rendering, and detail rendering need focused modules and one coordinator. |
| `ansible_integration.py` | 963 lines, 45 functions | Controller configuration, inventory, approvals, and process execution are separable responsibilities. |
| `vpn_endpoint_service.py` | 1,118 lines, 50 functions | Profile normalization, discovery, status, polling, and rollback switching need internal boundaries. |
| `detail/vpn-endpoints.js` | 760 lines | VPN forms, rendering, and actions should follow the established feature-module ownership rule. |
| Background orchestration | `morning_updates.py` 856 lines, `device_updates.py` 827, `poller.py` 736 | State machines and remote execution are mixed with persistence and scheduling. |
| Application facade | `services.py` 637 lines, 82 functions, 19 backend dependencies | Authorization is centralized correctly, but Compute refresh orchestration is too large for the general facade. |
| Browser regression suite | `critical-path.spec.mjs` 1,801 lines, 25 tests | The coverage is valuable, but shared setup and five unrelated feature areas are in one serial file. |
| Largest Python test | `test_ansible_maintenance_contracts.py` 1,832 lines, 65 tests | Tests should align with the responsibilities extracted from production code. |
| CSS | `styles.css` 1,225 lines | The documented roughly 1,200-line CSS layering trigger is met. |
| Static analysis | The configured Ruff gate checks runtime-danger rules; mypy covers `domain.py`, `devices.py`, and `poller.py` | New boundaries should enter stricter checks incrementally, without forcing vendor payloads into a universal type. |
| Optional complexity diagnostic | 102 C901/PLR diagnostics, concentrated in orchestration and vendor mapping code | Use the result as prioritization evidence, not as a repository-wide gate or a line-count target. |
| Persistence evolution | Schema version advanced from 1 to 7 between 2026-07-19 and 2026-08-23 | The “frequent migrations” SQLite reassessment trigger has occurred; this warrants a measured decision, not an automatic migration. |
| Python support | Documentation says 3.11–3.14; CI currently tests 3.11–3.13 | Verification claims and the CI matrix must be reconciled before refactoring. |

### Review verification baseline

`./scripts/verify.sh` completed successfully in this review environment on
Python 3.14.6:

| Check | Result |
|---|---|
| Python compilation, Ruff, and configured mypy | Passed |
| Pytest | 360 passed in 26.43 seconds |
| Branch coverage | 67.79% against the 54.9% floor |
| Dependency audit | Passed; no known third-party vulnerabilities found |
| Playwright | 25 passed in 55.7 seconds |
| Verification summary | 6 passed, 0 failed, 0 skipped |

These are repository-test results, not production performance measurements.

## Work classification

### Already complete

- FastAPI application factory, lifespan, middleware, typed status models, and
  declarative domain route modules.
- Actor-scoped authorization and service boundaries for request-driven work.
- Atomic, locked, versioned JSON persistence with validation, backups, bounded
  collections, and write instrumentation.
- Separate client discovery, merge, roster, and NAC responsibilities.
- Acyclic native frontend modules and an explicit feature-state ownership rule.
- Compile, Ruff, mypy, coverage-enforced pytest, dependency audit, Playwright,
  and a configured Python 3.11–3.13 CI matrix.
- Single-worker scheduling and process-local coordination, which remain the
  supported deployment architecture.

### Actionable repository work

1. Correct the verification-support mismatch and add characterization guards.
2. Split the CSS now that its documented trigger is met.
3. Decompose the Compute/Ansible backend and frontend vertical slice.
4. Decompose VPN endpoint management while preserving its rollback contract.
5. Isolate background state machines from scheduling and remote execution.
6. Narrow the general application facade and normalize package boundaries.
7. Split oversized tests along behaviour boundaries and ratchet static checks.

### Reassessment required before implementation

SQLite now needs a decision record because the store has accumulated six
ordered migrations in about five weeks. A migration is not part of this plan
until production evidence shows that SQLite solves an observed capacity,
query, authorization-scan, or multi-process need. Phase 0 collects that
evidence and records the decision.

### Intentionally deferred

- Redesigning or separating history storage: no longer-retention or richer-query
  requirement is present.
- Multiple Uvicorn workers or a durable job queue: no deployment requirement is
  present, and process-local schedulers and locks make this unsafe today.
- A frontend framework, bundler, or universal state library: native modules are
  serving the application and the problem is feature-local ownership.
- A universal vendor response schema: flexible mappings remain appropriate at
  driver boundaries.
- Driver-wide complexity cleanup: extract only cohesive, tested vendor mapping
  helpers when a driver is already being changed.

### Externally blocked evidence

This checkout cannot supply production poll duration, store write rate and
latency, data-directory size, roster size, or API p50/p95 values. Before Phase 2,
collect the baseline described in `docs/verification.md` from a representative
deployment and attach it to the release or implementation record. Do not put
invented or one-off development values in repository documentation.

## Refactor rules

Every phase follows these rules:

1. Make one responsibility move per Conventional Commit. Do not mix feature
   work with refactoring.
2. Add or move characterization tests before moving production behaviour.
3. Preserve public names with a small facade only where callers genuinely need
   a stable boundary; do not keep two implementations.
4. Move code first, then simplify it in a later commit. This keeps review and
   `git bisect` useful.
5. Keep authorization at or above the application-service boundary and keep
   credential handling inside existing secure integration boundaries.
6. Do not change persisted field names or schema versions during structural
   moves. Any later data change requires its own migration and rollback tests.
7. Run focused tests after each move and `./scripts/verify.sh` before every
   phase is merged.
8. Keep coverage at or above the measured baseline and the configured 54.9%
   floor. Raise the floor only from a successful full run; never lower it.

## Phase 0 — establish refactor evidence and guardrails

### Implementation

- Resolve the Python 3.14 support discrepancy in one commit. The full local
  verifier passes on 3.14.6, so add 3.14 to CI and prove it there; narrow
  `docs/verification.md` only if the CI environment exposes an unresolved
  incompatibility. Do not claim CI support from an unexecuted matrix entry.
- Use this review's verification results as the repository baseline. Re-run and
  replace them before implementation if the branch changes materially.
- Capture the deployment-side measurements required by
  `docs/verification.md`. Record the environment, release, device count, roster
  count, main-document bytes, write count/duration, poll duration, and API
  latency sample with the operational evidence.
- Write an SQLite decision record using those measurements and the observed
  schema history. Choose one of: retain JSON with a reassessment date, prototype
  core metadata in SQLite, or plan a migration. Keep chart history outside the
  decision unless its separate trigger is met.
- Add characterization tests only where current tests do not pin these
  invariants:
  - OpenAPI path/method/auth-policy parity;
  - interrupted Compute and Proxmox job recovery;
  - per-node task reconciliation and stale-task rejection;
  - VPN switch success, failed apply, failed health check, and rollback failure;
  - scheduler non-overlap, lease recovery, shutdown, and retained last-known
    observations;
  - service-worker online refresh and offline use of every linked stylesheet
    and module.
- Split `e2e/critical-path.spec.mjs` by feature after extracting shared setup and
  route fixtures. Keep `fullyParallel: false` and one worker until isolation is
  proven.

Suggested browser layout:

```text
e2e/
  support/
    api-fixtures.mjs
    session.mjs
  auth-access.spec.mjs
  compute.spec.mjs
  devices.spec.mjs
  vpn-endpoints.spec.mjs
  pwa.spec.mjs
```

### Acceptance

- The support statement matches an executed CI matrix.
- Existing test cases are moved without assertion loss.
- Full verification passes with no new skip.
- The SQLite decision cites real measurements or explicitly retains JSON
  pending missing measurements; it does not assume a migration outcome.

## Phase 1 — split CSS into documented layers

The trigger is met at 1,225 lines. This is a mechanical ownership split, not a
visual redesign.

### Implementation

- Move declarations in their current cascade order into:

```text
web/styles/
  base.css        # tokens, reset, typography, themes, accessibility utilities
  components.css  # buttons, cards, forms, dialogs, tables, toasts, skeletons
  views.css       # app shell, Devices, Compute, Access, VPN, Logs, Settings
```

- Load the three files explicitly and in that order from `web/index.html`.
- Keep media queries adjacent to the rules they modify and preserve selector
  specificity and declaration order.
- Update service-worker cache versioning and offline tests so all three files
  are refreshed online and served from cache offline.
- Add a lightweight structure test that rejects a return to a monolithic
  `web/styles.css` entry point and verifies the stylesheet load order.

### Acceptance

- No selector, property, breakpoint, theme value, or rendered workflow changes.
- Setup/login, mobile navigation, Compute, Access, VPN, modal, theme, and offline
  Playwright paths pass.
- `web/styles.css` is removed rather than retained as a duplicate bundle.

## Phase 2 — decompose Compute and Ansible backend responsibilities

Keep `compute_maintenance.py` and `ansible_integration.py` as small public
facades during the move. They may re-export stable operations, but all logic
must have a single owner.

### Target ownership

```text
backend/maintenance/
  contracts.py       # recap/structured-result parsing and payload validation
  docker_model.py    # container, image, project, and update result reduction
  job_store.py       # persisted job and instance-state mutations
  runner.py          # subprocess execution and terminal transition handling
  service.py         # start, sequence, query, and recovery use cases

backend/ansible_support/
  controller.py      # controller validation, encryption, and connection test
  inventory.py       # inventory refresh, host/group normalization, mappings
  approvals.py       # approved playbooks, modes, targets, and variables
  executor.py        # fixed-command process boundary and sanitized output
```

### Delivery sequence

1. Split `test_ansible_maintenance_contracts.py` into parser, Docker model,
   lifecycle, approval, inventory, and service test modules without changing
   assertions.
2. Extract pure result parsers and Docker reducers. Replace mutation-heavy
   functions with explicit input/output values; keep vendor callback payloads
   mapping-shaped at the edge.
3. Extract persisted job mutation helpers. Preserve bounded history, exact
   timestamps, requested-by identity, per-workload exclusion, and recovery of
   interrupted jobs.
4. Extract the runner. Keep process timeouts, command allowlists, output
   sanitization, PLAY RECAP retention, and terminal-state persistence intact.
5. Split controller, inventory, approval, and executor responsibilities behind
   `ansible_integration.py`.
6. Move `services.refresh_compute()` orchestration into a focused
   actor-aware Compute refresh service. The general service facade should
   authorize and delegate, not coordinate provider refresh, Proxmox checks,
   inventory refresh, and maintenance queueing itself.
7. Update architecture and Compute documentation only after module ownership is
   final.

### Acceptance

- Existing API payloads, operation names, persisted job shapes, environment
  variables, command allowlists, and recovery semantics are unchanged.
- Contract reducers are deterministic and directly unit tested.
- No extracted module imports HTTP request objects or frontend concerns.
- The current public facades contain delegation and compatibility exports only.
- Focused Compute/Ansible tests and full verification pass.

## Phase 3 — split the Compute frontend by state and workflow

Follow `docs/frontend-state.md`: one coordinator combines state, API, views,
and actions; view modules emit intents and do not mutate another module's DOM.

### Target ownership

```text
web/js/compute/
  store.js          # workloads, hosts, filters, active detail, operation maps
  model.js          # eligibility, labels, summaries, and status reductions
  api.js            # Compute, Proxmox, mapping, and maintenance requests
  proxmox.js        # node catalogue and task reconciliation/polling
  bulk-actions.js   # bounded bulk execution and progress reporting
  workload-grid.js  # host grouping, cards, filters, and summary rendering
  detail.js         # detail composition and history
  docker.js         # container/project views and project actions
  index.js          # public coordinator used by the router
```

### Delivery sequence

1. Move pure eligibility and status functions to `model.js` and test them with
   fixture payloads already used by Playwright.
2. Establish `store.js` as the sole owner of mutable Compute state. Avoid
   exporting writable bindings or DOM nodes.
3. Move API calls, then Proxmox reconciliation and timer ownership. Closing a
   view or switching routes must stop only the relevant timers.
4. Move workload-grid, Docker, detail, and bulk-action rendering one at a time.
5. Point `router.js` at the new coordinator and remove the old monolith in the
   same phase; do not leave parallel state owners.
6. Extend the frontend import-graph test to enforce acyclicity and Compute's
   single state owner.

### Acceptance

- Existing Compute Playwright scenarios pass, including old-shell refresh,
  per-node task isolation, reload recovery, partial bulk failure, unmanaged
  project guidance, and loading/empty/error states.
- There is one owner for timers, active detail state, and Proxmox operation
  maps.
- No module imports another feature's mutable state or manipulates another
  feature's DOM.

## Phase 4 — decompose VPN endpoint management

VPN switching is security- and availability-sensitive. Preserve the current
transaction and error detail before simplifying UI or service code.

### Target ownership

```text
backend/vpn_endpoints/
  model.py          # profile, candidate, target, validation normalization
  repository.py     # bounded owner/device history and utilization mutations
  discovery.py      # NordVPN/RDAP discovery, classification, and scoring
  status.py         # connected-server status, health, and utilization polling
  switching.py      # configure, apply, verify, rollback transaction

web/js/vpn-endpoints/
  model.js
  form.js
  candidates.js
  status.js
  actions.js
  index.js
```

### Delivery sequence

1. Split backend and Playwright tests by profiles, discovery, status,
   utilization, and switching/rollback.
2. Extract normalization and persistence without changing document fields,
   bounds, legacy-profile compatibility, or owner scope.
3. Express switching as named stages with one recorded outcome per stage.
   Preserve fixed device actions, confirmation, post-apply health checks, and
   rollback-failure reporting.
4. Extract polling and alert-state updates while preserving intervals and
   avoiding extra management-plane calls.
5. Split the frontend around its existing progressively disclosed workflow.

### Acceptance

- All existing OPNsense, NordVPN, RDAP redirect, multi-profile, utilization,
  validation, switch, and rollback tests pass.
- Stored credentials and WireGuard key material never enter public responses or
  logs.
- A failed switch still distinguishes apply failure, health-check failure,
  successful rollback, and rollback failure.
- Background polling performs no additional remote calls compared with the
  characterized baseline.

## Phase 5 — isolate background state machines

Do this after Compute and VPN boundaries settle because morning checks call
both update systems and the poller calls VPN/client integrations.

### Implementation units

- `poller.py`: separate cycle scheduling, one-device collection, record
  application/availability transitions, binding enforcement, and history
  emission. Keep `poller.start()`, `stop()`, `poll_once()`, and `status()` as the
  lifecycle boundary.
- `device_updates.py`: separate SSH configuration, reboot assessment, persisted
  operation projection, install runner, and reboot runner. Retain the one-slot
  per-device exclusion rule.
- `morning_updates.py`: separate schedule/lease persistence, source collection,
  result aggregation, notification publication, and scheduler lifecycle.
- `asgi/status_service.py`: extract pure Network, Proxmox, TrueNAS, and Docker
  projections while retaining cached-only request behaviour.

For each unit, first extract pure transitions, then persistence adapters, then
thread/process orchestration. Never make a thread own durable truth that is
currently recovered from the store.

### Acceptance

- Existing non-overlap, timeout, consecutive-failure debounce, stale-data,
  interrupted-job, lease, shutdown, and notification tests pass.
- Poll and status endpoints launch no new remote work.
- Scheduler counts, offsets, intervals, and shutdown joins remain unchanged.
- No new worker, queue, database, or cross-process assumption is introduced.

## Phase 6 — narrow application and package boundaries

### Implementation

- Split the general actor-aware service facade by request domain under
  `backend/application/` (`devices`, `compute`, `access`, `vpn`, and `admin`).
  Route modules should import their domain service directly. Keep authorization
  checks in these services and add architecture tests that reject direct route
  access to persistence or device integrations.
- Normalize imports on the installed `backend` package. Remove test-wide
  `sys.path` insertion and launcher path bootstrapping only after all production
  and retained `_verify` entry points work with package imports.
- Expand mypy one completed package at a time, beginning with pure contract and
  state modules. Keep raw vendor payloads as `Mapping[str, object]` or a local
  typed boundary rather than adding unsafe casts.
- Add focused Ruff complexity checks only for refactored pure/service modules
  after they pass. Do not enable a blanket C901/PLR gate against vendor drivers
  or lower thresholds to manufacture a result.
- Update contributor guidance with the final import, service, state ownership,
  and testing rules.

### Acceptance

- Every HTTP mutation crosses an actor-aware application service.
- Authorization and owner-isolation suites pass unchanged.
- `python -m backend.run`, editable installation, pytest, retained mock-server
  tests, and the container entry point work without incidental path mutation.
- Static-check scope grows and no existing gate is weakened.

## Phase 7 — close the program

- Review every facade and remove transitional exports with no callers.
- Run import-cycle checks for Python and frontend modules.
- Run the optional complexity diagnostic again and record changed findings; do
  not treat fewer lines or diagnostics as proof of better behaviour.
- Re-run deployment measurements and compare them with Phase 0. Report only
  observed values and explain environmental differences.
- Raise the coverage floor only if the complete suite establishes a stable
  higher baseline.
- Update `README.md`, `docs/architecture.md`, `docs/frontend-state.md`,
  `docs/verification.md`, and operations documentation to match the final
  ownership and deployment shape.
- Mark each phase complete in this document with commit IDs, checks, skips,
  blockers, and intentionally deferred work. Archive the plan only after all
  accepted phases are represented in durable architecture and operations docs.

## Suggested commit boundaries

Each row is a logical commit or small commit series; never combine rows merely
to reduce commit count.

| Order | Conventional Commit intent |
|---:|---|
| 1 | `ci:` verify the documented Python support range |
| 2 | `test:` add missing refactor characterization guards |
| 3 | `test:` split browser specs and shared fixtures |
| 4 | `docs:` record the measured SQLite reassessment decision |
| 5 | `refactor(web):` split stylesheet layers |
| 6 | `test:` split Compute/Ansible contract suites by responsibility |
| 7–11 | `refactor(compute):` extract contracts, state, persistence, runner, and refresh orchestration |
| 12–14 | `refactor(ansible):` extract controller, inventory/approval, and execution boundaries |
| 15–20 | `refactor(web):` extract Compute state, model, API, Proxmox, views, and coordinator |
| 21–25 | `refactor(vpn):` extract model, repository, discovery/status, switching, and frontend modules |
| 26–29 | `refactor:` isolate poller, device-update, morning-update, and status state machines |
| 30–32 | `refactor:` split application services, normalize package imports, and expand typed/lint gates |
| 33 | `docs:` record final architecture and refactor outcomes |

## Completion criteria

The program is complete when:

- every accepted phase has focused tests, documentation, a reviewed atomic
  commit, and a successful full verification run;
- public HTTP, persistence, authorization, scheduling, device-action, and PWA
  contracts are unchanged unless a separately approved feature requires a
  change;
- production measurements are reported from real instrumentation, with no
  fabricated improvement claims;
- the SQLite decision is resolved by evidence, while history storage and the
  single-worker model remain unchanged unless their own triggers occur; and
- no transitional facade, duplicate state owner, duplicate implementation, or
  obsolete plan-only test remains.
