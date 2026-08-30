# ADR 0001: Retain JSON pending a production baseline

Status: provisional decision

Date: 2026-08-30

## Context

HomelabHQ stores core state in one versioned JSON document and keeps chart
history in separate bounded files. The architecture requires a SQLite
reassessment when any of these conditions occurs:

- multiple application processes are required;
- JSON write latency becomes operationally significant;
- migrations become frequent;
- rosters or authorization scans become materially large; or
- filtering and query requirements become complex.

The migration-frequency condition has occurred. The store moved from schema
version 1 on 2026-07-19 to version 7 on 2026-08-23 through six ordered
migrations covering VPN history, Compute and Ansible state, maintenance
contracts, morning checks, notifications, and appliance health.

The repository does not contain representative production values for document
size, write duration/rate, roster size, poll duration, authorization scan cost,
or API latency. Local test timings are not substitutes for those measurements.
The running application now exposes safe process-local observations at the
administrator-only `GET /api/diagnostics/metrics` endpoint, and
`docs/operations.md` documents the collection command.

## Decision

Retain the JSON store for the current refactor program. Do not implement a
SQLite migration until a representative deployment baseline is captured and
shows an observed storage, query, authorization, or process-model problem that
SQLite addresses.

This is a provisional retain decision, not a conclusion that the existing
store will scale indefinitely. Reassess it after collecting:

- `store.metrics()` observations after representative poll and mutation work;
- main-document and complete data-directory sizes;
- device and roster counts;
- representative poll-cycle duration; and
- sample counts plus p50/p95 latency for `/api/session`, `/api/devices`, and
  `/api/clients`.

If a migration is approved, move core metadata first. Keep specialized chart
history separate unless its longer-retention or richer-query trigger is also
met. Design and test forward migration, startup failure, backup, rollback, file
permissions, and owner-isolation before replacing the JSON writer.

## Consequences

- Structural refactoring may proceed without a persistence-engine change.
- No unmeasured performance or capacity claim is recorded.
- Existing atomic replacement, backup validation, file locking, schema
  validation, and one-worker assumptions remain in force.
- New schema changes still require an ordered migration and tests while this
  decision remains active.
- A future SQLite proposal must cite captured deployment evidence and state
  which trigger it resolves.

## Deferred alternatives

- Migrating immediately because six migrations exist is rejected: migration
  frequency triggers reassessment but does not prove that SQLite improves the
  current operational shape.
- Moving chart history into SQLite is deferred because no longer-retention or
  query requirement exists.
- Supporting multiple application workers is deferred until schedulers and
  live coordination move to a single external worker or durable queue.
