# UI deep dive: refactoring & improvement recommendations

Scope: `web/` — 12 ES modules (~4,200 lines JS), `index.html` (~390 lines),
`styles.css` (~790 lines), `sw.js`. Reviewed after the §3/§4 refactors from
REVIEW.md landed (module split, keyed reconciliation, hash routing, timer
hygiene), so this is the *next* round, not a repeat of those.

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

### 2.1 `detail.js` (1,259 lines) is the new `app.js`
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

### 2.2 `clients.js` (770+ lines) mixes three features
Card grid/roster view, the edit-client modal, and the NAC setup flows. The
modal and setup flows (`openClientEdit` + `#ce-form` handler; `nacSetup*`)
are ~300 lines that never touch the grid — split into
`clients/edit-modal.js` and `clients/nac-setup.js` when the file is next
touched.

### 2.3 Break the `app.js ↔ clients.js` import cycle
`clients.js` imports `switchTab` from `app.js`, which imports `loadClients`
from `clients.js`. ESM tolerates it today, but it's the kind of cycle that
turns into a "cannot access before initialization" surprise when someone
reorders top-level statements. Move `switchTab`/routing into a tiny
`router.js` (imported by both), or dispatch a `CustomEvent("hlhq:navigate")`
that `app.js` listens for. Same fix removes `devices.js → detail.js →
devices.js` (loadDevices/renameDevice vs openDevice).

### 2.4 Three hand-rolled table builders
`clientsTable` (clients.js), `detailTable` (detail.js) and the header-building
in `ifTable` all repeat thead/tbody/severity/mono-class logic with small
variations. Extract one `buildTable({cols, rows, cellFn})` into `ui.js`;
`detailTable`'s cell-chart/pie hooks become the `cellFn`. ~80 lines saved and
one place to add future features (sticky headers, column sorting).

### 2.5 Modal plumbing: three implementations of "close on Esc/backdrop"
Device modal, client modal, and the shared dialog each wire their own
document-level click + Escape handlers; `openOverlay` already encapsulates
the pattern for dynamic overlays. Fold the two static modals onto a small
`bindModal(el, {onClose})` helper so Escape-priority (topmost modal first) is
handled once — today, Escape while the series-overlay is open *also* reaches
the device-modal handler and only ordering luck closes the right one.

### 2.6 Small consistency items
- `wizard.js renderCandidates` injects `c.displayName` via a template string
  (`innerHTML`). The values are server-curated driver names, so not
  exploitable today, but it's the one deviation from the textContent rule —
  align it before someone copies the pattern for user data.
- `timeAgo` capped at hours ("52h ago"); now shows days (fixed alongside the
  Access roster work). Consider weeks next.
- `settings.js` writes status into ad-hoc `<p>` elements while everything
  else uses toasts; pick one (toasts) for consistency.
- Magic numbers (`15000` device poll, `20000` detail tick, `30000` ticker)
  deserve named constants in one place; makes the "why is my battery warm"
  tuning conversation easier.
- CSS: at 790 lines a single file is still fine, but the client-card block
  now has ~30 selectors; when it next grows, split `styles.css` into
  `base/components/views` layers — no tooling needed with plain `@import`
  or multiple `<link>`s.

### 2.7 State pattern: fine at this size — document it
Module-level singletons (`ALL_DEVICES`, `CLIENTS`, `DM`) + explicit
`renderX()` calls is appropriate for this app; a framework would be net
negative. The one rule worth writing down in a comment (it's implicit today):
*mutate the singleton, then call the render function; never touch another
module's DOM.* The few violations (detail.js reaching into `#dm-*` nodes it
doesn't own) are what the 2.1 split cleans up.

## 3. UX / product improvement ideas

Ordered roughly by value-for-effort.

1. **Time-range picker on charts** — blocked on the history-storage migration
   (`history-migration.md`); once retention can grow, add 2h/24h/7d buttons
   to `chartCard`. This is the single biggest monitoring win.
2. **Access roster** *(shipped with this change)*: online/offline state,
   last-seen, connection history, status filter, Forget. Follow-ups:
   - presence push notifications per named device ("phone came home /
     left") — the roster events already carry the data; wire an opt-in flag
     per client into `_track_clients`'s transition points and reuse
     `push.notify`.
   - show "X new events since last visit" badge on the Access tab.
3. **Devices tab summary strip** — "12 devices · 11 online · 1 offline" above
   the grid (the Access tab now has one; Devices doesn't). Cheap:
   `ALL_DEVICES` already carries `state`.
4. **Offline duration on device cards** — "offline for 3h" (from the state
   timestamps) reads much better than a grey dot alone, and d-units in
   `timeAgo` now support it.
5. **PWA offline shell** — `sw.js` deliberately does no caching, so a network
   blip shows the browser error page inside the installed app. Cache the
   static shell (index/css/js/icons) with a stale-while-revalidate strategy;
   keep `/api/*` network-only. Also consider `navigator.setAppBadge` with the
   needs-approval count.
6. **Uptime history strip** — a 24 h red/green availability bar per device
   (like status pages) once history moves out of the main doc; the poller
   already records the per-poll online flag, it's just not persisted as a
   series.
7. **Search/filter parity** — Devices search hides under 5 devices and can't
   filter by status; Access now has status filtering. Unify: always show
   search, add "offline only" to Devices.
8. **Accessibility pass, round 2** (REVIEW.md §4.4 called it ongoing):
   tablist/dialog semantics are good; still missing are `aria-live` on the
   summary strips (they change under polling), text alternatives for canvas
   charts (a `sr-only` min/max/current sentence — data is already computed in
   `headFn`), and `prefers-reduced-motion` guards exist but the chart hover
   tooltip isn't keyboard-reachable.
9. **Bulk actions on Access** — approve-all-filtered / forget-all-offline
   once roster lists grow; the confirm dialog + `withBusy` primitives make
   this cheap.
10. **CSV/JSON export** of the client roster and history (one endpoint, one
    button) — useful for the NAC audit story the app is building toward.

## 4. Suggested sequencing

| # | Item | Size |
|---|------|------|
| 1 | 2.3 router extraction (unblocks clean splits) | S |
| 2 | 2.1 detail.js split | M (mechanical) |
| 3 | 2.4 shared table builder | S–M |
| 4 | 3.3 / 3.4 device summary + offline duration | S |
| 5 | 3.5 PWA shell caching | S |
| 6 | history migration → 3.1 time ranges → 3.6 uptime strip | M |
| 7 | 2.2 clients.js split + 3.2 presence notifications | M |

Keep refactors (2.x) in separate commits from features (3.x), matching the
repo's existing review discipline.
