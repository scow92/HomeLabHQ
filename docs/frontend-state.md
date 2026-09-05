# Frontend state ownership

Each feature module owns the state that drives its view. A module that needs
to perform work for another feature receives data and callbacks, or emits a
named UI event for router-level coordination; it does not import that feature's
mutable state or manipulate its DOM.

The client feature follows this boundary:

- `web/js/clients/store.js` owns the roster snapshot.
- `filters.js` owns filter and sort state.
- `api.js` contains client HTTP calls.
- `grid.js` renders the roster and emits user intents through callbacks.
- `actions.js` performs mutations supplied with a re-render callback.
- `index.js` is the only coordinator that combines the preceding modules.

The practical rule is: mutate the owning module's state, invoke that module's
render function, and never directly mutate another feature module's DOM.

Authentication is a lifetime boundary. `api.js::setSession()` advances a
generation on every authentication change (including same-user reauthentication),
aborts pending requests and synchronously invokes registered `onSessionChange`
disposers. The app shell reacts after the existing feature modules have disposed
their state. A current protected 401 clears the session at response headers;
stale responses cannot clear a newer session or return their payload to callers.
API requests use `no-store` and cannot start unauthenticated except for the four
public authentication endpoints.

Each owner clears its account-scoped memory and DOM at this boundary:

| Owner | Disposed state |
|---|---|
| Devices | Device/dashboard snapshots, keyed card cache, morning-run results, search/dashboard selection, drag state and poll handle |
| Device detail / shared UI | Detail snapshot, charts, interface-edit state, static detail contents, dynamic overlays, pending dialogs, focus traps and private visible polls |
| Access | Roster, keyed cards, search/status, badge request generation and edit-modal data |
| Compute | Inventory, parent filters, detail, maintenance caches, client polling and pending bulk continuations; server jobs keep running |
| Settings / wizard / Users / Logs | Account/controller snapshots, entered drafts and secrets, loaded results and diagnostics |
| Notifications | Existing notification snapshot, badge and request-sequence cleanup |

Theme, Access sort, driver-name metadata and Access seen timestamps keyed by
user ID are retained intentionally. No account data is added to browser storage
or the service-worker cache. Do not replace disposal with hiding the app or
clearing all local storage.

Async coordinators that retain snapshots across awaits must capture
`getSessionGeneration()` and check `isCurrentSession()` before rendering,
restarting timers or continuing a batch. Check failure paths too: the shared API
rejects obsolete work with `SessionChangedError`. Aborting a request alone does
not invalidate an already-buffered body. Page-hide clears protected state, and
a persisted page-show rechecks the session before restoring the app.

See [H01's verification record](UI_REFACTOR_PLAN.md#h01--clear-feature-state-at-the-account-boundary)
and `e2e/session.spec.mjs`. Timer-resumption improvements remain separate work.

Within a session, `request-owner.js::requestOwner()` provides a latest-request
token with `signal`, `current()` and synchronous invalidation. It reuses the
API's session generation; it does not establish another authentication lifetime.
Pass the token as API options (or spread it alongside method/timeout options).
The API checks ownership at headers before processing a protected 401, and again
at body completion. An obsolete view's 401 cannot expire the winning view;
current protected 401s still use H01's session invalidation.
The router owns navigation activation and passes its token into required
inventory lookups. A history traversal's popstate/hashchange pair activates once;
detail open intents use pushState without recursively dispatching hash navigation.

Device and Compute detail owners keep presentation lifetime separate from read
ordering. Opening/replacing a presentation captures its encoded resource route,
modal and body node; closing it or replacing its DOM invalidates that ownership.
Each read must still own both the session and that presentation before committing
snapshots, titles, content, errors, loading cleanup or live-chart updates. Aborting
is an optimization: a decoded response can arrive after cancellation, so every
continuation must check `current()` too. A refresh of the same resource is still a
new request. Maintenance completions may refresh only their originating view;
server jobs continue independently.

Nested history charts, series overlays, VPN profiles/settings, driver choices and
firewall choices have local read owners tied to the parent's presentation signal
and their own range/profile/DOM identity. Leaving or replacing the parent closes
its nested presentations. A disposed element must never acquire new content just
because a new element now has the same ID. Poll intervals and visibility-driven
resumption are unchanged. See [H02's verification record](UI_REFACTOR_PLAN.md#h02--make-route-transitions-and-detail-responses-single-owner-operations)
and `e2e/detail-ownership.spec.mjs`.
