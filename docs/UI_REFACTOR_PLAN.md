# HomeLabHQ UI review and improvement plan

Reviewed 2026-09-05 against `364af6192a9dc8b8256d429ff777869189a35145`.
Status: **review baseline preserved; H01 account-lifetime protection implemented
and verified on 2026-09-05. All other findings remain proposals.**
This document supplements [the existing refactor plan](refactor-plan.md); it
does not authorize deployment, replace its gates, or change persistence policy.

## Executive summary

HomeLabHQ is a capable working operations application. Devices, discovered
workloads, client access, approved maintenance, and VPN management already have
useful domain-specific interactions. The visual vocabulary is recognizable,
the basic mobile shell reflows, and meaningful regression coverage exists.
A framework migration or wholesale redesign is not justified.

Correctness should precede visual changes. Browser testing reproduced retained
device cards from a previous account after an account switch and failed load;
out-of-order responses displaying one device's data under another device's
heading; redundant live-detail requests; and a Devices refresh timer that stops
after leaving and returning. Idle Compute does not fetch new inventory. These
faults undermine an operator's ability to trust the displayed identity and age
of information.

Other confirmed problems include keyboard-inaccessible controls, unnamed
controls, ineffective Access table sorting, silent refresh failure, inconsistent
dialog semantics, undersized touch controls, and insufficient contrast for
some light-theme text. Settings, diagnostics, and contextual maintenance are
discoverable with prior knowledge but lack a coherent local hierarchy. Large
rosters are rendered in full and searched synchronously.

Prioritize isolated correctness fixes and regression protection, then shared
primitives, navigation, operational summaries, and collection/form improvements.
Retain existing contracts and make each slice independently reversible.

## Evidence, environment, and limits

### What actually ran

- Repository instructions: root `AGENTS.md`; recursive in-repository discovery
  found no nested `AGENTS.md`. The user explicitly prohibited commits, overriding
  the repository's normal commit step. Initial Git status contained six
  untracked production-baseline files; they were neither read nor changed.
- Existing service: listeners at 8770/8771. Read-only comparison of its public
  frontend found all 29 JavaScript files, all three stylesheets, and `sw.js`
  identical to this checkout. `index.html` matched after normalizing only the
  documented self-signed-TLS Apple-icon URL rewrite in
  `backend/asgi/routers.py::_rewrite_index`. This establishes frontend parity,
  **not** backend image/provenance or production-data parity. No login or
  mutation was performed on that service.
- Interactive review used this checkout's documented `python -m backend.run`
  launcher, bound to loopback port 8878, with a fresh test-only directory:

  ```bash
  source .venv/bin/activate
  review_data_dir=$(mktemp -d /tmp/homelabhq-ui-review-XXXXXX)
  HLHQ_DATA_DIR="$review_data_dir" HLHQ_ICON_HTTP_PORT=0 \
    HLHQ_PORT=8878 HLHQ_HOST=127.0.0.1 python -m backend.run
  ```

  The review's directory was `/tmp/homelabhq-ui-review-HUQEg5`. Only fictional
  administrator/member accounts were created there. Populated integration
  responses and operational mutations were intercepted in Chromium; no device,
  controller, firewall, VPN provider, or push provider was operated.
- Browser: installed Playwright 1.61.1 and bundled headless Chromium. Desktop
  1440×900, tablet 768×1024, mobile 390×844; additional 320×740 shell checks.
  Touch emulation used `isMobile: true`, `hasTouch: true`. Dark, explicit light,
  and system-light rendering were inspected. Main fixture runs blocked service
  workers for deterministic interception; a separate run allowed a real service
  worker, cached the shell, disconnected the browser, and reloaded it.
- Browser actions included setup/password mismatch, login/logout/account
  switching, all seven top-level tabs, member keyboard navigation, deep links,
  history navigation, dashboard/device controls, chart ranges and keyboard
  inspection, nested dialogs, client filtering and bulk confirmations, Ansible
  configuration/test results, maintenance history, VPN candidates/settings/
  history/utilization/confirmation, wireless/radio/disk/interface views, NAC
  setup, fictional CSV download, and all four device-wizard steps.
- The existing browser suite additionally exercised approved Compute jobs,
  partial bulk failure, node/task isolation, reload recovery, mapping changes,
  appliance capabilities, VPN saves/switch results and legacy compatibility,
  notification reconciliation, and PWA caching. These are executed automated
  browser scenarios, not claims of live hardware validation.

### Evidence index

Screenshots and machine-readable observations are in [ui-review/](ui-review/).
JSON scenario entries record URL/hash, viewport and visible text. Filenames
beginning with a scenario number correspond to those entries. A full-page
screenshot does not expand a scrollable modal: some captures show its current
scroll position and the underlying document. Use the text observations and
workflow descriptions together with the image.

| Evidence | Contents |
|---|---|
| [baseline.json](ui-review/baseline.json) | 28 observations: setup, every top-level page at three widths, member navigation, user confirmation, public asset comparison |
| [populated.json](ui-review/populated.json) | 32 observations: operational fixtures, detail/modal paths, Access sort/refresh checks, Compute, Settings, wizard, synthetic roster timings |
| [diagnostics.json](ui-review/diagnostics.json) | Account-switch reproduction, controlled browser-clock polling counts, duplicate and reordered requests, touch font sizes |
| [specialized.json](ui-review/specialized.json) | 14 observations: AP/radio/interface/disk/chart views, firewall actions, NAC setup, alert validation, loading/error/done states |
| [shell.json](ui-review/shell.json) | Cached-offline boot, API documentation, 320px layout, route aliases, unknown and malformed hashes |
| [performance.json](ui-review/performance.json) | Single fresh-context login-shell timing and module transfer sample |
| [fixture-data.json](ui-review/fixture-data.json) | Fictional devices, clients, workload, controller, detail/chart and VPN payloads used in the review |
| [remaining.json](ui-review/remaining.json) | Dashboard create/rename/delete, diagnostic-log filtering/clear, NAC settings, password validation and partial morning-run results |

Fixtures are render-contract examples, not representations of supported vendor
firmware. For example, the specialized renderer exercise combines table/chart
capabilities on one fictional device to cover each renderer. Relative dates and
ages in the images belong to the capture, not to production. Rebase fixture
timestamps to the test clock before using them as regression tests.

### Verification and unresolved boundaries

`source .venv/bin/activate && ./scripts/verify.sh` completed successfully on
Python 3.14.7: Python compilation, Ruff, configured mypy, coverage-enforced
pytest, dependency audit, and Playwright all passed; **6 PASS, 0 FAIL, 0 SKIP**.
Pytest collected and passed **369 tests**; coverage was **67.94%** against the
existing 54.9% floor. Playwright passed **25 tests in 41.5s**. A final full run
after drafting the plan again passed all six stages, with **369 pytest tests
in 24.38s**, the same **67.94% coverage**, and **25 browser tests in 42.6s**.
The final run's local log is `/tmp/homelabhq-ui-final-verification.log`. This is a
repository verification result, not evidence that all UX requirements are met.

No screen reader, Safari/iOS, Firefox, installed mobile PWA, real touch hardware,
browser zoom, or assistive magnification session was available in this review.
No axe audit was run. Semantic/keyboard/size/color findings below are direct
DOM/browser observations; full WCAG conformance and platform behavior remain
unverified. Ordinary viewport reflow is not a substitute for a 200%/400% zoom
test. No production timing, write-rate, poll-duration, or storage values were
collected or published.

A single fresh-context login-shell sample recorded 160ms load, 240ms first
contentful paint, and 28 JavaScript module responses totaling 372,211 transferred
bytes on unthrottled loopback. This establishes what was loaded, not a proven
startup bottleneck or a production performance baseline. Repeat under controlled
network/CPU conditions before changing eager imports or caching. Collection
scaling measurements and their timing limitations are recorded separately in
M08; polling/request correctness findings do not depend on these speed samples.

`/redoc` rendered in Chromium. `/docs` served its HTML but was blank at the
two-second capture; external Swagger asset loading was not conclusively
diagnosed. Do not report that as an application rendering defect without a
network trace and a longer explicit initialization check. No API-doc mutation
controls were exercised.

Review-tool interruptions were resolved: an attempted parent-directory search
returned `find: '..': Permission denied`, followed by successful in-repository
instruction discovery; a saved admin session had been revoked by the deliberate
logout, so the populated harness was corrected to log in; a same-document hash
navigation needed a reload to apply a mocked session; an ad-hoc contrast
calculation had a syntax error and was rerun correctly. The local server later
exited with status 143, causing `net::ERR_CONNECTION_REFUSED` in the shell
harness; restarting the same isolated test instance allowed that review to
finish. These were review-harness/environment issues, not failed app checks.

## Current architecture and patterns

The frontend is a native ES-module SPA with no framework, bundler or general
state library. `web/index.html` (571 lines) owns the auth screen, seven panels,
and shared/static modals. `app.js` boots the session, binds navigation/auth,
registers the worker, and starts notifications. `router.js` maps hashes to
feature coordinators and listens for both `popstate` and `hashchange` plus
named UI navigation events.

`api.js` supplies DOM helpers, mutable session identity, formatting and a
same-origin JSON fetch wrapper with a default 30-second timeout. Features call
the established `/api/*` contracts directly. There is no centralized request
generation/session invalidation boundary. Notifications already use a request
sequence and clear their own state on logout: a useful local precedent.

Devices uses keyed card reconciliation and module-level dashboard/device
snapshots. Access is split into store, filters, API, grid, actions, edit dialog,
NAC setup and coordinator. Some ownership remains imperfect: `clients/grid.js`
fetches history itself, despite its renderer-only purpose. Device detail passes
a snapshot to metrics, interfaces, tables, firewall, alerts and VPN builders,
but its coordinator still imports exported Devices state.

Compute remains 1,868 lines in `web/js/compute.js`; VPN is 760 lines in
`detail/vpn-endpoints.js`. Compute mixes filters, mutable operation maps,
timers, API requests, eligibility, host rendering, Docker, mappings and job
history. VPN combines profile/candidate state, forms, lookups and switching.
These are the same decomposition opportunities already identified in the
existing refactor plan; do not create competing module structures.

`ui.js` already provides busy buttons, toasts, dialogs, a modal stack, icons,
tables, skeletons, keyed reconciliation and visible polling. `charts.js`
provides canvas histories, SVG donuts, text alternatives and keyboard inspection.
Extend these primitives after characterization rather than replacing them.

CSS is **already split**, in cascade order, into `base.css` (312 lines),
`components.css` (280) and `views.css` (633): 1,225 lines total. It has color,
surface, radius and font tokens, but spacing, typography, status treatments and
responsive overrides remain distributed. `views.css` contains global theme,
modal and control overrides as well as view rules. This merits carefully scoped
token/primitives work, not another file split for appearance.

The worker uses network-first static-shell caching, excludes `/api/*`, and
reconciles notification counts with the backend. Existing tests pin module
acyclicity, Access state ownership, CSS order and offline shell behavior.

## Route and state inventory

“Browser” means the rendered path was opened and inspected during this review;
“suite” means a passing existing Playwright scenario supplies additional
interaction coverage. Hardware/vendor permutations and external execution are
deliberately excluded. There are no standalone Services, Jobs, Alerts, Inventory,
or Infrastructure routes today; those concepts live within the following views.

