# UI deep dive: refactoring & improvement recommendations

Scope: `web/` — 12 ES modules (~4,200 lines JS), `index.html` (~390 lines),
`styles.css` (~790 lines), `sw.js`. Reviewed after the §3/§4 refactors from
REVIEW.md landed (module split, keyed reconciliation, hash routing, timer
hygiene), so this is the *next* round, not a repeat of those.

> **Status: implemented**, as of 2026-07-18. Every §2 refactor and §3
> feature has landed (see per-item ✅ marks below). The one deliberate
> deferral is splitting `styles.css` into layer files (§2.6) — it stays a
> single sectioned file; see §5.

## 1. What's in good shape (keep, and lean on harder)

- **Module layering is clean**: `api.js` (no imports) → `ui.js`/`charts.js`
  (generic) → feature modules → `app.js` (boot/routing). No build step, native
  ESM — right call for this project.
- **`reconcileList` + per-card `patch()`** is a genuinely good hand-rolled
  virtual-DOM substitute: expanded cards and focus survive the background
  refresh. New list views should always use it (the Access card grid and the
  device grid already do).
- **`visiblePoll` / `startRelativeTimeTicker` / `withBusy`** solved the timer
  and stuck-button classes of bugs centrally. Same for `renderError` +
  textContent-only rendering of device-supplied strings (XSS hygiene).
- **`charts.js` is state-free** and the chart-registry refresh pattern (repaint
  in place, never rebuild while hovering) is solid.

## 2. Refactoring recommendations

### 2.1 `detail.js` (1,259 lines) is the new `app.js` — ✅ done (`e813dd7`)
It holds seven distinguishable features: modal shell/live tick, metric+donut
cards, generic tables (+cell chart/pie popups), the wireless-clients list +
AP-lock, the interfaces section, the firewall-rules editor, and the alerts
editor. Natural split, mirroring the existing section comments:

```
web/js/detail/index.js      openDevice/closeDevice, live tick, renderDetail dispatch
web/js/detail/metrics.js    metricCard, chartCard, donutCard
web/js/detail/tables.js     detailTable, clientsList, radiosTable, rowActionButton
web/js/detail/interfaces.js interfacesSection, ifTable, ifRate, dualChartCard
web/js/detail/firewall.js   firewallSection
web/js/detail/alerts.js     alertsSection
```

Purely mechanical; the only shared state is `DM`, which argues for passing it
into builders instead of importing it (see 2.3).

### 2.2 `clients.js` (770+ lines) mixes three features — ✅ done (`1ee13b6`)
Card grid/roster view, the edit-client modal, and the NAC setup flows. The
modal and setup flows (`openClientEdit` + `#ce-form` handler; `nacSetup*`)
are ~300 lines that never touch the grid — split into
`clients/edit-modal.js` and `clients/nac-setup.js` when the file is next
touched.

### 2.3 Break the `app.js ↔ clients.js` import cycle — ✅ done (`90d4f8d`, `router.js`)
`clients.js` imports `switchTab` from `app.js`, which imports `loadClients`
from `clients.js`. ESM tolerates it today, but it's the kind of cycle that
turns into a "cannot access before initialization" surprise when someone
reorders top-level statements. Move `switchTab`/routing into a tiny
`router.js` (imported by both), or dispatch a `CustomEvent("hlhq:navigate")`
that `app.js` listens for. Same fix removes `devices.js → detail.js →
devices.js` (loadDevices/renameDevice vs openDevice).

### 2.4 Three hand-rolled table builders — ✅ done (`b70ce32`, `buildTable()` in `ui.js`)
`clientsTable` (clients.js), `detailTable` (detail.js) and the header-building
in `ifTable` all repeat thead/tbody/severity/mono-class logic with small
variations. Extract one `buildTable({cols, rows, cellFn})` into `ui.js`;
`detailTable`'s cell-chart/pie hooks become the `cellFn`. ~80 lines saved and
one place to add future features (sticky headers, column sorting).

### 2.5 Modal plumbing: three implementations of "close on Esc/backdrop" — ✅ done
`pushModal(el, {onEscape})` + one capture-phase, stack-aware Escape router in
`ui.js` now closes the topmost modal only; the device modal, client modal,
shared dialog and `openOverlay` all pass their close function instead of
wiring their own document-level Escape listeners. `openDevice()` also stopped
double-pushing the stack when re-opened in place.

