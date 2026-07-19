# HomelabHQ — Architecture Refactor Status and Follow-up Plan

Last reviewed: 2026-07-19

## Conclusion

Phases 1–8 and the Phase 4 compatibility cleanup are implemented. The
application now has the intended security and store protections, actor-scoped
services, declarative HTTP routes, separated client-discovery/roster/NAC
responsibilities, typed boundary values, versioned persistence, decoupled
client modules, and production hardening.

There is no justified architecture migration remaining. HomelabHQ should remain
a standard-library HTTP server, native browser modules, and a JSON document
store until the documented SQLite decision triggers are met.

A repository review on 2026-07-19 did identify five actionable security,
reliability, and verification follow-ups. They are corrections within the
current architecture, not triggers for the deferred migrations below.

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

## Actionable follow-up review

Work through these findings in priority order. Each should be delivered as a
focused change with regression tests, documentation where behaviour changes,
the full verification entry point, and an atomic Conventional Commit.

### 1. Close remaining owner-isolation gaps

**Why it matters:** Actor checks protect the selected resource at the service
boundary, but some downstream operations broaden back to instance-global
state. In particular:

- `devices.set_client_binding()` removes the selected MAC from every owner's
  devices, and `poller.enforce_bindings()` consumes the resulting global map;
- administrators can associate a device with a dashboard owned by a different
  user because the two resources are authorized independently;
- administrator NAC lookup can select the first configured firewall globally,
  despite the Access roster being explicitly per-owner; and
- push unsubscribe removes an endpoint without confirming that the actor owns
  the subscription.

An isolated temporary-store reproduction confirmed that binding a MAC on
Alice's AP removed Bob's existing binding.

**Estimated effort:** 3–5 engineer-days, including owner-boundary regression
tests.

**Risks:** Existing administrator workflows may rely on global visibility.
Administrative mutations need an explicit owner context, and invalid existing
cross-owner references may need safe normalization.

**Decision:** Do now. These are multi-user correctness and authorization issues.

### 2. Define safe user deprovisioning

**Why it matters:** `auth.delete_user()` removes the user and sessions only.
Owned devices, credential blobs, dashboards, client-roster data, and push
subscriptions remain. Because the poller reads every stored device, deleting an
account does not stop HomelabHQ from accessing that user's infrastructure. The
temporary-store review reproduced retention of all five resource categories.

**Estimated effort:** 2–4 engineer-days.

**Risks:** An automatic cascade could destroy monitoring configuration and
history. The safest initial policy is to revoke sessions and subscriptions
immediately, then require explicit transfer or deletion of owned resources
before deleting the account.

**Decision:** Do now, after documenting the transfer-versus-delete policy.

### 3. Harden password changes and session revocation

**Why it matters:** `auth.set_password()` replaces only `passHash`; existing
sessions remain valid for up to 30 days. Password creation and changes also
accept any non-empty value. A compromised session can therefore survive a
password change and can set a weak replacement password.

**Estimated effort:** 1–2 engineer-days.

**Risks:** Revoking the current session can produce confusing UX. Require the
current password for self-service changes, enforce one documented minimum, and
deliberately choose whether to preserve the current session while revoking all
others.

**Decision:** Do now.

### 4. Make `scripts/verify.sh` authoritative and CI-equivalent

**Why it matters:** The required local entry point does not currently run the
same checks as `docs/verification.md` and CI. It omits compile checks, coverage
enforcement, and dependency auditing; invokes `mypy .`, which overrides the
configured module scope; and can return success after silently skipping lint or
pytest when launchers are unavailable.

During the review, plain `./scripts/verify.sh` ran zero lint and pytest checks
before Playwright failed because the system Python lacked `cryptography`. With
the repository virtual environment selected, `mypy .` reported 48 out-of-scope
errors. Direct invocation of the documented checks produced 51 passing pytest
tests with 49.72% branch coverage, a clean configured mypy run, a clean Ruff
run, and no known dependency vulnerabilities. Playwright remained externally
blocked because Chromium was absent; the documented completion command is
`npx playwright install --with-deps chromium`.

**Estimated effort:** 0.5–1 engineer-day.

**Risks:** A stricter wrapper will expose missing environment prerequisites.
Report those prerequisites and exact completion commands instead of treating
skipped required checks as success.

**Decision:** Do now so subsequent work has one trustworthy completion gate.

### 5. Add behavioural HTTP and authorization coverage

**Why it matters:** The review measured 49.72% aggregate branch coverage, but
critical boundaries remain lightly exercised: device routes 24%, auth routes
28%, the HTTP handler 32%, and actor-scoped services 39%. Existing tests cover
the refactor's main structural invariants but did not catch the owner and
lifecycle defects above.

Prioritize cross-owner device/dashboard/binding/NAC/push mutations, user
deletion, password/session lifecycle, real handler authentication and
same-origin handling, cookie behaviour, and admin actions with explicit owner
context.

**Estimated effort:** 3–5 engineer-days for the initial suite, followed by
incremental coverage with changed behaviour.

**Risks:** Socket- or browser-heavy tests can become slow and flaky. Keep most
coverage at the service and handler boundary with temporary stores and mocked
device connections, then raise the coverage ratchet only after measured gains.

**Decision:** Start now alongside findings 1–3 and continue incrementally.

## Ongoing operational work

### Maintain the verification baseline

The CI verification workflow exercises Python 3.11–3.13, has a 47.9% coverage
ratchet, and runs the focused Playwright suite once on Python 3.13. The local
`scripts/verify.sh` parity gap is tracked as actionable finding 4 above.
Performance/store measurements remain deployment-specific evidence, rather
than portable repository constants.

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