| Route / surface | Existing purpose and entry | Review status and state coverage |
|---|---|---|
| `/` before authentication | First-run setup or login | Browser: setup, mismatch, success, login/logout, member switch, offline and boot-503; 390px touch/light |
| `/`, `#/devices` | Default inventory/dashboard | Browser: empty, populated, unknown/offline/old observations, retained data on 503, dashboard tabs; desktop/tablet/mobile |
| Dashboard selector and `…` | All, Unassigned, named groups; create/rename/delete | Browser: real test-store dashboard create/rename/delete, delete confirmation, named selection; source: keyboard reorder and drag/move persistence |
| `#/device/:id` | Deep-linked device modal | Browser: open, close, rename, customize, move selector, delayed/error/incorrect-order responses; back and keyboard; desktop/mobile |
| Device metrics/availability | Identity, values, 2h/24h/7d histories | Browser: numeric history, availability strip, range selection, keyboard tooltip, empty/error series and donut overlay |
| Device vendor detail | Interfaces, gateways, disks, wireless clients, radios | Browser: each renderer family, interface/radio expansion, disk series/pie, row-action confirmation, binding control; generic table source reviewed |
| Device firewall / binding / alerts | Opt-in rule controls, AP binding, thresholds | Browser: fictional rule toggle and picker, binding surface, threshold validation; no real integration effects |
| VPN inside OPNsense detail | Profiles, current endpoint, candidates, history and checks | Browser: status/utilization, settings, candidate disclosure, Use confirmation, empty history; suite: create/save, profile keyboard navigation, partial results/switch outcomes/compatibility |
| `#/compute` | Hosts → VMs/LXCs → Docker | Browser: empty/loading/populated/stale/stopped/attention, packages, responsive hierarchy; suite: error, bulk partial failure and operation states |
| `#/compute/:id` | Workload detail, mapping, approved maintenance | Browser: detail, mappings, Docker projects/containers, successful/failed/compacted history; suite: check/update jobs, mapping persistence, capability gating, reboot and task recovery |
| Compute Update All / node confirmations | Eligible OS updates and separate node actions | Suite: confirmation, bounded concurrency, failure/skip progress; source: exact target and no implicit bulk reboot |
| `#/access` and legacy `#/clients` | Network roster, optional NAC | Browser: unconfigured table/configured cards, empty/populated, filter/no-match, sort, history expansion, manual-503 and source failure; desktop/mobile |
| Access edit/approval/bulk/export | Names, notes, notifications, aliases, filtered actions | Browser: edit modal/focus restore, filtered forget confirmation, export download; suite: exact filtered mutation target |
| Access setup | Existing alias or new allow-list | Browser: both branches, alias/interface/seed choices, fictional completion; source: enforcement starts separately |
| `#/add` | Connect → Detect → Entities → Done | Browser: custom transport, validation, ranked results, entities, fictional completion, navigation draft loss; suite: 12 device preset credential mappings |
| `#/users` (admin) | Create/remove member or admin | Browser: real test-user creation, removal confirmation/cancel, unauthorized member route and keyboard entry; source: owned-resource deletion guard |
| `#/logs` (admin) | Recent diagnostic request/event log | Browser: real test-instance entries, auto-refresh toggle, search/no-match, errors filter, clear, desktop/tablet/mobile, member error; no production logs accessed |
| `#/settings` | Password, schedule/push, certificate, NAC, Ansible | Browser: unset/configured forms, inventory/approval controls, partial connection-test result, NAC alias/DNS form, password mismatch/success, 320/390/768/1440 widths; remaining save/error paths source reviewed |
| Notifications popover | Persistent unread entries and badge | Browser: empty/mobile and populated shell; suite: read and badge reconciliation; real OS delivery not tested |
| Theme picker | Auto/dark/light | Browser: picker and both themes; coarse-pointer form measurement; system preference transition while page stays open not tested |
| Unknown/malformed hashes | Fallback to Devices / decode device ID | Browser: unknown route silently shows Devices; malformed percent escape emits `URI malformed` twice |
| `?checkRun=:id#/devices` | Notification-linked morning result | Browser: partial run with updates, reboot requirement and unreachable/check-failed groups; source and backend tests |
| `/docs`, `/redoc`, `/openapi.json` | Generated developer API documentation | Browser: ReDoc rendered, Swagger initialization inconclusive; schema/static/API checks passed |
| `/health`, `/healthz`, `/readyz`, `/api/v1/*` | Operational JSON endpoints | Source/contracts and verifier; not product navigation pages |
| `/homelabhq.crt`, manifest, worker | Certificate download and PWA shell | Test-instance certificate GET 200; real offline shell exercised; installation/push/trust remain platform checks |

Remaining manual coverage above is explicit; no claim is made that every
firmware response, validation permutation, or external side effect was tested.

## Strengths to preserve

- Preserve Devices as managed infrastructure and Compute as discovered
  workloads with parent links. Do not create duplicate Devices for VMs/LXCs.
- Keep compact cards, named dashboards, search, status filters, and keyboard
  move-up/down alternatives to drag ordering. The shell and normal panels
  stayed within the inspected 390/768/1440 widths; narrow tables scrolled
  inside their own containers rather than widening the document.
- Preserve last-known values on transient failure, but scope them to identity
  and label freshness. Keep unknown/no-healthcheck distinct from healthy,
  lifecycle failure distinct from failed healthchecks, and benign completed
  Proxmox operations quiet once fresh data establishes success.
- Retain exact-node/task operation tracking, independent reboot confirmation,
  approved playbooks, explicit inventory mappings, bounded bulk concurrency,
  partial-success reporting and sanitized, progressively disclosed job output.
- Keep VPN's compact current endpoint, separate provider-utilization history,
  top-three candidate disclosure, retained compatibility checks, explicit
  apply/verify and rollback reporting. Do not equate provider load with local
  CPU or WireGuard throughput.
- Preserve text-backed status, accessible icon-button names where present,
  global focus rings, basic modal focus restoration, keyboard chart inspection,
  reduced-motion handling, and clear empty-state calls to action.
- Keep worker network-first shell delivery, no cached API credentials/data,
  owner-scoped notification badges, native modules and the existing CSS order.

## Findings

Priorities describe practical impact, not a security-advisory severity score.
Evidence refers to the current checkout. Acceptance criteria are proposed
requirements for later implementation, not claims of current behavior.

### Critical

No critical finding established. No production data loss, external action on
the wrong target, or backend authorization bypass was demonstrated. H01 and
H02 are high-priority correctness/privacy risks even though the reproductions
used fictional data.

### High

#### H01 — Clear feature state at the account boundary

**Implementation record — 2026-09-05:** the retained previous-account device
finding is fixed. Baseline evidence is preserved in documentation commit
`3a33fa0`; the observations and screenshots below describe the original defect.
This completes only this account-lifetime fix, not Phase 0 or any other finding.

- **Confirmed root cause:** `app.js` reset only `SESSION` and notifications.
  `ALL_DEVICES`, `DASHBOARDS`, `DEV_CARDS`, morning-run results, filters, detail
  snapshots and DOM survived. The intentional same-account 503 retention path
  reused them under B. `api.js` had neither request generations nor shared 401
  handling, and pending list/detail/router continuations could restore old data.
  Backend owner filtering was not bypassed. HTTP API responses already use
  `no-store`, and the worker excludes `/api/*`; neither was the source of this leak.
- **Protection:** each `setSession` establishes a new generation, including
  reauthentication as the same user. Synchronous disposal clears account
  snapshots, keyed elements, dialogs, drafts and private timers before the shell
  renders the resulting identity. Indirect device references in Access, Compute,
  Logs, Users, Settings and the wizard are disposed by their existing owners.
  Driver-name metadata, theme, saved Access sort and owner-keyed Access seen
  timestamps remain. No broad storage clearing or server-job cancellation occurs.
- **Requests and routing:** abort pending requests at the boundary; reject
  obsolete responses at headers and after body decoding, even when abort is
  ineffective. Device list/detail and route continuations also check their
  initiating generation. Current protected 401 headers clear the session without
  waiting for an error body; an old 401 cannot expire B. Logout clears immediately
  and serializes login behind its response. Unauthenticated protected requests
  are rejected locally. Page-hide disposal and persisted-page-show bootstrap
  protect restored documents. Ordinary same-account hashes and polling cadence
  are unchanged; the static shell cache version is advanced to v8.
- **Regression evidence:** `e2e/session.spec.mjs` adds 17 deterministic Chromium
  scenarios with real temporary-store accounts and fictional API data: logout;
  B loading/success/empty/503; delayed A list/body/detail results; replacement and
  same-user reauthentication; revoked sessions and immediate/stale 401s; history
  and page-cache lifecycle; sync/polling shutdown; other feature caches/drafts;
  theme/sort survival; and delayed logout serialization. Assertions inspect
  hidden DOM, form values, snapshots, page errors and transient cross-account
  mutations. Five tests covering the required regression categories were also
  run against the unchanged application in an isolated temporary checkout:
  all five failed as expected. The original same-user failed-refresh test remains.
- **Verification:** `source .venv/bin/activate && ./scripts/verify.sh` passed all
  six stages, with **369 pytest tests (26.73s), 67.94% coverage**, and **42
  Playwright tests (1.0m)**; **0 FAIL, 0 SKIP**. The focused session run passed
  18 tests including authentication setup (21.2s). Local final verifier log:
  `/tmp/homelabhq-h01-final-verify.log`. No production data was used.
- **Limits/follow-up:** same-session device-to-device response ordering and
  duplicate reads (H02), normal-tab polling resumption (H03), and broader error/
  freshness presentation (H05) remain open. External session revocation is known
  on the next 401 or session bootstrap; this change adds no cross-tab or server
  push authentication protocol. Native page-cache eviction/restoration differs
  by browser: persisted lifecycle events were exercised deterministically, while
  actual back/forward navigation was tested in Chromium. Other browsers and
  production deployment are unverified. No wider UI or architecture refactor,
  push, PR or deployment was performed.

- **Affected:** logout/login, Devices; audit Access, Compute, settings, dialogs
  and in-flight requests for the same lifetime problem.
- **Observed:** sign in as A, display A's fictional devices, sign out, sign in
  as B, and return 503 from B's initial `/api/devices`. B's identity appears
  above A's retained cards.
- **Impact:** a shared-browser user sees another user's prior operational
  information. This is client memory/DOM retention; no server auth bypass or
  credentials disclosure was shown.
- **Evidence:** [42 screenshot](ui-review/42-cross-account-retained-data.png),
  `diagnostics.json: crossAccount`; `app.js` logout resets SESSION and
  notifications but not `devices.js::ALL_DEVICES`; `loadDevices()` retains it.
- **Change:** one session-change boundary disposes feature timers, clears
  snapshots/DOM/dialogs and owner-specific filters, increments a session
  generation, and rejects results from the prior generation. Preserve same-user
  transient-failure retention.
- **Dependencies/risks:** characterize every feature cache first; do not clear
  server jobs or erase the user's theme preference. Avoid server/persistence
  changes. Notification cleanup is a precedent.
- **Acceptance:** browser tests for A→B with successful, 503 and delayed B/A
  responses show no A data or actions after logout; same-user 503 still retains
  labeled data; no pre-login/private background API calls remain.

#### H02 — Make route transitions and detail responses single-owner operations