### 2.6 Small consistency items
- ✅ `wizard.js renderCandidates` renders `c.displayName` via textContent.
- ✅ `timeAgo` shows days and weeks (`web/js/api.js`).
- ✅ `settings.js` statuses use toasts + `withBusy` instead of ad-hoc `<p>`
  writes (the `pw-msg`/`push-msg`/`na-msg` elements are gone).
- ✅ Named constants for the intervals: `DEVICES_POLL_MS`,
  `DETAIL_REFRESH_MS`, `RELTIME_TICK_MS`, `API_TIMEOUT_MS`.
- ⬜ `styles.css` stays one sectioned file — splitting it into
  `base/components/views` was judged not worth the churn for ~800 lines
  with clear section banners (deliberately deferred, see §5).

### 2.7 State pattern: fine at this size — document it
Module-level singletons (`ALL_DEVICES`, `CLIENTS`, `DM`) + explicit
`renderX()` calls is appropriate for this app; a framework would be net
negative. The one rule worth writing down in a comment (it's implicit today):
*mutate the singleton, then call the render function; never touch another
module's DOM.* The few violations (detail.js reaching into `#dm-*` nodes it
doesn't own) are what the 2.1 split cleans up.

## 3. UX / product improvement ideas

Ordered roughly by value-for-effort.

1. ✅ **Time-range picker on charts** — `chartCard` has 2h/24h/7d buttons;
   the poller keeps a downsampled 7-day series (`historyLong`, one point
   per 5 min) served by `/history?range=`.
2. ✅ **Access roster** *(shipped)*: online/offline state, last-seen,
   connection history, status filter, Forget, presence push notifications
   (`1ee13b6`), and the "X new events since last visit" badge on the
   Access tab (`/api/clients/events` + localStorage last-visit).
3. ✅ **Devices tab summary strip** — done (`a57e72e`).
4. ✅ **Offline duration on device cards** — done (`a57e72e`).
5. ✅ **PWA offline shell** — stale-while-revalidate caching done
   (`8635baf`); `navigator.setAppBadge` carries the needs-approval count.
6. ✅ **Uptime history strip** — the per-poll online flag persists as its
   own series (~24h) and renders as an Availability bar in the detail view.
7. ✅ **Search/filter parity** — Devices tab has an All/Online/Offline
   status filter next to search.
8. ✅ **Accessibility pass, round 2** — `aria-live` summary strips, a
   now/min/peak `aria-label` sentence on every chart canvas, and
   keyboard-driven chart inspection (focus + arrow keys step the hover
   tooltip).
9. ✅ **Bulk actions on Access** — approve-all-shown / forget-offline-shown
   from the "⋯" menu, operating on the filtered view; the endpoints accept
   MAC batches.
10. ✅ **CSV/JSON export** — `/api/clients/export?format=csv|json` (JSON
    includes per-client connection history), reached from the same menu.

## 4. Suggested sequencing (historical — all landed)

| # | Item | Size | Status |
|---|------|------|--------|
| 1 | 2.3 router extraction (unblocks clean splits) | S | ✅ done |
| 2 | 2.1 detail.js split | M (mechanical) | ✅ done |
| 3 | 2.4 shared table builder | S–M | ✅ done |
| 4 | 3.3 / 3.4 device summary + offline duration | S | ✅ done |
| 5 | 3.5 PWA shell caching | S | ✅ done |
| 6 | history migration → 3.1 time ranges → 3.6 uptime strip | M | ✅ done |
| 7 | 2.2 clients.js split + 3.2 presence notifications | M | ✅ done |

Keep refactors (2.x) in separate commits from features (3.x), matching the
repo's existing review discipline.

## 5. Outstanding tasks

All of the previously outstanding work has landed — the §2 refactors
(including 2.5 modal plumbing and the 2.6 consistency items), the §3
features (time ranges, uptime strip, status-filter parity, a11y round 2,
bulk actions, exports, both badges), plus the CSP header carried over from
REVIEW.md §5.1.

The single deliberate deferral:

1. **`styles.css` layer split** (§2.6) — still one file. It's ~850 lines
   with clear section banners; splitting it into `base/components/views`
   adds requests and churn without making anything easier to find yet.
   Revisit if it grows past ~1,200 lines or gains theme variants.
