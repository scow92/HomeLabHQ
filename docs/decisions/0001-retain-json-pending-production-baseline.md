# ADR 0001: Retain JSON with targeted history compaction

Status: accepted

Date: 2026-08-30

## Context

HomelabHQ stores core state in one versioned JSON document. A representative
production baseline was reviewed privately. It did not establish a capacity,
query, authorization, or process-model problem requiring SQLite. It identified
duplicated successful Docker discovery history as a bounded persistence issue:
the retained stdout and structured result repeat the canonical current Docker
projection stored on each Compute instance.

## Decision

Retain the JSON store and compact only superseded successful Docker discovery
diagnostics. The newest successful discovery for each Compute instance remains
complete. Older successful discoveries retain their identifiers, operation,
state, timestamps, summary, recap, target, requesting-user information, and
other audit metadata while duplicated stdout, stderr, and structured results
are removed.

Failed and incomplete discoveries retain full diagnostics. SQLite, separate
history storage, history redesign, and multi-process support remain deferred.
After this fix is deployed, repeat the representative production measurements
privately before Phase 2 begins.

## Consequences

- The bounded duplicated data is reduced without a persistence-engine or
  history-storage change.
- Existing maintenance history remains visible and audit information is
  retained.
- Full diagnostics remain available for the latest successful discovery and
  for failed or incomplete discoveries.
- Existing atomic replacement, backup validation, file locking, schema
  validation, and one-worker assumptions remain in force.
- Post-deployment evidence remains private and must be collected before Phase 2.

## Deferred alternatives

- SQLite migration is deferred because the representative private baseline did
  not establish a problem requiring it.
- Separate history storage or broader history redesign is deferred because the
  identified issue is addressed by targeted compaction.
- Multi-process support remains deferred; this decision does not change the
  supported single-worker model.