**Implementation record — 2026-09-05:** H02 remained reproducible on `3cfa8fe`
after H01 and is now fixed. The original finding/evidence below is retained;
this completes H02 only, not H03 or the wider refactor.

- **Confirmed reproduction:** within one authenticated account/session, hold
  fictional A's detail body, open B, complete B, then complete A. Device detail
  displayed A's identity beneath B's title/address; Compute replaced B's title
  and workload with A. Repeating a read of the same resource also accepted the
  older result. Both initial deterministic reproductions failed on unchanged
  HEAD before implementation. No production systems or real device data were
  accessed, and the action-target assertion uses an intercepted fictional POST.
- **Affected presentations:** both routed modals (`#/device/:id` and
  `#/compute/:id`), their inventory lookup continuations, device live-detail
  reads, Compute's management-settings/history reads, device chart ranges,
  series overlays, VPN profile discovery/settings, and driver/firewall pickers.
  Held Compute history could append an extra empty section to B. Returning to
  24h accepted an older 24h result; a closed series overlay still acquired new
  detached DOM. VPN discovery could reselect A after selecting B, and its shared
  loading flag blocked B's refresh. Delayed choices could open obsolete dialogs.
  These are detail-lifetime defects; inventory polling/resumption is H03 work.
- **Root cause:** H01 owned the authentication lifetime, not navigation,
  presentation identity or request order. Shared detail snapshots/body nodes
  accepted same-session continuations, including errors and cleanup. The router
  handled both history events, loaded inventories twice for detail routes, and
  device opening recursively triggered hash routing. Range/profile equality
  alone could not distinguish successive requests for the same selection.
- **Protection:** one `requestOwner()` mechanism supplies unique latest-request
  tokens, abort signals and `current()` checks, reusing H01's session generation.
  Route activation, modal/view instance and individual reads have separate
  owners. Checks capture the encoded resource route and actual DOM nodes;
  replaced nodes, closed views and superseded reads cannot commit success,
  failure, empty state, loading cleanup, chart data or a detail refresh from an
  old maintenance continuation. Child reads inherit the presentation lifetime
  and check their own range/profile/overlay identity. Parent departure closes
  nested presentations. Device actions retain the winning resource snapshot.
  The API also checks ownership before processing 401 headers and after decoding
  bodies; a stale view cannot expire the winning session. Current errors and
  current protected 401s remain visible/effective. Cancellation never cancels a
  server job, and response guards remain necessary even when abort is ignored.
- **Routing:** history event pairs activate once; detail open intents use
  pushState without recursive hash opens. Deep links/reloads perform one required
  inventory read and one initial detail read; a card click uses its existing
  inventory snapshot and performs one detail read. Encoded IDs, browser
  back/forward, tab selection, focus ownership and direct-entry close are covered.
- **Regression evidence:** `e2e/detail-ownership.spec.mjs` adds **57 deterministic
  Chromium tests**, sharing fictional fixtures and held fetch headers/bodies.
  Cases cover A/B success/error permutations, stale empty results, same-resource
  refreshes, abort/loading cleanup, close/reopen, DOM replacement, route lookup
  races, back/forward, nested reads/pickers, live-read disposal, action targets,
  and overlapping session/view invalidation (including obsolete 401s).
  **12 representative regressions failed on an isolated `3cfa8fe` checkout**:
  11 in the representative run, plus the direct VPN profile-reselection case.
  Two additional obsolete-401 cases exposed the API header-side effect during
  development and pass with the final protection. The temporary checkout was
  removed; concise failure records remain under `/tmp/homelabhq-h02-*-repro.log`
  and `/tmp/homelabhq-h02-baseline-regressions.log`.
- **Verification:** the affected browser run passed **91 tests (1.9m)**. After
  the final API guard, focused H02 + H01 coverage passed **75 tests (1.3m)**,
  including authentication setup. Then
  `source .venv/bin/activate && ./scripts/verify.sh` ran once and passed all six
  stages: **369 pytest tests (25.64s), 67.94% coverage**, and **99 Playwright
  tests (2.1m)**; **0 FAIL, 0 SKIP**. No narrow Python run was needed: backend
  code/contracts were unchanged. Concise final verification record:
  `/tmp/homelabhq-h02-final-verify.log`. Successful full logs were not retained.
- **Limits/follow-up:** Chromium with an isolated temporary store is verified;
  other browsers, real hardware and production deployment remain unverified.
  H03 polling cadence/resumption and unrelated error/navigation improvements
  remain deferred. The six unrelated measurement files remain untracked and
  SHA-256-identical to the initial inspection. No deployment configuration,
  wider UI refactor, push, deployment or PR was performed.

- **Affected:** `#/device/:id`, router/detail coordinator; characterize Compute
  detail before applying the same request-identity guard.
- **Observed:** one Details click produced three `/detail` GETs. Holding A's
  response, navigating to B, then releasing A displayed `review-a response`
  beneath B's title and address.
- **Impact:** wrong resource attribution; because actions use the mutable detail
  snapshot, wrong-target actions are a plausible consequence, not an executed
  result. Repeated detail reads also repeat live management requests.
- **Evidence:** [44 screenshot](ui-review/44-out-of-order-device-response.png),
  `diagnostics.json: detailRequests/outOfOrderDetail`, `populated.json:
  detailRequestsOnClick`; `router.js` handles both history events and calls
  `loadDevices` twice on detail routes; `detail/index.js::openDevice` also writes
  the hash and accepts responses without checking current ID/generation.
- **Change:** route parsing and activation run once per navigation intent;
  opening UI cannot recursively re-open the same resource through hash events.
  Abort obsolete reads where possible and discard any obsolete result before
  assigning state, rendering, starting timers or enabling actions.
- **Dependencies/risks:** H01 session generation; preserve encoded IDs,
  old links, browser/PWA back, and direct-entry close behavior. Canceling a
  browser read must never imply canceling an already-started server job.
- **Acceptance:** one initial detail request per click/deep link, one Devices
  list request per required refresh; A→B, close-before-response, reload and
  rapid-back tests never render A under B or restart a closed timer. Assert
  mutation target against the visible resource in intercepted tests.

#### H03 — Restore reliable passive refresh and expose observation age

- **Affected:** Devices, Compute, Access; `ui.js::visiblePoll`, feature lifecycle.
- **Observed:** with a controlled browser clock, Devices had two reads after
  the first 16s. Leaving for 16s, returning, then waiting 31s produced only the
  return read (count stayed at three). Idle Compute had two reads before and
  after another 301s. Access loads on entry/manual refresh but has no passive
  roster timer. Devices still showed a green card for a two-hour-old observation.
- **Impact:** the backend can keep polling while the open UI stops reflecting
  changes; “live” status becomes misleading and operators may trigger expensive
  Refresh All solely to update the screen.
- **Evidence:** `diagnostics.json: devicePolling/computePolling`,
  [10 screenshot](ui-review/10-devices-populated-desktop.png);
  `devices.js::ensureDevPoll` retains a stop handle after `visiblePoll` stops;
  Compute `loadCompute` has job timers but no passive inventory interval;
  `clients/index.js::loadClients` is entry/action driven.
- **Change:** explicit feature enter/leave/dispose lifecycle, non-overlapping
  cached reads while visible, refresh on return, and a persistent last-success/
  stale indicator. Separate cached UI refresh from remote discovery actions.
  Use authoritative source timestamps; response time is not observation time.
- **Dependencies/risks:** H01/H02; characterize endpoint effects. Do not poll
  `/state`, `/updates` or remote-refresh POSTs as a convenience. Do not reuse
  the public global status summary as an owner-scoped source without a scope
  decision. New cadence must be measured against existing backend capacity.
- **Acceptance:** virtual-clock entry→leave→return and hidden-tab tests prove
  resumption and no overlap; zero remote mutations from passive refresh; cached
  failures retain values with persistent age/error; unknown stays unknown.
  Focus, filters, expanded rows and job progress survive refresh.

**Navigation-polling repair verified on 2026-09-05.** H03 remained
reproducible at `22b86d8` after H02; this completes the timer/navigation repair
only. The passive Compute/Access inventory expansion and observation-age/error
presentation proposals above remain deferred.

- **Exact reproduction:** isolated documented launcher on loopback 8878,
  fictional fixtures, Chromium 149.0.7827.55, 1440×900, service workers blocked,
  browser clock starting at 2026-09-05 12:00:00 UTC. On `#/devices`, cached
  `GET /api/devices` and `GET /api/dashboards` run every **15,000ms** (plus
  `GET /api/morning-updates/runs/:id` only when a `checkRun` query is present).
  Device reads occurred at t=0/15/30s. Navigate internally to `#/compute`, wait
  30s, return to Devices at t=60s, then advance to 75/90s: reads stayed at four;
  “Fictional sample 4” remained displayed and its observation age increased.
  Back to Compute and Forward to Devices repeated the failure (count five
  through t=150s). Open `#/device/router-1`, wait two **20,000ms** detail
  intervals (`GET /api/devices/router-1/detail`), close, and wait two parent
  intervals: Devices still had five reads. Reload at t=220s restored one read
  immediately and further reads at t=235/250s. The same parent failure applies
  to Devices' root/default route. No remote discovery or mutation was polled.
- **Root cause:** feature modules are reused, not reconstructed on navigation.
  `visiblePoll` cleared its interval on the first inactive tick but retained
  its visibility listener; `ensureDevPoll` retained the non-null stop handle
  and would not recreate it. The timer was absent, not blocked by H02 response
  ownership. A later hidden→visible event could also revive the old timer;
  reload was the reliable recovery in the ordinary visible-tab sequence.
  H02's removal of duplicate route activation did not fix this. Devices also
  failed to return its load promise to the helper, which itself had no running
  guard. A held read overlapped the next interval. Logs recreated polls in
  read completions, allowing late completions to recreate inactive polling.
- **Corrected ownership:** the router disposes Devices/Logs on departure and
  explicitly creates a fresh lifecycle on activation. Reads never create
  parent timers. Device detail replaces parent polling while open; Close,
  Escape, browser history and direct-detail fallback activate the parent once.
  H02's history deduplication remains intact. The shared helper awaits each
  callback, skips overlapping ticks without catch-up, pauses while hidden,
  invalidates pending reads on pause/disposal, removes its visibility listener
  on disposal, and cannot restart a disposed instance. Devices, Logs and the
  Access badge use H02 `requestOwner` tokens; detail keeps its existing owners.
  Both response commits and obsolete error paths check ownership. H01 remains
  the sole session generation and synchronously disposes polls/aborts requests
  on logout, expiry, reauthentication and page-hide.
- **Cadence/scope retained:** Device detail stays at 20s; `#/logs` reads
  `GET /api/logs` 3s after completion when auto-refresh is enabled (including
  its initial/manual-read timing). The app-scoped Access badge retains its
  60s `GET /api/clients/events?since=...` cadence and marks activity seen on
  Access entry. It intentionally survives internal navigation. Idle Compute
  and the Access roster still have no passive inventory timers. Existing
  Compute operation/job timers (1.5s; 3s status-error retry), notification
  polling (60s) and the relative-age text ticker (30s) were not changed.
