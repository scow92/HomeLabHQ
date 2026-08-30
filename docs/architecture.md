# Architecture

HomelabHQ combines a FastAPI application served by Uvicorn, a native-module
single-page application, a background poller, and a versioned JSON document
store. It requires no external database or message broker.

## Repository layout

```text
backend/
  app.py            compatibility launcher and ASGI application export
  run.py            production Uvicorn/TLS launcher
  asgi/             FastAPI factory, lifespan, middleware, models, and routers
  api/              route modules and transport-neutral response contracts
  context.py        authenticated actor and trusted-system context
  authorization.py  central resource-visibility policy
  services.py       actor-scoped application service boundary
  store.py          atomic, flock-guarded JSON document store
  history.py        bounded per-device chart history
  client_*.py       client discovery, merge, roster, and orchestration
  nac_service.py    network-access and firewall coordination
  device_updates.py async vendor update installation and progress
  compute.py        discovered VM/LXC domain and parent Device relationships
  ansible_integration.py restricted controller, inventory, and playbook boundary
  compute_maintenance.py persisted Compute maintenance jobs and result parsing
  poller.py         polling, history, and availability transitions
  drivers/          device-specific probes, entities, details, and actions
web/                 installable single-page web application
  styles/            ordered base, component, and view CSS layers
tests/               pytest behaviour and architecture coverage
_verify/             mock device servers exercised by pytest
e2e/                 Playwright critical-path coverage
```

## Request and ownership boundaries

FastAPI `APIRouter` registrations adapt the domain route catalogue and identify
public, authenticated, and administrator-only operations. Dependencies resolve
the HttpOnly session cookie to an `Actor`, enforce administrator policy, and
apply same-origin CSRF checks before the route runs. Application services apply
resource visibility before calling persistence or device integrations. Existing
synchronous route and integration calls run in Starlette's bounded worker-thread
pool, so SSH, Ansible, REST, SNMP, and filesystem calls do not block the ASGI
event loop.

FastAPI owns OpenAPI generation and exposes `/openapi.json`, Swagger UI at
`/docs`, and ReDoc at `/redoc`. API routes and the explicit `/api/{path}` JSON
404 are registered before static delivery. A missing filename with an extension
is a real 404; extensionless non-API browser locations retain the SPA fallback.

Request middleware creates an `X-Request-ID`, applies the browser security
headers, and records redacted structured request metadata. Central exception
handlers return safe JSON with `error`, stable `code`, and `requestId` fields.

Devices, dashboards, Access rosters, client history, notifications, bindings,
and push subscriptions are owner-scoped. Administrator visibility does not
implicitly turn owner-scoped operations into global mutations.

## Persistence

Most state is stored in `<data-dir>/homelabhq.json`, including users, sessions,
devices, encrypted credentials, dashboards, subscriptions, persistent
owner-scoped notifications, SSH host keys, and owner-scoped client rosters.
Writes use a process lock plus a cross-process
`flock`, a temporary file, and atomic replacement. A validated
`homelabhq.json.bak` is written before the main document is replaced. The main
document, validated backup, and shared lock are restricted to their owning
account with mode `0600`; startup also corrects permissive modes left by older
versions.

The document has an explicit `schemaVersion`. Ordered migrations run before
requests are accepted; malformed, unreadable, or newer-version documents cause
startup to fail rather than being replaced.

Schema version 3 adds `computeInstances`, `ansibleControllers`, and
`computeJobs`; later migrations add maintenance contracts, morning checks,
notifications, and appliance health. The store reached schema version 7 in
August 2026. That migration frequency met the documented SQLite reassessment
trigger, but no representative production capacity or latency evidence is
available. [ADR 0001](decisions/0001-retain-json-pending-production-baseline.md)
therefore retains JSON provisionally while deployment measurements are
collected.

Schema version 6 adds the bounded `notifications` collection. Web-push event
creation and notification-centre persistence share the existing push service;
there is no separate client-side notification ledger.

