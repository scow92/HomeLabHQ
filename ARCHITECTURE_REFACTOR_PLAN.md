# HomelabHQ — Architecture Refactor Status and Follow-up Plan

Last reviewed: 2026-07-19

## Conclusion

Phases 1–8 and the Phase 4 compatibility cleanup are implemented. The
application now has the intended security and store protections, actor-scoped
services, declarative HTTP routes, separated client-discovery/roster/NAC
responsibilities, typed boundary values, versioned persistence, decoupled
client modules, and production hardening.

There is no remaining repository architecture work. HomelabHQ should remain a
standard-library HTTP server, native browser modules, and a JSON document store
until the documented SQLite decision triggers are met.

## Implemented phases

| Phase | Status | Evidence in the repository |
|---|---|---|
| 0. Safety baseline | Implemented, with measurement follow-up | `pyproject.toml` has a 47.9% coverage ratchet, CI verifies Python 3.11–3.13, and `docs/verification.md` owns the full command and deployment measurements |
| 1. Security and data integrity | Implemented | owner-scoped `clientRosters`, fail-safe atomic store writes and backups, resolved static paths, atomic setup, bounded JSON parsing |
| 2. Application policy | Implemented | `context.py`, `authorization.py`, `services.py`, `errors.py`, and central HTTP error mapping |
| 3. HTTP decomposition | Implemented | `backend/http/`, declarative `backend/api/*_routes.py`, and route-level authentication policy |
| 4. Client discovery, roster, and NAC | Implemented | `client_discovery.py`, `client_merge.py`, `client_roster.py`, `client_service.py`, and `nac_service.py`; the former owner-ID adapters are removed |
| 5. Typed domain contracts | Implemented | `domain.py` values, typed poller/device boundaries, mypy verification, and driver contract tests |
| 6. Persistence maturity | Implemented | schema migrations, atomic batches/no-op writes, retention limits, integrity checks, store metrics, and backup/restore guidance |
| 7. Frontend decoupling | Implemented | focused `web/js/clients/` modules, acyclic import test, Playwright critical-path coverage, and `docs/frontend-state.md` |
| 8. Deployment and observability | Implemented | structured redacted logs, liveness/readiness endpoints, poller/push metrics, graceful shutdown tests, hardened Compose, and Dependabot |

The phase tests are intentionally grouped in `tests/test_phase1.py` through
`tests/test_phase8.py`; they cover each phase's primary invariants.

## Ongoing operational work

### Maintain the verification baseline

The repository's verification workflow now exercises Python 3.11–3.13, has a
47.9% coverage ratchet, and runs the focused Playwright suite once on Python
3.13. Performance/store measurements remain deployment-specific evidence,
rather than portable repository constants.

- Record representative poll duration, store bytes/write rate, and API latency
  for production-like releases or deployments.
- Run the full command in `docs/verification.md` in an environment with Python
  and browser dependencies before merging production follow-up work.

### Reassess only when a stated trigger occurs

These are deliberate deferrals, not currently missing implementation:

| Trigger | Reassessment |
|---|---|
| Multiple application processes, complex filtering, frequent migrations, a large roster, or material JSON write latency | Move core metadata to SQLite; history can remain specialized initially. |
| `web/styles.css` grows beyond roughly 1,200 lines or acquires theme variants | Split CSS into base, component, and view layers. |
| Longer or more queryable client/event history is required | Give that history its own bounded store before increasing retained data in the main document. |

## Completed delivery order

1. Removed the Phase 4 compatibility adapters. Client background refresh now
   uses `client_service`, and `client_roster` is the sole roster persistence API.
2. Kept release/deployment measurements in `docs/verification.md`, where they
   can be captured as environment-specific evidence rather than source facts.
3. Reassessed SQLite, CSS layering, and history storage. No stated trigger is
   currently met, so no speculative migration is planned.

## Documentation ownership

This file is the authoritative status and follow-up plan. The README owns
operator-facing architecture, backup/restore, capacity limits, and deployment
guidance. `docs/verification.md` owns the verification command, and
`docs/frontend-state.md` owns the frontend state rule. Historical review and
one-off migration plans were removed because their completed recommendations
are represented here and in the implementation history.
