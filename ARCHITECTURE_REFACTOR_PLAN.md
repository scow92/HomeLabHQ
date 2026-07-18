# HomelabHQ — Phased Architecture Refactoring Plan

Reviewed as a senior Python architecture assessment, prioritizing
maintainability, simplicity, security, and testability.

## Architectural assessment

HomelabHQ is appropriately lightweight for its deployment model: a threaded
Python HTTP server, native browser modules, a JSON document store, and
protocol-specific drivers. The driver/transport split, concurrent polling,
external history files, credential encryption, and small frontend architecture
are sound.

The main architectural risks are:

1. Incomplete multi-user isolation in the global client roster.
2. Persistence failures that can silently reset the store.
3. Unsafe static-path containment checking.
4. Business rules and authorization distributed through a large HTTP handler.
5. Read operations that also perform network discovery and persistent mutations.
6. A custom verification suite without conventional automated testing or CI.
7. Extensive untyped dictionary contracts between drivers, services,
   persistence, and HTTP responses.

The goal should be evolutionary refactoring—not adopting a web framework,
database, or frontend framework merely to reduce line counts.

## Phase 0 — Establish safety and baselines

**Objective:** Make later refactoring measurable and safe.

### Work

- Introduce `pyproject.toml` with pytest, coverage configuration, Ruff or an
  equivalent linter, and optional static type checking.
- Convert `_verify/*_test.py` scripts into discoverable tests while preserving
  their mock servers.
- Add CI for Python compilation, tests, linting, and dependency vulnerability
  scanning.
- Capture baseline test runtime, coverage, poll-cycle duration, store size,
  write frequency, and key API latency.
- Document supported Python versions and operating assumptions.
- Add a dependency lock or constraints file for reproducible container builds.

### Initial regression tests

- Authentication and session expiry.
- Admin/member authorization.
- Device and dashboard ownership.
- Client-roster access.
- Static path traversal.
- Malformed and oversized JSON requests.
- Concurrent first-run setup.
- Corrupt store behavior.
- Poll debounce and transition notifications.
- Atomic store updates.
- Driver registry invariants.

### Exit criteria

- Tests run through one documented command.
- CI runs on every change.
- Existing behavior is covered sufficiently to refactor confidently.
- Production dependencies are reproducible.

## Phase 1 — Correct security and data-integrity defects

**Objective:** Close the highest-risk issues without broad structural changes.

### 1.1 Scope the persistent client roster

The roster under `meta.nacClients` is global, while most other resources are
owner-scoped. Authenticated members can reach unscoped history, event, ignore,
and forget operations.

Choose and document one product model:

- Per-owner rosters, recommended for genuine multi-tenancy.
- Instance-global roster restricted to administrators.
- Shared roster with explicit per-user permissions.

For per-owner storage:

```text
clientRosters:
  owner_id:
    mac:
      ...
```

Require an actor or ownership scope for every roster operation, including
reads, exports, notifications, ignores, and deletions.

### 1.2 Make corrupt persistence fail safe

- Treat a missing file as an empty initial store.
- Treat an existing invalid or unreadable file as fatal.
- Never convert a parsing or permission error into an empty document.
- Preserve a previous known-good copy.
- `fsync()` the temporary file and parent directory during replacement.
- Log actionable errors without secrets.
- Validate the top-level store schema after reading.

### 1.3 Fix static path containment

Replace string-prefix checking with resolved-path containment using
`Path.resolve()` or `os.path.commonpath()`.

Test `..` traversal, sibling directories sharing the `web` prefix, encoded path
components, repeated separators, and symlinks if they are allowed in the static
directory.

### 1.4 Make initial setup atomic

Replace the `has_any_user()` followed by `create_user()` sequence with one
transactional store mutation that confirms no users exist, creates exactly one
initial administrator, and returns a conflict if setup has already occurred.

### 1.5 Harden request parsing

- Set a small maximum JSON body size.
- Reject invalid `Content-Length`.
- Reject valid JSON that is not an object where an object is required.
- Validate content type for JSON endpoints.
- Avoid returning raw exception strings for unexpected failures.
- Redact request-log errors and traces.

### Exit criteria

- Cross-user roster access is impossible by design and test.
- Corrupt persistence cannot be overwritten silently.
- Traversal tests fail against the old code and pass against the fix.
- Concurrent setup creates exactly one initial administrator.
- Request-size and malformed-body behavior is deterministic.