Proxmox package catalogues and the latest direct-maintenance operation remain
fields on their existing parent Device. The operation service still owns
execution; persistence supplies reload/restart recovery and does not introduce
a second job engine or collection.

Chart history is stored separately under `<data-dir>/history/<device-id>.json`.
Raw instance, credential, TLS, and VAPID key material lives under
`<data-dir>/secrets/`. Backups must therefore include the complete data
directory.

## Driver model

Drivers subclass `Driver` from `backend/drivers/base.py` and declare compatible
transports. Detection opens one transport connection, calls compatible
`probe()` implementations, and ranks their confidence scores. The selected
driver describes sensors and opt-in controls through `entities()` and may add
structured tables through `detail()`.

Transport code owns SSH, SNMP, HTTP, REST, and bounded ICMP probe behaviour.
Drivers own vendor-specific field mappings and actions. Keeplink uses ICMP for
background availability while retaining HTTP as its management transport.
Scheduled Keeplink client discovery reads only the MAC forwarding table; rich
detail reads fan out to the remaining management pages only for an explicitly
opened device view. Mock servers model documented vendor endpoints so contracts
remain deterministic in verification.

Software updates are an opt-in driver capability. Discovery stays on the
driver's primary transport. The application update service owns asynchronous
operation state and any privileged secondary transport, keeping credentials
and worker lifecycle out of vendor payload/rendering code.

Compute is a separate workload domain, not a Device subtype. Virtualization
drivers may opt into `compute_instances()`. The Compute service persists those
results with a parent Device relationship and stale/unavailable discovery
states. Proxmox's existing Device monitoring and SSH update service are not
routed through Ansible. Compute renders that service's persisted safe per-node
maintenance summary and calls the same service for host updates and refreshes;
there is no second Proxmox updater in the Ansible subsystem.

The Ansible boundary has one configured controller, an Ansible-produced
inventory cache, confirmed optional Compute mappings, and a fixed operation
allowlist. Persisted background jobs execute only discovered and approved
playbooks against discovered, optionally approval-restricted targets. Docker
project updates use a validated `pull`/`build` mode with generic or compatible
separate approvals. Jobs prefer structured callback/set-stats data, fall back
to JSON objects in sanitized callback output, and retain PLAY RECAP plus raw
sanitized output. The result contract is described in [Compute](compute.md).

## Frontend state

Each feature module owns the state that drives its view. Cross-feature work
passes data and callbacks or emits a named UI event; one feature must not import
another feature's mutable state or directly manipulate its DOM. The detailed
client-module ownership rule is documented in
[frontend-state.md](frontend-state.md).

The CSS layering trigger was met at 1,225 lines in August 2026. The original
cascade is preserved across ordered `base.css`, `components.css`, and
`views.css` files; this is a responsibility split, not a visual redesign.

## Deliberate architecture boundaries

These changes remain deferred until their measurable trigger is met:

| Area | Reassessment trigger |
|---|---|
| SQLite | Multiple application processes, material JSON write latency, frequent migrations, a large roster, complex filtering/query needs, or increasingly expensive authorization scans. Move core metadata first; history may remain specialized. |
| History storage | Longer retention or materially more queryable client/event history is required. Give that history a separate bounded store before increasing main-document churn. |

These are decision points, not missing implementation.

## Process model

Production deliberately uses one Uvicorn worker. The poller, morning-update
scheduler, login throttle, diagnostic ring, and active operation coordination
are process-local, while several long-running jobs also have persisted recovery
state. Multiple workers would duplicate schedulers and split coordination.
Before increasing the worker count, move scheduling and live coordination to a
single external worker or durable queue and make every process-local lock and
metric explicitly multi-process safe.

## Key initialization

VAPID keypair creation prepares and flushes both files before publication and
is serialized across threads and cooperating processes. Existing partial,
malformed, or mismatched keypairs fail closed without automatic rotation
because replacing VAPID keys can invalidate browser subscriptions.
