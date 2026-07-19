# HomelabHQ — Architecture Refactor Status and Follow-up Plan

Last reviewed: 2026-07-19

## Conclusion

Phases 1–8 are implemented. The application now has the intended security and
store protections, actor-scoped services, declarative HTTP routes, separated
client-discovery/roster/NAC responsibilities, typed boundary values, versioned
persistence, decoupled client modules, and production hardening.

The work that remains is completion and evidence work, not another broad
architecture rewrite. HomelabHQ should remain a standard-library HTTP server,
native browser modules, and a JSON document store until the documented SQLite
decision triggers are met.

## Implemented phases

| Phase | Status | Evidence in the repository |
|---|---|---|
| 0. Safety baseline | Implemented, with measurement follow-up | `pyproject.toml`, `constraints.txt`, discoverable `tests/`, GitHub Actions verification, and `docs/verification.md` |
| 1. Security and data integrity | Implemented | owner-scoped `clientRosters`, fail-safe atomic store writes and backups, resolved static paths, atomic setup, bounded JSON parsing |
| 2. Application policy | Implemented | `context.py`, `authorization.py`, `services.py`, `errors.py`, and central HTTP error mapping |
| 3. HTTP decomposition | Implemented | `backend/http/`, declarative `backend/api/*_routes.py`, and route-level authentication policy |
| 4. Client discovery, roster, and NAC | Implemented, with compatibility cleanup pending | `client_discovery.py`, `client_merge.py`, `client_roster.py`, `client_service.py`, and `nac_service.py` |
| 5. Typed domain contracts | Implemented, with migration cleanup pending | `domain.py` values and driver contract tests |
| 6. Persistence maturity | Implemented | schema migrations, atomic batches/no-op writes, retention limits, integrity checks, store metrics, and backup/restore guidance |
| 7. Frontend decoupling | Implemented, with browser-test follow-up pending | focused `web/js/clients/` modules, acyclic import test, and `docs/frontend-state.md` |
| 8. Deployment and observability | Implemented | structured redacted logs, liveness/readiness endpoints, poller/push metrics, graceful shutdown tests, hardened Compose, and Dependabot |

The phase tests are intentionally grouped in `tests/test_phase1.py` through
`tests/test_phase8.py`; they cover each phase's primary invariants.

## Remaining work

### 1. Retire Phase 4 compatibility adapters

`backend/clients.py` and the roster aliases at the end of `backend/nac.py`
preserve the former owner-ID APIs. Production request paths already use the new
services, but these adapters leave two ways to reach the same responsibilities.

- Identify any out-of-tree consumers before removal.
- Announce a deprecation window if they are supported integrations.
- Remove the adapters and their compatibility tests once that window closes.
- Keep `nac_service.py` focused on firewall/NAC actions and
  `client_roster.py` as the only roster persistence API.

This is the only remaining structural cleanup from the completed phases.

### 2. Add browser-level critical-path tests

Phase 7's module and import-graph checks pass, but the planned browser-level
coverage has not been added. Use a lightweight headless-browser suite for:

- initial setup and login;
- last-known device state after a failed refresh;
- client filtering and bulk actions;
- keyboard modal behaviour and route navigation; and
- the PWA offline shell.

Keep these tests focused on public workflows; Python unit tests remain the
right place for routing, authorization, persistence, and driver contracts.

### 3. Complete the type migration

The most important boundaries now have dataclasses and typed wire shapes.
`DevicePollResult` still exposes mapping-style compatibility methods while old
call sites are migrated, and several driver/device internals remain intentionally
dictionary-oriented.

- Replace remaining mapping-style `DevicePollResult` use with typed access.
- Add type checking to the documented verification command once the current
  annotations are clean enough to make it useful.
- Continue to use flexible mappings only for vendor-specific payloads at the
  driver boundary.

Do not force vendor responses into a universal schema merely to satisfy a type
checker.

### 4. Make the verification baseline enforceable

The repository has a verification workflow, but the claimed Python 3.11–3.13
support is currently exercised only on 3.11 and 3.13. Coverage is reported but
has no minimum, and performance/store measurements are documented as manual
baselines rather than recorded release evidence.

- Add Python 3.12 to the CI matrix, or narrow the support statement.
- Set a coverage floor only after recording a stable baseline; ratchet it up
  rather than choosing an arbitrary target.
- Record representative poll duration, store bytes/write rate, and API latency
  for production-like releases or deployments.
- Run the full command in `docs/verification.md` in an environment with Python
  and Docker before merging this review's follow-up work.

### 5. Reassess only when a stated trigger occurs

These are deliberate deferrals, not currently missing implementation:

| Trigger | Reassessment |
|---|---|
| Multiple application processes, complex filtering, frequent migrations, a large roster, or material JSON write latency | Move core metadata to SQLite; history can remain specialized initially. |
| `web/styles.css` grows beyond roughly 1,200 lines or acquires theme variants | Split CSS into base, component, and view layers. |
| Longer or more queryable client/event history is required | Give that history its own bounded store before increasing retained data in the main document. |

## Recommended delivery order

1. Establish the verification evidence: run the suite, add 3.12 CI, and choose
   a coverage ratchet from measured results.
2. Add the small browser critical-path suite.
3. Deprecate then remove the Phase 4 compatibility adapters.
4. Finish typed-call-site migration and introduce static type checking.
5. Revisit SQLite, CSS layering, or history storage only when their triggers
   occur.

## Documentation ownership

This file is the authoritative status and follow-up plan. The README owns
operator-facing architecture, backup/restore, capacity limits, and deployment
guidance. `docs/verification.md` owns the verification command, and
`docs/frontend-state.md` owns the frontend state rule. Historical review and
one-off migration plans were removed because their completed recommendations
are represented here and in the implementation history.
