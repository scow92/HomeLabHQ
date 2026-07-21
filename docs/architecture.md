# Architecture

HomelabHQ combines a standard-library threaded HTTP server, a native-module
single-page application, a background poller, and a versioned JSON document
store. It requires no external database or message broker.

## Repository layout

```text
backend/
  app.py            application startup and server wiring
  http/             request parsing, routing, responses, and static delivery
  api/              route modules grouped by API domain
  context.py        authenticated actor and trusted-system context
  authorization.py  central resource-visibility policy
  services.py       actor-scoped application service boundary
  store.py          atomic, flock-guarded JSON document store
  history.py        bounded per-device chart history
  client_*.py       client discovery, merge, roster, and orchestration
  nac_service.py    network-access and firewall coordination
  poller.py         polling, history, and availability transitions
  drivers/          device-specific probes, entities, details, and actions
web/                 installable single-page web application
tests/               pytest behaviour and architecture coverage
_verify/             mock device servers exercised by pytest
e2e/                 Playwright critical-path coverage
```

## Request and ownership boundaries

Declarative routes identify public, authenticated, and administrator-only
operations. Authenticated requests resolve an `Actor`, and application services
apply resource visibility before calling persistence or device integrations.

Devices, dashboards, Access rosters, client history, notifications, bindings,
and push subscriptions are owner-scoped. Administrator visibility does not
implicitly turn owner-scoped operations into global mutations.

## Persistence

Most state is stored in `<data-dir>/homelabhq.json`, including users, sessions,
devices, encrypted credentials, dashboards, subscriptions, SSH host keys, and
owner-scoped client rosters. Writes use a process lock plus a cross-process
`flock`, a temporary file, and atomic replacement. A validated
`homelabhq.json.bak` is written before the main document is replaced.

The document has an explicit `schemaVersion`. Ordered migrations run before
requests are accepted; malformed, unreadable, or newer-version documents cause
startup to fail rather than being replaced.

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

Transport code owns SSH, SNMP, HTTP, and REST connection behaviour. Drivers own
vendor-specific field mappings and actions. Mock servers model documented
vendor endpoints so contracts remain deterministic in verification.

## Frontend state

Each feature module owns the state that drives its view. Cross-feature work
passes data and callbacks or emits a named UI event; one feature must not import
another feature's mutable state or directly manipulate its DOM. The detailed
client-module ownership rule is documented in
[frontend-state.md](frontend-state.md).

## Deliberate architecture boundaries

These changes remain deferred until their measurable trigger is met:

| Area | Reassessment trigger |
|---|---|
| SQLite | Multiple application processes, material JSON write latency, frequent migrations, a large roster, complex filtering/query needs, or increasingly expensive authorization scans. Move core metadata first; history may remain specialized. |
| CSS layers | `web/styles.css` grows beyond roughly 1,200 lines or theme variants make one file difficult to maintain. Split into base, component, and view layers only then. |
| History storage | Longer retention or materially more queryable client/event history is required. Give that history a separate bounded store before increasing main-document churn. |

These are decision points, not missing implementation.

## Known reliability follow-up

First-use VAPID keypair creation should become atomic across threads and
cooperating processes. Existing partial, malformed, or mismatched keypairs must
fail closed without automatic rotation because replacing VAPID keys can
invalidate browser subscriptions. This is a focused reliability improvement,
not a storage or service-boundary redesign.