- **Regression evidence:** 22 new deterministic tests in
  `e2e/polling.spec.mjs`, with shared fictional instrumentation in
  `e2e/support/polling.mjs`, cover exact clock events, repeated/rapid internal
  navigation, Back/Forward, detail Close/Escape/history/direct entry, missing
  detail fallback, all four visible-poll consumers, slow initial/manual/live
  reads, hidden→visible, failures, late decoded bodies despite abort, real
  logout/login, expiry, account reauthentication and page-cache restoration.
  Page-hide/teardown asserts zero visible-poll timers/listeners, unsettled
  fixture bodies or API timeout handles; browser contexts then close normally.
  The final three central cases were copied into a detached `22b86d8`
  worktree: both navigation cases failed with four reads versus five expected;
  the slow case failed with three versus two. Setup passed. The worktree was
  removed without touching this checkout's unrelated files.
- **Browser acceptance:** a separate Playwright-driven headless walkthrough
  outside the test runner repeated the original sequence. After repair,
  Devices counts were 4/5/6 at t=60/75/90s, 7/8/9 after Forward at
  t=120/135/150s, and 10/11/12 after detail close at t=190/205/220s.
  Inactive parents had no timer/reads; active parents had exactly one 15s
  timer. Card sample names and observation timestamps advanced again, without
  reload or accelerated cadence. A separate real local administrator logout
  and fictional member login cleared prior cards/detail and resumed one timer.
  [Minimal before/after clock and DOM evidence](ui-review/h03-polling.json)
  records the requests and timer/listener counts; no new screenshots were
  needed. This was browser-driver acceptance, not human-driven headed testing.