## Phase 2 — Centralize application policies

**Objective:** Remove authorization and error-policy decisions from individual
route branches.

### 2.1 Add an actor/request context

Introduce a small immutable model:

```python
@dataclass(frozen=True)
class Actor:
    user_id: str
    role: Role

    @property
    def is_admin(self) -> bool: ...
```

Replace parallel `owner_id, is_admin` arguments where practical.

### 2.2 Create an authorization boundary

Provide explicit operations such as:

```python
authorize.device(actor, device_id)
authorize.dashboard(actor, dashboard_id)
authorize.client(actor, mac)
authorize.nac(actor)
```

These should either return the authorized resource or raise a typed
`NotFound`/`Forbidden` error. Keep resource-visibility policy centralized so
new endpoints cannot accidentally bypass it.

### 2.3 Define application exceptions

Create a compact hierarchy:

```text
ApplicationError
├── ValidationError
├── AuthenticationRequired
├── Forbidden
├── NotFound
├── Conflict
└── UpstreamUnavailable
```

Map these to HTTP responses once. Unexpected exceptions should produce a
generic 500 response and a redacted internal log entry.

### 2.4 Move invariants into domain services

Rules such as initial administrator uniqueness, preservation of the last
administrator, dashboard ownership, driver/transport compatibility, NAC device
capabilities, and roster access should not exist only in `app.py`.

### Exit criteria

- Route handlers no longer implement ownership rules manually.
- Public service operations cannot be called without an actor or explicit
  trusted-system context.
- Expected failures use typed exceptions.
- HTTP status behavior is tested centrally.

## Phase 3 — Decompose the HTTP layer

**Objective:** Make adding and testing endpoints straightforward while
retaining the standard-library server.

### Proposed structure

```text
backend/
  main.py
  http/
    server.py
    handler.py
    router.py
    requests.py
    responses.py
    static.py
  api/
    auth_routes.py
    device_routes.py
    dashboard_routes.py
    client_routes.py
    nac_routes.py
    push_routes.py
    admin_routes.py
```

### Work

- Replace method-specific `if` ladders with a small declarative router.
- Support named path parameters.
- Declare authentication and authorization policy alongside each route.
- Centralize JSON decoding, query/path extraction, error mapping, JSON/file
  responses, security headers, and request logging.
- Move certificate and static-file delivery out of the API handler.
- Keep route functions thin: validate transport input, call an application
  service, and serialize the result.

Avoid building a general-purpose framework. Implement only the routing features
HomelabHQ needs.

### Exit criteria

- The main handler is primarily dispatch and protocol plumbing.
- Each API domain has an isolated route module.
- Route tests do not need a real listening socket.
- Authorization and error mapping cannot be accidentally omitted.

## Phase 4 — Separate client discovery, roster state, and NAC

**Objective:** Reduce the most tightly coupled backend area.

### Proposed responsibilities

```text
client_discovery.py
  Query eligible devices and return ClientObservation values.

client_merge.py
  Purely merge observations by MAC.

client_roster.py
  Persist identity, online state, history, names, notes, and notifications.

nac_service.py
  Manage aliases, membership, enforcement, and DNS synchronization.

client_service.py
  Orchestrate authorized reads, refreshes, exports, and edits.
```

### Behavioral change

`GET /api/clients` should not implicitly poll network devices, mutate
persistence, create connection events, or trigger notifications. Background
polling should record observations, `GET /api/clients` should read the latest
roster, and an explicit refresh action should perform live discovery.

Background jobs should use an explicit trusted context rather than passing
`owner_id=None, is_admin=True`.

### Exit criteria

- Discovery and merge logic are pure and independently testable.
- Roster mutations occur only through the roster service.
- NAC does not own general client-history behavior.
- GET endpoints are read-only unless explicitly documented otherwise.

## Phase 5 — Introduce typed domain contracts

**Objective:** Make dictionary-heavy boundaries understandable and enforceable.

### Start at unstable boundaries

Prioritize types for:

- `DevicePollResult`
- `DeviceState`
- `ClientObservation`
- `ClientRosterRecord`
- `EntityDescription`
- `DriverDetail`
- `NacConfiguration`
- `HistoryPoint`
- `AlertRule`

Use dataclasses internally. Typed dictionaries are acceptable for serialized
API shapes.

### Driver contracts

Add contract tests for every registered driver:

- Globally unique, nonempty ID.
- Supported transport declaration.
- JSON-serializable entity descriptions.
- Stable entity keys.
- Valid capability declarations.
- Valid detail-table structure.
- Predictable authentication and connection failure behavior.
- No secret material in returned errors.

Move normalization to boundaries:

```text
driver response -> validated domain value -> application logic -> API serializer
```

Avoid forcing every vendor response into an overly rigid schema. Normalize only
the fields consumed by shared code.

### Exit criteria

- Core services have useful type annotations.
- Polling and roster logic no longer depend on undocumented dictionary shapes.
- Driver contract violations fail during tests.

## Phase 6 — Strengthen persistence without prematurely adopting a database

**Objective:** Keep the simple one-container model while improving durability
and scalability.

### Short-term JSON-store work

- Add a store schema version and explicit migrations.
- Use atomic batch operations for related records.
- Avoid a disk rewrite when a mutator made no change.
- Bound sessions, push subscriptions, client records or their retention period,
  event history, and SSH host-key records where appropriate.
- Add backup and restore documentation.
- Add a startup integrity check.
- Add observability for write duration and document size.

### SQLite decision point

Do not migrate immediately. Reassess SQLite when one or more conditions become
true:

- Multi-process deployment is required.
- Query/filter requirements grow materially.
- The roster becomes large.
- Schema migrations become frequent.
- JSON write latency becomes operationally significant.
- Per-owner authorization requires increasingly complex document scans.

If adopted, migrate authentication, devices, dashboards, and roster metadata
first. History can remain in specialized files until its query requirements
justify moving.

### Exit criteria

- Store failures are diagnosable and recoverable.
- Schema changes are versioned and tested.
- The JSON-store capacity boundary is documented.

## Phase 7 — Finish frontend decoupling

**Objective:** Remove the remaining module cycle and keep the framework-free
frontend maintainable.

### Work

Split the large client module:

```text
web/js/clients/
  store.js
  api.js
  grid.js
  actions.js
  filters.js
  edit-modal.js
  nac-setup.js
  index.js
```

`edit-modal.js` should receive a client and completion callback instead of
importing mutable state from its parent module.

Document the frontend state rule:

1. Mutate the owning module's state.
2. Invoke that module's render function.
3. Do not directly mutate another feature module's DOM.

Add lightweight browser tests for login/setup, last-known device state during
request failure, modal keyboard behavior, client filtering and bulk actions,
route navigation, and the PWA offline shell.

Avoid a framework unless view/state complexity grows well beyond the present
design.

### Exit criteria

- No circular frontend imports.
- Large feature modules have focused responsibilities.
- Critical user workflows have browser-level coverage.

## Phase 8 — Deployment and observability hardening

**Objective:** Improve production diagnostics and reduce compromise impact.

### Work

- Add structured logging with request ID, route name, status and duration,
  poll/device identifiers, and automatic secret redaction.
- Separate `/healthz` process liveness from `/readyz` store and poller readiness.
- Track the last successful poll cycle, per-device duration/failures, store
  writes, and push delivery failures.
- Add graceful shutdown tests for poller and HTTP threads.
- Use a dedicated container identity and correctly owned data volume where
  feasible.
- Drop unnecessary Linux capabilities and use a read-only root filesystem with
  writable `/data` and temporary mounts.
- Document trusted-proxy behavior and the recommended reverse-proxy setup.
- Add dependency and base-image update automation.

### Exit criteria

- Operators can distinguish application, storage, device, and push failures.
- Logs do not leak credentials or authorization headers.
- Container privileges are explicitly justified and minimized.

## Recommended delivery order

| Order | Phase | Risk | Expected size |
|---:|---|---|---|
| 1 | Testing baseline | Low | Medium |
| 2 | Security and integrity fixes | High priority | Medium |
| 3 | Authorization boundary | Medium | Medium |
| 4 | HTTP decomposition | Medium | Medium-large |
| 5 | Client/NAC separation | Medium-high | Large |
| 6 | Typed contracts | Low-medium | Incremental |
| 7 | Persistence maturity | Medium | Medium |
| 8 | Frontend decoupling | Low | Medium |
| 9 | Deployment hardening | Medium | Medium |

Each phase should be delivered separately from feature work. Security fixes
should be narrowly scoped and merged before larger architectural refactors. The
testing baseline should precede behavior-preserving module moves.
