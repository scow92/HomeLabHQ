# UI deep dive: refactoring & improvement recommendations

Scope: `web/` — 12 ES modules (~4,200 lines JS), `index.html` (~390 lines),
`styles.css` (~790 lines), `sw.js`. Reviewed after the §3/§4 refactors from
REVIEW.md landed (module split, keyed reconciliation, hash routing, timer
hygiene), so this is the *next* round, not a repeat of those.

> **Status: partially implemented**, as of the 2026-07-18 review. §2.1–§2.5
> and most of §3 have landed (see per-item ✅ marks below and the sequencing
> table in §4, which now lists only what's left). Outstanding work is
> tracked in **§5 Outstanding tasks**.

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

### 2.5 Modal plumbing: three implementations of "close on Esc/backdrop" — ⬜ outstanding
Device modal, client modal, and the shared dialog each wire their own
document-level click + Escape handlers; `openOverlay` already encapsulates
the pattern for dynamic overlays. Fold the two static modals onto a small
`bindModal(el, {onClose})` helper so Escape-priority (topmost modal first) is
handled once — today, Escape while the series-overlay is open *also* reaches
the device-modal handler and only ordering luck closes the right one.

### 2.6 Small consistency items
- ⬜ `wizard.js renderCandidates` still injects `c.displayName` via
  `innerHTML` (`web/js/wizard.js:109`). Still the one deviation from the
  textContent rule.
- ✅ `timeAgo` now shows days (`web/js/api.js`). ⬜ Weeks still not added
  (caps at "`Nd ago`").
- ⬜ `settings.js` still writes status into an ad-hoc `<p>` (`box.innerHTML
  = "<p class='muted'>…</p>"`, `web/js/settings.js:77`) instead of a toast.
- ⬜ Magic numbers are still inline (`15000` in `devices.js`, `20000` in
  `detail/index.js`, `30000` in `ui.js`/`api.js`) — no named constants yet.
- ⬜ `styles.css` is still one 782-line file — not yet split into
  `base/components/views`.

### 2.7 State pattern: fine at this size — document it
Module-level singletons (`ALL_DEVICES`, `CLIENTS`, `DM`) + explicit
`renderX()` calls is appropriate for this app; a framework would be net
negative. The one rule worth writing down in a comment (it's implicit today):
*mutate the singleton, then call the render function; never touch another
module's DOM.* The few violations (detail.js reaching into `#dm-*` nodes it
doesn't own) are what the 2.1 split cleans up.

## 3. UX / product improvement ideas

Ordered roughly by value-for-effort.

1. ⬜ **Time-range picker on charts** — no longer blocked: the history
   migration (`history-migration.md`) has landed, but `HISTORY_MAX` is
   still 120 points (~2h) and no 2h/24h/7d buttons exist on `chartCard`.
   Still the single biggest monitoring win, and now unblocked.
2. ✅ **Access roster** *(shipped)*: online/offline state, last-seen,
   connection history, status filter, Forget, and presence push
   notifications (`1ee13b6`) are all in. ⬜ Still missing: "X new events
   since last visit" badge on the Access tab.
3. ✅ **Devices tab summary strip** — done (`a57e72e`).
4. ✅ **Offline duration on device cards** — done (`a57e72e`).
5. ✅ **PWA offline shell** — stale-while-revalidate caching done
   (`8635baf`). ⬜ `navigator.setAppBadge` with the needs-approval count is
   not implemented.
6. ⬜ **Uptime history strip** — not implemented. The per-poll online flag
   still isn't persisted as its own series; needs a small addition to
   `history.py`/`poller.py` plus a render strip in `detail/index.js`.
7. ⬜ **Search/filter parity** — Devices tab still has no "offline only"
   filter (Access has status filtering, Devices doesn't).
8. ⬜ **Accessibility pass, round 2** — tablist/dialog semantics and
   `sr-only` status text on cards are in place, but `aria-live` on the
   summary strips, a text alternative for canvas charts (`sr-only`
   min/max/current sentence), and a keyboard-reachable chart hover tooltip
   are all still missing.
9. ⬜ **Bulk actions on Access** — no approve-all-filtered /
   forget-all-offline yet.
10. ⬜ **CSV/JSON export** of the client roster and history — not
    implemented; no export endpoint or button exists.

## 4. Suggested sequencing (historical — see §5 for what's actually left)

| # | Item | Size | Status |
|---|------|------|--------|
| 1 | 2.3 router extraction (unblocks clean splits) | S | ✅ done |
| 2 | 2.1 detail.js split | M (mechanical) | ✅ done |
| 3 | 2.4 shared table builder | S–M | ✅ done |
| 4 | 3.3 / 3.4 device summary + offline duration | S | ✅ done |
| 5 | 3.5 PWA shell caching | S | ✅ done |
| 6 | history migration → 3.1 time ranges → 3.6 uptime strip | M | history migration ✅; time ranges + uptime strip ⬜ |
| 7 | 2.2 clients.js split + 3.2 presence notifications | M | ✅ done |

Keep refactors (2.x) in separate commits from features (3.x), matching the
repo's existing review discipline.

## 5. Outstanding tasks

Everything below is unimplemented as of this review. Roughly ordered by
value-for-effort (per the original §3 ranking):

1. **Chart time-range picker + retention bump** (§3.1) — unblocked by the
   history migration; add 2h/24h/7d buttons to `chartCard` and raise
   `HISTORY_MAX` (or move to a downsampled server-side range).
2. **Uptime history strip** (§3.6) — persist the per-poll online flag as a
   series and render a 24h availability bar per device.
3. **CSP header** (carried over from REVIEW.md §5.1's backstop suggestion,
   never implemented) — add `Content-Security-Policy: default-src 'self'`
   in `backend/app.py`; the app is fully self-contained so this should be
   close to free.
4. **Accessibility pass, round 2** (§3.8) — `aria-live` on summary strips,
   `sr-only` min/max/current text for canvas charts, keyboard-reachable
   chart hover tooltip.
5. **Modal plumbing consolidation** (§2.5) — fold the device modal and
   client modal onto the existing `openOverlay` stack helper in `ui.js` so
   Escape-priority is handled in one place.
6. **Devices tab "offline only" filter** (§3.7) — parity with Access's
   status filter.
7. **Bulk actions on Access** (§3.9) — approve-all-filtered /
   forget-all-offline.
8. **CSV/JSON export** (§3.10) — client roster and history export.
9. **Small consistency items** (§2.6) — `wizard.js` innerHTML → textContent,
   `timeAgo` week units, `settings.js` toasts instead of ad-hoc `<p>`,
   named constants for the poll/tick intervals, and eventually splitting
   `styles.css` into layers.
10. **"New events since last visit" badge** on the Access tab (§3, item 2
    follow-up).
11. **`navigator.setAppBadge`** with the needs-approval count (§3, item 5
    follow-up).