- **Verification:** focused H01/H02/Access/polling run: 99 passed; final H03
  run: 22 tests plus setup passed. The targeted frontend import-graph contract
  passed. After focused work and browser acceptance were stable, one complete
  `source .venv/bin/activate && ./scripts/verify.sh` passed: **6 PASS, 0 FAIL,
  0 SKIP; 369 Python tests, 67.94% coverage; 121 browser tests**. Local log:
  `/tmp/hlhq-h03-verification.log`. Earlier development failures included the
  expected baseline regressions and corrected fixture selectors (the Access
  badge changes the tab's accessible name). An acceptance attempt to sign out
  behind an open modal was corrected to close it first. The isolated review
  server logged complete shutdown; no deployment was used.
- **Limits:** no human-driven headed/non-Chromium acceptance or production
  measurements. Existing refresh-error presentation, stale-source semantics,
  Compute/Access inventory expansion, maintenance-job lifetimes and all
  unrelated findings remain unchanged. The six private measurement files
  retain their original checksums and are excluded from this change.

#### H04 — Make existing operational controls keyboard reachable

**Completed in tranche — 2026-09-05** (`fix: make operational controls keyboard reachable`).
At `ee6b166`, new keyboard tests could not locate a native SSH radio on `#/add`
or a History button in the radio table. The root cause was click-only generic
containers. Transports and candidates now use native radio groups; completed
steps, Access expansion, radio/interface history and chartable table cells use
native buttons. Table semantics and pointer entry remain. Disclosures expose
controlled regions and expanded state. AP binding is a separate named 44px
button with text, pressed/busy/disabled state, outside the wireless disclosure.
Shared target/focus rules retain compact icons; interface removal is named.
Two representative tests failed against the pre-fix code
(`/tmp/hlhq-h04-before.log`). Focused tests passed: 7 including existing device
refresh/preset consumers; final keyboard and overlay run 8 including setup.
Chromium at 1440×900, 768×1024 and 390×844 completed all four fictional wizard
steps with keyboard activation, navigated completed steps and Access history,
and checked focus/overflow. Shared network-control acceptance verified radio
and interface expansion, independent binding, target size and table headings.
A compact mobile wizard screenshot was visually inspected locally. These are
browser-driver checks, not physical-device or specialist-reader certification;
M04's wider physical-device/zoom gate remains open.


- **Affected:** Add device custom transports/candidates/completed steps; Access
  card expansion; device radio/interface history and AP binding.
- **Observed:** transport/candidate `div`s, completed-step `li`s, Access cards,
  radio/interface `tr`s and the AP-lock `span` have click handlers but no
  keyboard activation/tab stop. Compute cards already implement keyboard
  activation, so behavior differs by feature.
- **Impact:** keyboard users cannot complete custom-device setup or access
  certain existing details and controls. These are confirmed missing input
  paths, not a screen-reader compatibility conjecture.
- **Evidence:** `populated.json: wizardKeyboard/clientCardKeyboard`,
  `specialized.json: clickableRows/apLock`; `wizard.js::initWizard`,
  `clients/grid.js::buildCard`, `detail/tables.js`, `detail/interfaces.js`.
- **Change:** use radio inputs for exclusive connection/driver choices,
  buttons for step/disclosure controls, and distinct buttons for AP binding.
  Keep a table's semantics and put its expander inside a cell. Provide
  `aria-expanded`, controlled-region IDs and descriptive resource names.
- **Dependencies/risks:** shared focus/disclosure primitives; no nested button
  inside the existing wireless-client header button. Retain pointer behavior
  and do not change NAC action semantics.
- **Acceptance:** complete custom wizard and open/close each detail using
  Tab/Shift+Tab/Enter/Space alone; radios support arrow selection; expanders
  announce state; AP binding is a separate named keyboard action. Apply the
  [W3C keyboard guidance](https://www.w3.org/WAI/WCAG22/Understanding/keyboard.html).

#### H05 — Give failed refresh, offline boot and session expiry visible recovery

- **Affected:** Access Refresh, app boot, shared API feedback; audit notifications
  and Settings requests with their source-proven silent catches.
- **Observed:** injected 503 from `/api/clients/refresh` restored the button and
  retained cards but emitted an unhandled page error with no visible failure.
  Boot 503 and actual cached-offline reload both displayed ordinary Sign in.
  Other retained-data failures use a toast that disappears after seven seconds.
- **Impact:** users cannot tell whether a scan succeeded, whether data is old,
  or whether credentials are needed. Repeated sign-in or refresh attempts do
  not address an unavailable service.
- **Evidence:** [26](ui-review/26-access-refresh-error.png),
  [60](ui-review/60-boot-service-error.png),
  [61](ui-review/61-offline-cached-shell.png); `clients/index.js` Refresh handler
  lacks catch; `app.js::boot` converts every exception to `showAuth(false)`;
  `api.js` throws status/data but has no session-expiry coordination.
- **Change:** shared first-load, refreshing, retained/error, offline, expired-
  session and retry states. Show a durable inline error for failed operations
  and last-success age, with one retry action; toasts supplement it. Use the
  backend request ID for expandable diagnostics when supplied.
- **Dependencies/risks:** H01 ensures offline/error retention cannot cross
  users. Only confirmed unauthenticated/401 responses should initiate login;
  use browser connectivity as a hint, not proof of server availability.
- **Acceptance:** injected 503/timeout/offline/401 paths produce distinct,
  accessible feedback, no unhandled rejection, restored controls and a working
  retry. Boot failure does not masquerade as a credentials form. No API data
  is added to the worker cache.

#### H06 — Label forms and switches consistently

**Completed in tranche — 2026-09-05** (`fix: label forms and associate validation`).
At `b4bb0d3`, `#/settings` failed the new Current password label assertion:
placeholders were the only captions. Users/alert selects and enforcement had the
same missing-name/association root cause. Added persistent native labels, explicit
caption associations, shared field/help/error helpers, native-invalid feedback and
first-actionable-error focus. Alert thresholds are grouped by device with sensor
units; enforcement names the firewall/alias; searches and prompts retain captions.
Busy controls preserve their prior disabled state and announce loading. Session
disposal clears validation text/invalid state. Password/autocomplete and payload
contracts remain unchanged. New regression failed before implementation
(`/tmp/hlhq-h06-before.log`); final focused run passed 3 tests plus setup.
Chromium browser acceptance at 1440×900, 768×1024 and 390×844 covers typed labels,
password mismatch, empty user submission, alert threshold focus/units, named
switch state, search captions and document overflow. Tests are in
`e2e/foundation.spec.mjs`; no specialist screen-reader certification is claimed.


- **Affected:** password and user forms, device alert editor, NAC enforcement;
  search fields rely on disappearing placeholders.
- **Observed:** the enforcement switch has `role=switch`/`aria-checked` but no
  name. Both alert selects have no labels or ARIA names. Settings password and
  Users inputs rely on placeholders; the role select is unnamed. The shared
  prompt's wrapping label has no caption. Search has no persistent visible label.
- **Impact:** users cannot reliably identify controls once values are entered;
  unnamed switches/selects have ambiguous assistive-technology meaning.
- **Evidence:** `index.html` forms, `detail/alerts.js::alertsSection`,
  `clients/grid.js::nacBanner`; `baseline.json: unnamedInputs`,
  `diagnostics.json: passwordLabel`, `specialized.json: alertNames` and
  `populated.json: enforcementName/nestedDialog`. Placeholder fallback naming
  varies; do not describe every placeholder-only input as completely nameless.
- **Change:** persistent labels and help/error associations; group threshold
  sensor, comparison and value; include unit and scope. Name enforcement with
  the protected network/firewall. Mark invalid fields and focus the first error.
- **Dependencies/risks:** shared field primitive; keep autocomplete, password
  minimum, blank-secret-keeps-existing behavior and backend validation.
- **Acceptance:** all controls in these flows have meaningful accessible names,
  visible field labels and associated error text; labels remain after typing;
  keyboard submission of invalid forms identifies the first actionable error.

### Medium

#### M01 — Decouple roster sorting and inspection from NAC configuration

- **Affected:** `#/access`; `clients/grid.js::clientsTable/clientCards`.
- **Observed:** table order remained Zulu laptop, Alpha camera, Guest tablet
  after selecting IP sort, although their IPs were .20, .10 and .25. Only cards
  call `sortClients`. Disabling NAC switches to a plain table with no per-client
  inspection/edit entry; the configured view has expandable history and actions.
- **Impact:** a visible control does nothing, and an unrelated enforcement
  configuration determines whether client history can be inspected.
- **Evidence:** [27](ui-review/27-access-table-mobile.png), `populated.json:
  tableSortBefore/tableSortAfter`; the two renderer branches above.
- **Change:** apply one sort/filter model before either presentation. Provide
  read-only client details/history independent of NAC; gate approval/firewall
  actions by capability. Keep export's explicit whole-roster scope distinct
  from filtered bulk targets. Add user-controlled table/card view only after
  both share the same capabilities.
- **Dependencies/risks:** H04/H06 and M08; confirm personal-edit API behavior
  without NAC before exposing writes. Numeric IP comparison must specify IPv6
  and missing-address behavior rather than copying the current IPv4-only key.
- **Acceptance:** every advertised sort works in both views, with deterministic
  ties; a non-NAC user can open the same available history; filtered bulk
  operations target exactly the stated set; no extra remote scan on expansion.

#### M02 — Unify modal semantics, scroll locks and close navigation

**Completed in tranche — 2026-09-05** (`fix: unify modal ownership and semantics`).
At `3136d70`, desktop `#/device/router-1` → nested rename failed the new
accessible-dialog assertion; the shared form had no dialog role/name. Independent
writers cleared body overflow, the capture-phase Escape handler preempted chart
inspection, and Compute Close/Escape used different history paths.
The modal stack now owns scroll locking and preserves/restores background inert
state; only its top traps focus. Prompts/pickers and dynamic overlays are named,
focus entry is synchronous, chart inspection consumes its first Escape, and
Compute Escape uses the same dismissal as Close/backdrop. Existing H02 direct-entry
handling remains. Regression: 11 focused stack/history/detail tests passed;
final three-width stack + chart walkthrough passed (3 tests plus setup).
Chromium at 1440×900, 768×1024 and 390×844 checked nested naming, Tab containment,
scroll/inert restoration, two-stage chart Escape, parent usability, URL and overflow.
The baseline failure is recorded in `/tmp/hlhq-m02-before.log`; test evidence is
`e2e/foundation.spec.mjs`. No specialist reader or non-Chromium validation claimed.


- **Affected:** shared prompts/confirmations/pickers, stacked chart/VPN overlays,
  device and Compute details.
- **Observed:** shared `#dialog-form` has no dialog role/name/aria-modal. Closing
  a nested rename clears body overflow while the parent device modal remains
  open. Escape from a keyboard-focused chart closes device detail instead of
  just clearing chart inspection. Compute Escape hides its modal while leaving
  the detail hash; Close uses history.back. The tested Shift+Tab trap stayed
  inside the rename dialog; a universal focus-trap failure was **not** confirmed.
- **Impact:** ambiguous screen-reader structure, background scrolling and
  inconsistent back/reload behavior. Unannounced obscured background remains a
  specialist-testing concern because the stack does not make it inert.
- **Evidence:** `populated.json: nestedDialog/afterNestedClose`,
  `specialized.json: chartEscapeClosesDevice`; `ui.js::pushModal/_dialogClose`,
  `compute.js::closeCompute`, `detail/index.js::closeDevice`.
- **Change:** one named dialog contract, stack-owned scroll locking and inert
  background, explicit topmost Escape handling, and one route-aware dismissal
  path for Escape/Close/backdrop/back. Let chart inspection consume Escape first.
- **Dependencies/risks:** H02 routing; preserve focus restore and asynchronous
  confirmation semantics; distinguish direct entry from an in-app pushed route.
- **Acceptance:** stacked dialog keyboard tests, scroll-lock assertions at
  each depth, accessible dialog names, and identical post-close URLs for
  equivalent entry paths; no accidental navigation outside the app on direct
  entry; parent remains usable after dismissing a child.

#### M03 — Separate light-theme text contrast from accent/status fills

**Completed in tranche — 2026-09-05** (`fix: separate semantic text colours from fills`).
At `ab45876`, the deterministic light-theme chart range test measured 3.9437:1
against the actual page background and failed the 4.5:1 text criterion. Fill/line
colours were reused for small text. Added semantic accent/success/warning/error
text roles, separate action/success fills for contrasting labels, a focus role
and interactive boundary colour. Migrated the text consumers across the existing
three ordered sheets; chart lines and decorative status fills retain their roles.
No new CSS split. Contrast regression and browser acceptance passed (1 test plus
setup): 105 computed pairs across explicit light/dark and system-light, three
surfaces, chart ranges, client severity, tinted Compute badges, buttons, active
dashboards, muted text and separate 3:1 control-boundary/focus checks. Responsive
checks use 1440×900, 768×900 and 390×900. Both theme captures were visually inspected;
[compact exact ratios](ui-review/m03-contrast.json) preserve the evidence. Existing
text/icons remain, and tests do not claim full WCAG or reader certification.


- **Affected:** small accent links, active chart ranges, severity-colored text.
- **Observed:** system-light tokens were accent `#1f7ae0`, green `#0f8a5f`, amber
  `#b9791a`, background `#f4f6f9`. Relative-luminance ratios on that background
  are respectively **3.94:1, 4.02:1, 3.34:1**. Active chart-range text uses the
  accent on that background. Muted text measured 5.32:1; do not darken all muted
  text indiscriminately. White backgrounds and tinted badges need separate
  pair measurements.
- **Impact:** small operational labels can be hard to distinguish in light mode.
- **Evidence:** [47](ui-review/47-specialized-detail-light-mobile.png),
  `specialized.json: lightColors`, `styles/views.css` light tokens/range rules.
- **Change:** introduce text-specific accent/success/warning tokens, retaining
  separate line/fill/border colors. Verify every actual foreground/background
  pair and disabled-control exception. Preserve text/icon status redundancy.
- **Dependencies/risks:** token changes affect charts and controls in all views;
  do not infer a chart-line contrast failure from a text threshold.
- **Acceptance:** normal-size text pairs meet 4.5:1 and appropriate large text
  meets 3:1 in both themes; control boundaries/focus get their own checks, per
  [W3C contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
  Capture both themes before/after and manually inspect semantics, not just hue.

#### M04 — Correct coarse-pointer sizing and small action targets

- **Affected:** login/settings/wizard fields, chart range controls, AP lock.
- **Observed:** coarse-pointer Chromium still computed auth input text at 12px
  and Settings password text at 14px: the broad 16px media rule loses to more
  specific field selectors. Chart range target was about 27×16px; the AP lock
  was 13×13px inside another clickable control.
- **Impact:** difficult touch activation; iOS input zoom is a plausible risk
  from the computed sizes, not verified on iOS. A small target alone does not
  establish a WCAG failure because spacing/equivalent-control exceptions exist.
- **Evidence:** `diagnostics.json: touchFont/passwordLabel`,
  `specialized.json: apLock/rangeTarget`, `base.css` field and coarse rules,
  `views.css::.client-lock`.
- **Change:** effective 16px touch-field rule, larger independent lock button,
  larger hit regions for range/actions without proportionally enlarging icons.
  Allow modal titles to expose full identity when truncated.
- **Dependencies/risks:** H04/M02; verify narrow headers and long names after
  increasing hit regions. Do not disable viewport zoom.
- **Acceptance:** computed mobile field size is at least 16px; measured targets
  meet 24×24px or documented spacing/equivalence, with 44px preferred for
  frequent touch actions. Evaluate the nested lock against
  [W3C target-size guidance](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).
  Test physical iOS/Android, 320px reflow and zoom before closing the item.

#### M05 — Make attention, freshness and existing feature locations explicit

- **Affected:** Devices overview, Compute summary, Access source warnings,
  contextual alerts/VPN/jobs/Ansible inventory.
- **Observed:** Devices counts online/offline but omits the unpolled device
  from those subtotals and leaves old online data green; Access says one source
  is unreachable without identifying it there. Compute has useful attention
  filtering, but no name/IP search. Alerts are at the bottom of device detail,
  VPN inside OPNsense detail, jobs inside workload detail, inventory in Settings.
- **Impact:** operators must know feature placement and manually inspect cards
  to find aging observations or relevant work; the default screen is chiefly
  inventory rather than a clear queue of things needing attention.
- **Evidence:** [10](ui-review/10-devices-populated-desktop.png),
  [28](ui-review/28-compute-desktop.png), [21](ui-review/21-access-cards-mobile.png),
  source route inventory and `backend/asgi/status_service.py` source-age logic.
- **Change:** extend existing summary/filter bars with explicit unknown/stale
  categories, drill-downs and named source errors; add Compute name/IP/node
  search. Add section links to existing detail/configuration locations. Start
  with per-feature summaries; a cross-domain Overview is optional, gated below.
- **Dependencies/risks:** H03 freshness first. The public
  `/api/v1/status/summary` is a global operational projection, **not** an
  owner-scoped list endpoint. Do not silently merge it into member dashboards.
  Use current visible-resource payloads; missing authoritative age metadata
  needs a small additive owner-aware contract, separately reviewed.
- **Acceptance:** each displayed count resolves to an explainable set; unknown
  and stale are visible without opening every card; named source errors lead
  to the source; search finds workload/node/IP and preserves host context;
  no new discovery calls or changed access policy from viewing a summary.

#### M06 — Give Settings a local hierarchy and configuration prerequisites

- **Affected:** `#/settings`, `settings.js`, Ansible setup links from Compute.
- **Observed:** a narrow password card, full-width morning-check form, narrow
  certificate card and full-width Ansible form appear in one long grid. At
  1440px this leaves substantial empty columns; on mobile reaching controller
  configuration requires scrolling through unrelated account/notification
  content. “Open Settings” has no Ansible anchor. Inventory and approval controls
  appear only after their preceding discovery results are present.
- **Impact:** setup requires remembering sequence and repeatedly locating the
  right section; mixed personal and administrative tasks slow troubleshooting.
- **Evidence:** [03 Settings](ui-review/03-empty-settings-1440.png),
  [36 configured Settings](ui-review/36-settings-configured.png),
  `settings.js::loadNacConfig/loadAnsibleConfig/renderPlaybookOperations`.
- **Change:** local section navigation for Account, Notifications/schedule,
  Network Access, Ansible, and Certificate; deliberate column widths and
  aligned form actions. Show saved/configured/tested/discovered/approved stages
  with the relevant next action. Deep-link Ansible guidance to its section.
- **Dependencies/risks:** M07 route context, H06 fields. Do not conflate Test
  Connection with saving discovered executable paths; keep approvals explicit,
  secrets blank on load, and existing member/admin visibility.
- **Acceptance:** a Compute setup link lands on Ansible without a manual search;
  incomplete stages explain what is missing; test/inventory/approval failures
  retain entered values and display local recovery; one successful saved
  configuration completes the existing documented workflow unchanged.

#### M07 — Preserve navigation and draft context without persisting secrets

- **Affected:** route parser, dashboard/filter selection, Add wizard and detail
  close paths; member tab navigation.
- **Observed:** member ArrowRight from Add device lands on hidden Users and
  displays “admin only”; direct admin hashes behave similarly. Wizard host
  resets to empty after leaving/returning. Dashboard and Compute filters are
  not represented in URLs. Unknown hashes silently render Devices; a malformed
  device ID throws `URI malformed` twice.
- **Impact:** unexpected navigation, lost setup work, non-shareable filtered
  views and unrecoverable-looking malformed links.
- **Evidence:** [07](ui-review/07-operator-arrow-navigation.png),
  `populated.json: wizardDraftAfterReturn`, `shell.json` scenarios 64–66;
  `app.js` arrow handler uses hidden tabs; `router.js::tabFromHash`,
  `wizard.js::initWizard`, `devices.js::currentDashboard`, Compute FILTER.
- **Change:** route registry supplies allowed destinations and visible keyboard
  targets; safe decode/not-found/forbidden handling; optional encoded filter/
  dashboard state and settings-section anchors. Preserve non-secret wizard
  drafts in memory or warn before abandoning them; never put credentials in
  URL or localStorage. Normalize legacy `#/clients` without breaking it.
- **Dependencies/risks:** H01/H02 and M02; keep existing bare hashes valid,
  browser back intuitive, and query-based morning notification links working.
- **Acceptance:** member arrow/Home/End navigation skips hidden tabs; direct
  unauthorized routes give a safe explanation/return path; malformed links do
  not throw; reload/back restores defined filters; wizard context behavior is
  explicit and secrets are absent from persisted browser state.

#### M08 — Bound collection rendering and measure before adding virtualization

- **Affected:** Access table/cards, Compute lists/history, generic driver tables.
- **Observed:** Access creates all rows and all cells, then filters/rebuilds on
  each input event. One loopback desktop sample took 186ms/248ms/1,318ms to
  enter and render 100/1,000/5,000 rows. A synchronous search dispatch took
  3.3ms/11.6ms/49.1ms respectively. These include fixture/browser costs; the
  entry measure includes a deliberate 150ms settle wait. They are not isolated
  render CPU timings, production API latency, INP, or repeatable p95 benchmarks.
- **Impact:** confirmed unbounded DOM work and measurable local growth; larger
  rosters/slow hardware plausibly amplify it. No storage-engine conclusion
  follows from these synthetic browser measurements.
- **Evidence:** `populated.json: roster100/1000/5000`; `ui.js::buildTable`,
  `clients/filters.js`, `clients/grid.js`, `compute.js::render` replacing its list.
- **Change:** instrument query/render/request costs separately; common paged
  collection model with total/filtered counts, stable sort, preserved selection
  and focus. Start with bounded client-side DOM (e.g. 50-row default, selectable
  page size) while preserving existing full-response API. Consider server paging
  only after transfer/parse cost is independently measured and justified.
- **Dependencies/risks:** M01; pagination must not quietly change “all shown” to
  current-page or whole-roster mutations. Avoid unnecessary full rebuilds of
  Compute cards; preserve expanded job details. Virtualization complicates
  keyboard, find-in-page and screen readers and is not the initial solution.
- **Acceptance:** a 5,000-record fixture renders no more than selected page size
  plus fixed chrome; counts/search/export refer to the documented sets; focus
  and selection survive refresh. Collect five same-environment samples before
  and after, reporting transfer, parse, render and input costs separately. A
  numerical performance gate is chosen from those repeatable samples.

#### M09 — Extend existing feature boundaries and regression coverage

- **Affected:** Compute/VPN monoliths, shared UI/polling, stateful renderers,
  frontend test strategy.
- **Observed:** 1,868-line Compute file contains many independent responsibilities;
  760-line VPN file combines forms/state/actions; Access grid fetches history;
  initial login loads 28 JS modules even before using those features. Existing
  25 browser tests emphasize Compute (15 tests), with no dedicated Users,
  Settings, shared-dialog, full accessibility or visual-snapshot suite.
- **Impact:** defects cross route/session/timer boundaries that current feature
  tests do not pin; large modules and eager coupling make isolated changes harder.
- **Evidence:** source counts/imports, `e2e/*.mjs`, `tests/test_phase7.py`,
  [existing plan](refactor-plan.md#phase-3--split-the-compute-frontend-by-state-and-workflow).
- **Change:** adopt the existing Compute/VPN extraction plan and single-owner
  rule. Add behavior tests for H01–H06 before moves; make history fetching an
  injected intent; expose lifecycle methods. Evaluate lazy feature imports only
  after measuring a need and protecting offline delivery.
- **Dependencies/risks:** coordinate with existing refactor phases; preserve
  API façades and persisted payloads. No simultaneous second state owner,
  universal store, framework switch or line-count-based completion criterion.
- **Acceptance:** import graph stays acyclic; pure reducers have contract tests;
  timers and request generations have one owner; retained browser assertions
  pass; no new external dependencies needed for application runtime.

#### M10 — Render available interface rates on initial detail display

- **Affected:** `detail/interfaces.js::ifTable/updateRate`.
- **Observed:** initial interface rows had blank rate cells even with two rx/tx
  counter samples in `ifHistory`. `updateRate()` returns while the new cell is
  disconnected; the live-cell registry is refreshed later by the detail timer.
- **Impact:** available throughput looks absent until a later refresh, with no
  explanation of whether collection has started.
- **Evidence:** blank Rate column in [12](ui-review/12-device-detail-desktop.png),
  `fixture-data.json: detail.ifHistory`, `interfaces.js` connectedness guard and
  `charts.js::registerLiveCell/refreshCharts`. Later vendor refresh success was
  not needed or measured to establish the initial blank rendering.
- **Change:** perform the initial rate fill after insertion or permit safe
  detached initial rendering; show explicit “not enough samples” for absent
  history. Preserve counter reset handling and bits/second formatting.
- **Dependencies/risks:** H02 prevents filling detached obsolete details; keep
  the existing 20s live read policy unless separately measured.
- **Acceptance:** two valid samples yield the expected rate on first display;
  missing/one/reset samples have defined output; no extra API call is introduced.

### Low

#### L01 — Reduce repetitive card controls and clarify action names

- **Affected:** Devices card footers, topbar notification/theme glyphs, Access
  naming and Update All wording.
- **Observed:** each Device card exposes six icon actions, including ordering
  and removal; operational and configuration actions share equal prominence.
  The same roster calls entries “devices” while infrastructure also uses Devices.
  Compute “Update All” operates on eligible workloads, not host package actions.
- **Impact:** icon scanning and scope ambiguity grow with inventory size; the
  safety cost is mitigated by existing labels, confirmations and eligibility.
- **Evidence:** [10](ui-review/10-devices-populated-desktop.png),
  [28](ui-review/28-compute-desktop.png), card/action builders and bulk description.
- **Change:** retain Details/Sync prominently; group rename/reorder/remove in
  an accessible secondary menu or explicit arrangement mode. Keep keyboard
  ordering. Name the bulk operation “Update eligible workloads” with count;
  prefer “clients” for discovered network endpoints in help/empty-state copy.
- **Dependencies/risks:** H04 menu semantics, M05 hierarchy; do not hide all
  controls behind hover or change global bulk scope to match a filter silently.
- **Acceptance:** all current actions remain keyboard/touch reachable, destructive
  actions keep clear confirmations, and bulk confirmation states scope/count
  and no-reboot policy before submission. No icon-only meaning without a name.

#### L02 — Add date context and durable retrieval feedback to long charts

- **Affected:** chart 24h/7d ranges and detail series overlays.
- **Observed:** range text uses `fmtClock` (time only) even for long ranges;
  long-series failures are swallowed and can leave blank/previous data, while
  standalone series errors do render a message. Keyboard tooltip works, but
  detailed screen-reader point announcements were not tested.
- **Impact:** multi-day spikes and missing series are hard to interpret.
- **Evidence:** `charts.js::updateRange`, `detail/metrics.js::fetchLong`,
  [51](ui-review/51-series-error.png); existing charts already expose current,
  min and peak text alternatives, which must not be removed.
- **Change:** date/time/timezone-aware ranges beyond one day, visible loading/
  failed/empty series states and retry; retain a clearly labeled previous series
  only if it belongs to the selected range. Validate keyboard/screen-reader
  inspection with a specialist before adding a larger chart abstraction.
- **Dependencies/risks:** M02 Escape ownership and H05 errors; no new chart
  library or increased polling frequency.
- **Acceptance:** 2h/24h/7d, DST, one-point, no-point and 503 fixtures have
  unambiguous ranges and units; min/current/peak remain available as text;
  retry succeeds without closing the parent view.

## Proposed target information architecture

Keep existing root routes and operator concepts. First improve hierarchy within
the current shell; do not manufacture standalone products called Services,
Inventory, Jobs or Infrastructure without a demonstrated cross-resource use.

| Primary destination | Local hierarchy and cross-links | Compatibility |
|---|---|---|
| Devices | Attention/age summary → named dashboards → infrastructure detail; detail sections Overview, Metrics, Network, VPN (when supported), Alerts, Configuration | Keep `#/devices`, `#/device/:id`, dashboards/order and driver-dependent sections |
| Compute | Host/node → VM/LXC → Docker project → container; search/type/attention filters; contextual maintenance and jobs | Keep `#/compute`, `#/compute/:id`; parent Devices remain separate |
| Network Access | Client search/status → table/cards → client identity/history; NAC setup/enforcement as explicit optional management section | Keep `#/access` and alias `#/clients`; preserve non-NAC monitoring |
| Settings | Account; Notifications and scheduled checks; Network Access integration; Ansible controller/inventory/approvals; Certificate | Keep `#/settings`; add optional section address without invalidating old links |
| Administration (admin only) | Users and Logs; use a grouped menu or clearly separated nav region | Preserve `#/users`, `#/logs` and role checks; grouping is navigation only |
| Contextual Add device action | Available from Devices/empty states and accessible global action | Keep `#/add`; removing its top-level tab is optional after discoverability checks |
| Notifications | Unread badge/popover; link to the actual device/workload/check run where URL data exists | Preserve backend unread/read/dismiss state and current notification URLs |

Device/workload headers should expose full identity, parent relationship and
stable section links. Desktop may keep modal detail initially; mobile may use
the same routed content as a full-screen surface after close/back behavior is
unified. A page-like detail treatment is an incremental shell change, not a new
detail implementation.

Optional later Overview: combine attention counts and active work only from
authorized existing data, with explicit freshness and links to source views.
Do not make it the default landing page until the owner decides it improves
the morning-check/incident workflow. A cross-workload Jobs index also needs a
demonstrated monitoring use case and authorized API scope; contextual history
remains the first implementation.

## Component and design-system structure

Use native DOM builders plus small pure model functions. Existing names below
are retained until callers migrate; proposed paths are destinations, not a
request to scaffold empty files.

| Area | Proposed responsibility and primitive | Existing source to evolve |
|---|---|---|
| Tokens | Semantic text/fill/status colors, surface/border/focus, spacing scale, type sizes, control sizes and content widths; dark/light pair tests | Three existing CSS files; do not change cascade order incidentally |
| Layout | `PageHeader`, `Section`, `ActionBar`, form-width layout, responsive detail shell and local section links | `index.html`, `ui.js::detailSection`, panel/modal CSS |
| Navigation | One route registry/parser, safe IDs, allowed destinations, NavLink, filter/disclosure selection and return context | `router.js`, `app.js`, dashboard/Compute tabs |
| Status | `StatusBadge`, `ObservationAge`, `AttentionSummary`, `SourceFailure`; explicit data state separate from reachability, maintenance, lifecycle and healthcheck state | Devices, Compute reducers, Access summary, VPN pills |
| Forms | `Field`, `FieldHelp`, `FieldError`, `FieldGroup`, radio choice, secret field, busy submit; keep native controls | Wizard, Settings, user forms, alerts, VPN fields |
| Collections | `CollectionToolbar`, stable sort/filter model, `DataTable`, row disclosure, page-size/navigation/counts; explicit filtered/page selection scope | `ui.js::buildTable`, Access grid/filters, driver tables, Compute grid |
| Feedback | First-load/empty/no-match, refresh-with-retained-data, inline error/retry, operation progress/result; named dialog stack and secondary menu | `ui.js`, notification panel, Compute progress |
| Visualization | Existing chart/donut engine plus date-aware range selector, legend, loading/error/no-data wrapper and accessible summaries | `charts.js`, `detail/metrics.js`, VPN utilization |
| Lifecycle | Per-feature store, request/session generation, enter/leave/dispose, no-overlap cached refresh; view emits intents | Access coordinator precedent, Devices, Compute and VPN |

Adopt the already-proposed `web/js/compute/{store,model,api,proxmox,
bulk-actions,workload-grid,detail,docker,index}.js` and
`web/js/vpn-endpoints/{model,form,candidates,status,actions,index}.js`
boundaries when those slices are authorized. Extract reusable UI helpers to
`web/js/ui/` only when they have real callers and a clear responsibility.
Keep a thin compatibility export in `ui.js` temporarily where necessary, with
one implementation. Do not introduce a single mega status enum: “running”,
“healthy”, “stale”, “up to date” and “permitted” describe different dimensions.

## Phased implementation plan

The UI phase numbers below are independent of the backend plan's phase numbers.
Its Phase 2 gate still requires deployment of the compaction fix and repeated
private measurements before **backend** decomposition begins. UI correctness,
tests, navigation and primitives can proceed against current contracts. None of
these phases authorizes a backend rewrite or real infrastructure action.

Each work package is a separate reviewable change. Add characterization first,
then fix behavior, then extract code separately. Future commit/PR titles below
are suggestions only; this review creates no commit or PR. Run focused browser
tests during each slice and the full verifier before merging it.

### Phase 0 — Baseline, regression protection and isolated correctness fixes

- **Objective:** capture a trustworthy starting point and eliminate H01/H02/H03
  before changing shell or visual structure.
- **Routes/source:** all current hashes; `e2e/support`, new session/router/
  polling specs; `app.js`, `api.js`, `router.js`, Devices/detail/Compute lifecycle.
- **Prerequisites:** current checkout comparison; fictional isolated data and
  saved screenshots/fixtures here. Recheck only areas changed since this review.
- **Ordered packages:** (1) promote H01–H03 reproductions to deterministic
  tests; (2) clear state and invalidate prior-session requests; (3) deduplicate
  navigation and reject obsolete detail responses; (4) repair existing Devices
  timer resumption; (5) characterize cached Compute/Access refresh behavior
  and preserve focus/expanded state before introducing their passive refresh;
  (6) capture major states in both themes and route/role expectations.
- **Unchanged:** authentication, authorization, device commands, response/store
  fields, default routes, refresh endpoint effects and server job execution.
- **Evidence required:** account-switch/late-response tests, one request per
  initial detail, browser-clock non-overlap/resumption, all existing 25 browser
  tests retained, full verification and sanitized screenshots.
- **Risk/rollback:** revert each lifecycle fix independently; do not revert
  data or cancel backend jobs. Tests/documentation can remain after a revert.
- **Boundaries:** `test(web): capture session and route lifecycle regressions`;
  separate `fix(web): clear state on account change`, `fix(web): guard detail
  request identity`, and `fix(web): resume visible device refresh` changes.

### Phase 1 — Tokens and shared primitives

- **Objective:** fix H04/H06, M02/M03/M04 at reusable boundaries and establish
  consistent feedback for H05 without a visual redesign.
- **Routes/source:** shared dialogs/fields on all pages; `ui.js`, `charts.js`,
  existing CSS layers, wizard/alerts/Access controls, `index.html`.
- **Prerequisites:** Phase 0 identity/routing protections and modal baselines.
- **Ordered packages:** (1) named dialog/stack/scroll/close contract; (2) form
  labels, associated validation and busy/error helpers; (3) keyboard choice/
  disclosure/AP-lock controls; (4) text-specific light colors with computed pair
  checks; (5) coarse-pointer field sizing and action hit regions; (6) status/
  age/empty/error primitives, replacing one caller family at a time.
- **Unchanged:** native input behavior, autocomplete, secret handling, value
  formats, confirmation results, chart keyboard controls and action payloads.
- **Evidence required:** keyboard-only wizard/Access/detail completion; stacked
  dialog and direct-entry/back tests; control-name checks; dark/light screenshots,
  contrast calculations, computed touch dimensions; no increased remote calls.
- **Risk/rollback:** shared CSS/helper changes have broad reach. Keep palette,
  semantics, and layout changes in separate PRs; preserve the three link order
  and worker compatibility. Roll back a helper and its migrated callers together.
- **Boundaries:** `fix(a11y): name shared dialogs and operational controls`,
  `fix(web): retain nested dialog scroll locks`, `fix(a11y): support keyboard
  setup and detail controls`, `fix(styles): correct light text contrast`, then
  `refactor(web): extract tested feedback primitives`.

### Phase 2 — Application shell, navigation and page hierarchy

- **Objective:** resolve M07 and make existing features discoverable without
  changing product boundaries.
- **Routes/source:** `router.js`, `app.js`, `index.html`, Devices dashboard tabs,
  Compute filters/parent links, Settings section links, modal close handlers.
- **Prerequisites:** Phase 0 route guard and Phase 1 dialog/navigation primitives.
- **Ordered packages:** (1) allowed route registry and safe decoding; (2) visible
  member keyboard navigation and explicit not-found/forbidden handling;
  (3) documented optional filter/dashboard/section URL state; (4) consistent
  detail parent/return links and direct-entry handling; (5) local Settings
  navigation; (6) optional grouped Administration and contextual Add action,
  with old URLs preserved.
- **Unchanged:** every current deep link and alias, owner/admin policy, morning
  `checkRun` query, dashboard membership/order, default Devices landing page.
- **Evidence required:** full route×role tests, refresh/back/forward/direct-entry,
  malformed/missing ID, no selection loss, mobile horizontal-navigation and
  keyboard visibility; old notification URLs still resolve correctly.
- **Risk/rollback:** link/copy changes are reversible in shell only. Encode new
  state additively; do not require a new backend route or store migration.
- **Boundaries:** separate `fix(web): handle allowed and malformed routes`,
  `feat(web): preserve collection route context`, and
  `feat(web): add settings section navigation` PRs. Optional topbar regrouping
  follows usability validation, not a prerequisite for correctness fixes.

### Phase 3 — Operational dashboards and priority workflows

- **Objective:** H03/H05 recovery and M05 attention make the existing screens
  trustworthy for daily checks and incident triage.
- **Routes/source:** Devices, Compute, Access, notification/check-run links;
  feature APIs/status reducers, `settings.js`, progress and error components.
- **Prerequisites:** lifecycle, status and route primitives; characterize cached
  reads versus remote operations; resolve owner scope before any new aggregate.
- **Ordered packages:** (1) boot/offline/401 recovery and Access refresh errors;
  (2) persistent per-feature observation age/error and explicit unknown states;
  (3) passive Compute/Access cached refresh with preserved user context;
  (4) clickable per-feature attention/source summaries and Compute search;
  (5) clear eligible-workload bulk wording and named result destinations;
  (6) Ansible stage/next-action feedback. Overview/global Jobs remains gated.
- **Unchanged:** debounce, stale-source semantics, approved target/mode
  selection, independent native Proxmox updater, three-way bulk concurrency,
  no automatic reboot, VPN apply/verify/rollback and authoritative unread state.
- **Evidence required:** healthy/unknown/stale/offline, first-load and retained
  failures, partial source/job failure, reload during jobs, exact-node scope,
  retry and request-count assertions. No passive remote scans. Owner isolation
  tests accompany any additive metadata endpoint.
- **Risk/rollback:** separate cached-view updates from remote command workflows;
  revert individual summary/recovery components. Server jobs continue to be
  recoverable through current APIs and existing detail views.
- **Boundaries:** `fix(web): show durable refresh and offline recovery`,
  `feat(web): expose observation age and source failures`,
  `feat(compute): search workloads within host context`, and separate setup/
  bulk-copy changes. Do not combine these with backend decomposition.

### Phase 4 — Collections, forms and detail views

- **Objective:** resolve M01/M06/M08/M10, preserve drafts, and improve repeat
  operations across table/card/detail presentations.
- **Routes/source:** Access grid/filters/store/API/edit; `ui.js::buildTable`;
  Devices dashboards/wizard; Settings/Users forms; driver tables/interfaces;
  Compute workload grid and maintenance history.
- **Prerequisites:** Phase 1 fields/disclosures, Phase 2 context, Phase 3 stable
  refresh; explicit scope for paged/filtered bulk actions.
- **Ordered packages:** (1) shared sorting before rendering; (2) non-NAC client
  inspection with capability-gated actions; (3) bounded table rendering/counts/
  page navigation and named scroll regions; (4) coherent settings form widths,
  prerequisites and local failure retention; (5) non-secret wizard draft policy;
  (6) immediate interface rates; (7) preserve keyed cards/expanded detail state
  and contextual history while adopting existing Compute/VPN extraction paths.
- **Unchanged:** full-roster export, filtered bulk target contract, history
  retention/compaction messages, units, entity selection, bindings, alias/DNS
  options, blank-secret preservation, explicit approval and server validation.
- **Evidence required:** sort/search ties, IPv4/IPv6/missing values, pagination
  boundary/filter reset, selection and export scope, successful/invalid/503
  saves, two-sample/reset rates, 5,000-row DOM bound, no focus loss on refresh.
- **Risk/rollback:** one collection or form family per PR. Keep current API
  payloads; client paging is reversible without data migration. Gate new
  no-NAC write controls on verified server capability, not merely visibility.
- **Boundaries:** `fix(access): apply sorting to table rows`,
  `feat(access): inspect clients independently of enforcement`,
  `feat(access): bound roster rendering with pagination`, and separate Settings,
  wizard, interface-rate and feature-extraction PRs.

### Phase 5 — Responsive, accessibility and performance verification

- **Objective:** close measured layout/contrast/interaction gaps and validate
  complete workflows on representative platforms; improve only measured costs.
- **Routes/source:** all migrated views, CSS layers, chart ranges, collections,
  worker/module loading, browser test matrix.
- **Prerequisites:** Phases 1–4 stable; access to target browsers/devices and a
  repeatable synthetic large-data harness. Do not wait until here to fix
  keyboard/label blockers already identified in Phase 1.
- **Ordered packages:** (1) automated accessibility scans with reviewed
  exceptions; (2) keyboard and screen-reader full flows; (3) 320/390/768/1440,
  long labels, landscape and 200%/400% zoom; (4) chart date/range/error states;
  (5) five-sample performance comparisons, slow CPU/network and overlapping
  requests; (6) optional lazy feature loading only if measured benefit warrants
  the additional lifecycle/offline complexity.
- **Unchanged:** PWA installation/worker policy, no API caching, color-independent
  status, all keyboard chart/data alternatives, progressive job/VPN disclosure.
- **Evidence required:** named browser/device/OS reports, screenshots, focus and
  contrast results, collection/input/transfer timings with environment, offline
  first-use and previously-visited features, old shell/new backend compatibility.
- **Risk/rollback:** separate CSS, chart and loading changes. Any dynamic-import
  change must version the worker shell appropriately and be reverted with its
  cache dependency change. No framework/storage migration to meet a score.
- **Boundaries:** responsive/a11y fixes per component family, chart-context fix,
  then a separately justified performance PR. Never combine benchmarking claims
  with unmeasured production improvement.

### Phase 6 — Remove superseded paths and complete consistency review

- **Objective:** leave a single implementation/owner per migrated concern,
  durable docs and complete compatibility evidence.
- **Routes/source:** public feature coordinators, migrated UI exports, CSS
  selectors, worker asset list/behavior, frontend-state/architecture/verification
  documentation and e2e fixtures.
- **Prerequisites:** consumers migrated and all prior phase criteria recorded;
  backend-plan gates satisfied for any separately scheduled backend work.
- **Ordered packages:** (1) identify unused exports/selectors with callers and
  browser coverage; (2) remove temporary façades only after old-shell strategy
  is accounted for; (3) reconcile terminology/actions/spacing; (4) repeat
  route/state/manual matrix and full verifier; (5) update durable documentation
  and mark completed backlog rows with actual evidence/commit IDs.
- **Unchanged:** legacy route aliases, external APIs, persisted data and
  operational safeguards; compatibility is not “dead code” merely because the
  latest shell no longer uses a path.
- **Evidence required:** acyclic module graph, one state/timer owner, zero
  duplicate implementations, checked offline assets, full test results and
  explicit unresolved platform/production checks.
- **Risk/rollback:** remove exports and their callers atomically; retain a
  prior release artifact. Revert cleanup PRs without touching application data.
- **Boundaries:** `refactor(web): remove migrated UI compatibility exports`,
  separate proven-unused CSS cleanup, `docs: record completed UI architecture`.

## Prioritized backlog

Effort is relative implementation/test effort: S = small localized change;
M = several related components; L = feature slice; XL = optional cross-feature
capability. Estimates exclude external hardware access and are not time promises.
P0 precedes visual refactoring; P1 follows immediately; P2/P3 are ordered evolution.

| ID | Priority | Effort | User value | Engineering value | Dependencies | Phase |
|---|---|---|---|---|---|---|
| H01 | P0 | M | No previous-account data | Explicit session lifetime | Session/cache characterization | 0 |
| H02 | P0 | M | Correct device identity | Single request/route owner | H01 generation | 0 |
| H03 | P0 | M | Current, honestly aged information | Predictable timer disposal | H01/H02; cached endpoint map | 0 repair; 3 expansion |
| H04 | P1 | M | Complete keyboard workflows | Shared native interactions | Baseline; M02 for overlays | 1 |
| H05 | P1 | M | Understand and recover from failure | Unified error states | H01/H03 | 1 helpers; 3 flows |
| H06 | P1 | M | Identify inputs and controls | Reusable field contract | Field inventory | 1 |
| M02 | P1 | M | Consistent safe dialog use | One stack/close policy | H02 | 1–2 |
| M01 | P1 | M | Working sort, discoverable client history | Shared collection model | H04/H06 | 4; sort fix may ship earlier |
| M03 | P1 | S | Readable light-theme labels | Tested semantic color pairs | Theme baseline | 1 |
| M07 | P1 | M | Predictable navigation, retained work | Route/role model | H01/H02/M02 | 2; drafts 4 |
| M10 | P1 | S | Immediate available rates | Deterministic initial render | H02 | 4; may ship earlier |
| M04 | P2 | M | Usable touch controls/forms | Size tokens with computed checks | H04/M02 | 1 sizing; 5 platform checks |
| M05 | P2 | L | Faster operational triage | Shared status reductions | H03; owner-scope check | 3 |
| M06 | P2 | M | Easier controller configuration | Cohesive settings sections | H06/M07 | 2 navigation; 3–4 forms |
| M08 | P2 | L | Responsive large rosters | Bounded DOM/cost evidence | M01; bulk-scope contract | 4–5 |
| M09 | P2 | L | More reliable future changes | Smaller feature owners/tests | Existing refactor plan; characterize first | 0 guards; 4 moves; 6 cleanup |
| L01 | P3 | M | Clearer actions and terminology | Consistent action primitives | H04/M05 | 3 wording; 6 consistency |
| L02 | P3 | M | Interpret multi-day history | One chart-state contract | H05/M02 | 5 |
| O01 Overview | Gated | XL | Cross-domain daily triage if needed | Reuse authorized summaries | Owner decision; M05; scoped data | After 3 |
| O02 Jobs index | Deferred | L | Cross-workload active-work overview | Reuse persisted job projections | Demonstrated use; authorized query scope | After 3 |

## Test strategy and acceptance evidence

Preserve all current Python/browser tests and the coverage floor. Add focused
behavior tests; avoid assertions that merely repeat markup or freeze accidental
layout. New tests should use fictional fixtures and intercept all integration
side effects. Keep the current serial setup/server model until independent
test isolation is demonstrated.

| Layer | Required additions |
|---|---|
| Pure model | Route parsing/encoding/role destinations, sort/filter/paging, missing data/freshness, existing Compute eligibility/status reductions, chart dates/rates |
| Shared UI in browser | Named dialog stack/scroll/inert/focus/close paths, field validation, no-overlap polling/session epochs, late-response rejection, native keyboard choices/disclosures |
| Feature browser | H01–H06 repros, Access table/card parity, no-NAC history, Users/password saves and failures, Settings staged setup, draft policy, dashboard create/rename/delete/reorder/move, notification/checkRun destinations |
| Operational browser | Existing Compute node/job/reboot/bulk and VPN rollback tests retained; slow/error/canceled read responses cannot change resource identity or job ownership |
| Visual/responsive | Deterministic populated/empty/loading/error snapshots for 1440×900, 768×1024, 390×844 and 320px; both themes; long names, long errors, many dashboards/columns and text expansion |
| Accessibility | Accessible-name/state checks and automated scan; keyboard full workflows; NVDA+Firefox or equivalent desktop reader, VoiceOver+Safari on iOS; 200%/400% zoom, focus visibility/occlusion, contrast and touch target spacing |
| Performance | 100/1,000/5,000 clients and representative multi-host workloads; separate request/transfer/parse/model/DOM/input costs; repeated samples, known CPU/network conditions, request counts per route and hidden/visible lifecycle |
| PWA/compatibility | Fresh online, cached offline, unavailable boot, session expiry, existing worker upgrade, all newly split/imported assets cached as intended; no API response caching; old hashes/payloads and restored jobs |

Do not assume the general status endpoint is suitable for a member overview.
Any API metadata/query addition must have explicit visibility tests and retain
current compatibility routes. Frontend performance work does not require a
storage change: HTTP payload/DOM cost and JSON-store write cost are separate
measurements.

Manual sign-off scenarios: discover a fictional device; locate an offline or
stale source; inspect its metrics/history; find a workload by host and IP; run
an approved test maintenance job with one partial failure; inspect retained
diagnostics; configure a controller through approval; review client history
without enforcement; enable fictional NAC only after confirmation; review a
VPN replacement and failure/rollback evidence; leave/revisit during polling;
switch users after a failed load; reload a direct detail link; recover from an
offline shell. Record browser, viewport, fixture revision, expected/observed
behavior and screenshot/trace for each failure.

## Migration and rollback

1. Ship correctness fixes independently from code movement and visual changes.
   Preserve native module public entry points while callers migrate; keep only
   a forwarding façade, never two mutable stores or duplicate logic.
2. Preserve hashes, aliases, notification query strings, IDs, API payloads and
   persisted settings. New filter/section URL state is optional and has sensible
   defaults. Do not persist secrets or shared-browser private data to restore
   UI context. Leave existing theme/dashboard/order preferences intact where safe.
3. Characterize one feature, move it, validate it, then improve its presentation.
   Coordinate Compute/VPN moves with the existing refactor program so a UI PR
   does not race another owner of the same state or rename API fields.
4. Keep the three stylesheets ordered and migrate one selector family at a time.
   Worker cache/version changes accompany changes to loaded assets; test online
   upgrade, offline use and an older cached shell before removal of any asset.
5. Roll back by reverting the affected frontend PR/releasing the prior known
   frontend/image through the existing [operations procedure](operations.md).
   This task does not deploy or modify that procedure. No data-store migration
   is proposed; no volume deletion/restoration is needed for a UI rollback.
6. After rollback, verify login, legacy deep links, existing server job status,
   notifications and offline shell. Do not cancel real jobs or re-run commands
   just because a UI surface was reverted. Retain regression tests/evidence.

## Deferred and rejected approaches

| Idea | Decision and evidence |
|---|---|
| SQLite migration or history redesign | Rejected as a dependency of this UI plan. ADR 0001 retains JSON; schema 8 compacts only superseded successful Docker diagnostics. Current review measured browser rows, not production write latency. Repeat private post-deployment measurements before the backend plan's Phase 2. |
| Another cosmetic CSS split | Rejected. The previous >1,200-line trigger was met and the three ordered layers already exist. Extract tokens/primitives when behavior has shared ownership; don't reorganize files for appearance alone. |
| Framework/bundler/global-state rewrite | Rejected. Native modules and current tests support incremental repair. 28 eagerly fetched modules/roughly 372KB script transfer is a measurement to investigate, not proof a framework is needed. |
| Virtualize all tables/cards immediately | Deferred. First establish bounded DOM paging and repeated browser measurements; virtualization complicates keyboard, reading order and find-in-page. |
| Server pagination/global search immediately | Deferred. Current contracts return complete collections. Add compatible owner-aware query contracts only if transfer/parse measurements or product requirements warrant them. |
| Replace details with new standalone implementations | Rejected. Reuse routed detail content in modal/page shells and preserve deep links; two parallel implementations would multiply regression risk. |
| New Services/Infrastructure inventory | Rejected without a product requirement. Current Devices → Compute → Docker hierarchy already represents existing capabilities; do not duplicate resources. |
| Automatic repair/update/reboot/VPN switching | Rejected. Existing explicit approvals, confirmations, exact target scope and rollback contract are product safeguards, not UX friction to remove. |
| New global Overview/Jobs/Alerts pages by default | Deferred pending operator workflow/scope decision. Start with per-feature attention and links to existing histories and alerts. |
| Client caching of live API data for offline mode | Rejected. Shared-browser/session isolation is already a concern (H01); keep the static-only worker cache and honest offline state. |
| Cosmetic redesign, card restyling or framework benchmarks as success criteria | Rejected. Every accepted change must resolve a listed behavior/consistency/accessibility/maintenance problem with evidence. |

Production follow-up, if later authorized, uses the existing administrator
`GET /api/diagnostics/metrics` collection command in
[Operations](operations.md#logs-and-diagnostics) and the baseline template in
[Verification](verification.md). Collect after a representative cycle and
normal authenticated traffic; keep the values private. This review neither
reassesses that accepted storage decision from synthetic rows nor records
production measurements.

## Product-owner questions

These affect optional product behavior; they do not block H01–H06 or current
workflow fixes.

1. Is the primary landing task inventory browsing, morning update review, or
   incident triage? Keep Devices as default unless an Overview is explicitly
   preferred after the improved per-feature summaries are tried.
2. Is a cross-workload Jobs view needed during daily operations, or is the
   current contextual maintenance history sufficient? Identify a concrete
   missed-work scenario before adding another navigation destination.
3. Which account roles should see a cross-domain/global operational summary?
   The current public operational API and owner-scoped application collections
   have different scopes; a member overview must not imply a new visibility policy.
4. What representative environment sizes and minimum client hardware should
   define performance acceptance? Synthetic 100/1,000/5,000-client cases are
   useful regression tiers, not observed deployment sizes or product limits.
5. Which mobile browsers/installed-PWA workflows are required for release
   sign-off? Obtain those devices for touch, keyboard/zoom, certificate trust
   and notifications testing rather than inferring success from Chromium.

## Completion record for this review

Inspected root instructions/README/CONTRIBUTING; architecture, configuration,
frontend-state, operations, security, API, Compute, VPN, verification, existing
refactor plan and ADR 0001; all frontend module families and three CSS layers;
e2e setup/support and six feature/setup spec files; related Python frontend,
ASGI, Compute/monitoring/notification/VPN tests; verification scripts/config;
recent Git history including CSS layering and discovery-history compaction.
The route inventory records source-only or externally unverified edges.

The review produced this plan and sanitized evidence under `docs/ui-review/`.
No `web/`, backend, existing tests, dependency files or deployment configuration
were changed. No commit, staging, push, pull request, production mutation or
external integration action was performed. Implementation remains future work.
