# HomelabHQ — Code Review: Refactoring & UI Recommendations

> **Status: ✅ Complete.** Every item below (§1–§6) has been implemented —
> see `git log` for the corresponding commits (bugs §1, backend refactor §2,
> frontend refactor §3, UX §4, and security hardening §5 all landed). This
> document is kept as a historical record. Follow-on work identified during
> the post-refactor UI deep dive lives in `refactor.md`; that file is the
> current source of outstanding tasks.

Reviewed at commit `e626468` (July 2026). Scope: the whole tree — `backend/`
(~5.9k lines Python), `web/` (~4.8k lines JS/HTML/CSS), plus a skim of the 17
drivers and `_verify/` harness.

**Overall impression:** this is a well-crafted codebase for its size. The
driver/transport separation is clean, comments explain *why* rather than
*what*, error paths fail soft in the right places, and the frontend avoids
frameworks without descending into spaghetti. The recommendations below are
about keeping it that way as it grows — plus a handful of genuine bugs found
along the way.

---

## 1. Bugs found during review (fix first)

### 1.1 Duplicate `/api/drivers` route — the transport filter is dead code
`backend/app.py:292` and `backend/app.py:326` both handle
`path == "/api/drivers"`. The second block (which reads `?transport=` and
filters/sorts) is unreachable — the first block always returns first, ignoring
the query string.

**User-visible effect:** the "Change driver" dialog (`web/app.js:1535` calls
`/api/drivers?transport=…`) lists **every** driver, including ones that don't
speak the device's transport. Picking an incompatible one round-trips to the
server just to fail with `driver X does not speak Y`.

**Fix:** delete the first block and keep the filtering one (it degrades to the
full catalogue when no `transport` is given; just add the `transports` key the
wizard needs to its response).

### 1.2 Device card status dot ignores the offline debounce
The poller carefully debounces reachability (`confirmedOnline`,
`OFFLINE_AFTER=5` misses — `backend/poller.py:189-198`) so notifications don't
flap on slow management planes. But the UI renders the **raw** `state.online`
(`web/app.js:1302`, `web/app.js:1463`), so a card's dot flips red on a single
missed poll while notifications correctly stay quiet. Render
`state.confirmedOnline` (fall back to `online` for old records), and consider
a third visual state ("1/5 missed" tooltip) so a slow device reads as
"degraded" rather than flapping.

### 1.3 A transient fetch error makes the Devices tab claim "No devices yet."
`loadDevices()`'s catch path (`web/app.js:292-296`) wipes the list and shows
the **empty state** — whose default copy is "No devices yet. Add a router…".
A server hiccup or briefly dropped Wi-Fi makes it look like every device was
deleted. Keep the last-good render and surface a toast/banner instead
(exactly what `loadClients()` already does — `web/app.js:413-417`).

### 1.4 "Sync now" marks a device offline when it simply has no values
`web/app.js:1312`: `dot.className = … (Object.keys(r.values).length ? "up" :
"down")`. A reachable device whose selected entities all returned errors — or
a device with zero numeric sensors — shows as down even though the request
succeeded. Success of the `/state` call itself should mean "up".

---

## 2. Backend refactoring

### 2.1 `app.py`: replace the if-ladder with a route table
`_api_get`/`_api_post` are ~500 lines of repeating this exact shape
(≈12 occurrences):

```python
x = _match(path, "/api/devices/", "/suffix")
if x:
    dev = devices.get_device(x)
    if not dev or not _owns(user, dev):
        return self._send_json(404, {"error": "not found"})
    try:
        result = devices.something(x, ...)
    except ValueError as e:
        return self._send_json(400, {"error": str(e)})
    except transports.ConnectionError as e:
        return self._send_json(502, {"error": str(e)})
    except Exception as e:
        return self._send_json(500, {"error": str(e)})
    return self._send_json(200, result)
```

Two small extractions collapse most of the file:

1. **One error mapper.** A single `def _api_call(self, fn, *a)` that runs `fn`
   and maps `ValueError→400`, `transports.ConnectionError→502`,
   `Exception→500`. Every endpoint body becomes one line.
2. **A device-scoped route helper.** `def _device_route(self, path, suffix)`
   that does the `_match` + `get_device` + `_owns` dance and returns
   `(dev_id, dev)` or sends the 404 itself. Or go one step further and declare
   a table: `ROUTES = [("POST", "/api/devices/", "/action", handler), …]` and
   loop it — new endpoints then become data, not another 20-line block.

This isn't cosmetic: today, forgetting one `except transports.ConnectionError`
in a new endpoint silently turns a firewall timeout into a 500 with a raw
traceback string.

### 2.2 Split `devices.py` (1,166 lines) by domain
It currently mixes four unrelated concerns. A natural split, with no circular
imports required:

| New module | Moves from `devices.py` |
|---|---|
| `devices.py` (keep) | CRUD, `_public`, `read_state`, `read_detail`, `poll_read`, bindings |
| `nac.py` | `nac_*`, `_nac_device`, `_track_clients`, `edit_client`, `client_membership`, alias management, `scan_new_clients` |
| `clients.py` | `list_clients`, `_device_clients`, merge/hostname logic |
| `firewall.py` | `_firewall_conn`, `firewall_*` |

The NAC block alone is ~600 lines and has its own vocabulary (aliases,
enforcement, dnsmasq sync) that a device-CRUD reader never needs.

### 2.3 A `device_conn()` context manager
The pattern `conn = transports.open_connection(dev[...], …)` /
`try: … finally: conn.close()` appears **11 times** in `devices.py`, and
`_firewall_conn`/`_nac_conn` are near-duplicates of each other. `Connection`
already implements `__enter__/__exit__` (`backend/transports.py:34-38`) — it's
just never used. Suggested shape:

```python
@contextmanager
def device_conn(dev_id, timeout=15, require=None):   # require="nac"/"firewall"
    dev = get_device(dev_id)
    if not dev: raise ValueError("device not found")
    drv = _drv_for(dev)
    if require and not getattr(drv, CAPS[require], None):
        raise ValueError(f"device does not support {require}")
    with transports.open_connection(...) as conn:
        yield dev, drv, conn
```

This also fixes the awkward manual `conn.close()`-before-raise in
`nac_approve`/`nac_set_enforcement` (`backend/devices.py:501,515`).

### 2.4 Store: the whole document is parsed per call — and it's growing
Every `store.load()` reads + parses the entire JSON doc, and history makes that
doc big: 120 points × every numeric entity × every device, **plus** per-
interface rx/tx pairs, rewritten with `indent=2` on **every device, every poll
cycle** (`poller._record` calls `store.update` per device). A single HTTP
request can call `store.load()` 3–4 times (`get_device` → `_credentials_for` →
`list_devices`…).

Incremental fixes, in order of payoff:

1. **Cache reads.** Keep the parsed doc + file mtime in memory; `load()`
   re-parses only when the mtime changed. One-writer-process design makes this
   safe (the flock already serializes writers).
2. **Batch the poll write.** Poll all devices, then persist once per cycle in
   a single `store.update` — N-devices → 1 rewrite instead of N.
3. **Move history out of the main doc** (e.g. `history.json` or per-device
   files, `separators=(",", ":")`). Device records shrink to a few hundred
   bytes; auth/session reads stop paying for chart data.
4. Longer term, if you want more than 2 h of history: SQLite is one file, no
   server, and would remove the locking code entirely.

### 2.5 Parallelize the poll loop
`poll_once()` polls sequentially (`backend/poller.py:42-50`). With
`POLL_TIMEOUT=10`, five unreachable devices already eat 50 s of the 60 s
interval, and each slow device delays every device behind it.
`list_clients` already has the right pattern
(`ThreadPoolExecutor(max_workers=min(8, len(devs)))`,
`backend/devices.py:907`) — reuse it here. Combined with 2.4(2), the cycle
becomes: map reads in parallel → one batched write.

### 2.6 Deduplicate the two HTTP transports
`HTTPConnection` and `HTTPWebConnection` (`backend/transports.py`) share the
URL assembly, `request()`, `get()`, `info()`, `close()`, the
scheme-in-host parsing and the urllib3-warning suppression — ~80 duplicated
lines. Extract a `_BaseHTTPConnection` and keep only auth wiring
(`_build_session` vs. login helpers) in the subclasses.

### 2.7 Smaller items
- `logbuf`, `dashboards`, `detect`, `crypto`, `store` are all tight and
  right-sized — no action.
- `auth.login` (`backend/auth.py:117-129`) runs scrypt only when the username
  exists — a measurable timing oracle for username enumeration. Verify against
  a dummy hash on miss.
- Expired sessions are only pruned when *that token* is presented
  (`user_for_token`); the sessions map otherwise grows forever. Sweep expired
  tokens opportunistically inside an existing `store.update` (e.g. on login).
- `_read_json` (`backend/app.py:161-168`) silently coerces malformed JSON to
  `{}`; a 400 would make client bugs visible instead of turning them into
  "field required" errors.
- `_send_json`'s `extra_headers` iterates `(extra_headers or {})` — it accepts
  a list of tuples but the `{}` default reads like a dict is fine; make the
  contract explicit (`extra_headers: list[tuple] | None`).

---

## 3. Frontend refactoring (`web/app.js`, 3,731 lines)

### 3.1 Split into ES modules — no build step required
One file is at the size where find-and-scroll dominates editing. Native
modules (`<script type="module" src="/app.js">`) work in every browser the app
already requires (it uses `?.`-era syntax, canvas, service workers). A natural
split mirroring the section comments already in the file:

```
web/js/api.js        fetch wrapper, timeAgo, fmt* helpers
web/js/ui.js         toast, dialogs (prompt/confirm/pick), modal helper, iconBtn, icons
web/js/theme.js      theme cycling
web/js/charts.js     makeChart, paintChart, donut*, toRate, cssVar
web/js/devices.js    device list, cards, dnd, dashboards
web/js/detail.js     device modal, sections, customize
web/js/clients.js    Access tab, NAC banner/setup, client edit modal
web/js/wizard.js     add-device wizard, TRANSPORTS/PRESETS
web/js/users.js, logs.js, settings.js
web/app.js           boot + tab routing, imports the rest
```

The modules also kill the current implicit global state sprawl (`DM`, `WIZ`,
`CLIENTS`, `ALL_DEVICES`, `CHART_REG`… all top-level `let`s).

### 3.2 Extract the busy-button helper
This exact sequence appears ~15 times (`wiz-detect`, `wiz-choose`, `wiz-save`,
`approveClient`, `ignoreClient`, `toggleRule`, `addBtn`, `bindingSection`,
`clients-refresh`, `cz-save`, action buttons…):

```js
btn.disabled = true; btn.textContent = "Working…";
try { … } catch (ex) { toastErr(ex.message); }
finally { btn.disabled = false; btn.textContent = orig; }
```

One helper removes ~100 lines and makes the behavior uniform (some sites
currently forget the spinner class, some forget to restore on error):

```js
async function withBusy(btn, busyLabel, fn) {
  const orig = btn.textContent;
  btn.disabled = true; if (busyLabel) btn.textContent = busyLabel;
  btn.classList.add("spinning");
  try { return await fn(); }
  finally { btn.disabled = false; btn.textContent = orig; btn.classList.remove("spinning"); }
}
```

### 3.3 One modal helper instead of four hand-wired copies
`openSeriesChart` (`web/app.js:2542`) and `openPieModal` (`web/app.js:2590`)
build byte-identical overlay markup and each re-implement backdrop-click +
Escape + `body.overflow` handling; the device modal and client modal wire the
same three listeners again at top level. Extract
`openOverlay({title}) → {body, close}` and a shared `bindModalClose(el, fn)`.
This is also the natural place to add the focus-trap from §4.4.

### 3.4 Stop duplicating server knowledge
- **`DRIVER_NAMES`** (`web/app.js:1344-1353`) hardcodes the display names the
  server already owns (`registry` drivers have `display_name`; `/api/drivers`
  returns them). Every new driver currently needs a matching frontend edit.
  Fetch the catalogue once at boot, build the id→name map from it, keep the
  humanize fallback.
- **`PCT_KEY` / `RATE_KEY_RE` / `DETAIL_KEYS`** infer units and chart type
  from key-name regexes. Drivers already declare `unit` on entities — extend
  `Entity.describe()` with e.g. `"display": "percent"|"rate"|"identity"` and
  let the card/detail formatting read metadata instead of guessing from names.
  The regexes can stay as fallback for old data.

### 3.5 Render errors through one helper
Most of the code fills templates via `textContent` (good), but there are a
handful of `innerHTML` interpolations of dynamic strings:
`web/app.js:416` and `:1482` (`ex.message`), `:3007` (interface `name`),
`:2584`, `:1318`. See §5.1 for why this matters; a
`renderError(el, prefix, msg)` that uses `textContent` fixes all of them and
prevents regressions.

### 3.6 Timer hygiene
Three hand-rolled interval managers (`devicesTimer`, `logsTimer`,
`LIVE_TIMER`) with slightly different stop conditions. A tiny
`visiblePoll(panelName, fn, ms)` that auto-stops when the panel hides — and
pauses on `document.visibilityState === "hidden"` — would unify them and stop
background-tab polling (the Logs tab at 3 s is the main offender).

---

## 4. UI / UX improvements

### 4.1 Add hash routing — the back button is the #1 PWA gap
There is no URL state at all: tabs, the device modal, and the client modal are
invisible to the browser. On an installed PWA (the project's headline use
case) the Android back gesture / hardware back **exits the app** instead of
closing the modal or returning to the Devices tab, and no view is linkable or
survives a refresh. Minimal fix, no router library needed:

- `#/devices`, `#/access`, `#/add`, `#/settings`… on `switchTab`;
- `#/device/<id>` when the detail modal opens;
- one `hashchange` listener that opens/closes accordingly.

This also makes "share a link to this device's detail view" work for free.

### 4.2 Stop rebuilding lists under the user
- The Devices grid re-renders wholesale every 15 s. Mid-interaction this can
  yank a card out from under a tap (drag is already guarded, taps aren't),
  and it discards text selection. Client cards are worse: the 60-second-ish
  refresh **collapses any expanded card** (`renderClients` rebuilds the grid).
  Key the DOM by `device.id`/`mac` and patch in place (the codebase already
  does exactly this pattern well inside the detail modal via `CHART_REG` /
  `LIVE_CELLS` — extend the same idea to the card grids).
- "updated 42s ago" only changes when a poll response arrives. A single 30 s
  ticker that re-renders the `.updated`/"First seen" labels keeps them honest.

### 4.3 Charts: add a time axis and a range
The history charts have no x-axis labels — you can't tell if a spike was 2
minutes or 2 hours ago (the tooltip helps but requires hover, which is awkward
on touch). Cheap wins: first/last timestamps in the `c-foot` row, and a
hover-line label. Larger win: since `HISTORY_MAX` is only ~2 h, add a range
selector (2 h / 12 h / 24 h) backed by a longer, downsampled server-side
history (ties into §2.4's history storage change).

### 4.4 Accessibility pass
- **Focus management:** modals don't trap focus and don't restore it on close;
  after closing the device modal, focus is lost to `<body>`. The shared modal
  helper (§3.3) is the place to fix both.
- **`outline: none` on focus** (`web/styles.css:66,279,519,543,617,667`)
  replaces the ring with a border-color change — fine for pointer users, but
  add `:focus-visible` rings so keyboard users don't lose their place. There
  is currently no `:focus-visible` styling at all.
- **Tabs** are plain buttons; add `role="tablist"` / `role="tab"` /
  `aria-selected` and arrow-key navigation, and `aria-controls` to panels.
- **Status dots** communicate up/down purely by color (`.dot.up/.down`).
  There's a `title`, but add a visually-hidden text or distinct shape for
  color-blind users (the pill on client cards does this right).
- **`prefers-reduced-motion`** isn't honored — the toast/card animations
  should be wrapped in a media query.

### 4.5 Smaller polish items
- **Login:** no pending state on the submit button (double-submit is
  possible), and no show-password toggle. Both are 5-line fixes.
- **Loading states:** the device modal and clients view show plain "Loading…"
  text; skeleton cards would reduce perceived latency, especially on the
  detail modal which does a full live read of the device (often 1–3 s).
- **Reordering on touch:** card order relies on HTML5 drag-and-drop, which
  doesn't exist on mobile — the primary platform. Either add a pointer-events
  fallback or a "move up/down" affordance in the card's ⋯ actions.
- **Wizard step indicator** (`#wiz-steps`) looks clickable but isn't; make
  completed steps navigate back (state is already retained).
- **Theme button** cycles silently through three states with only a toast as
  feedback; a three-option menu (Auto / Dark / Light) is more discoverable.
- **`/api/logs` view** re-renders the whole list every 3 s with auto-refresh
  on, resetting scroll — prepend new entries instead, or pin scroll unless at
  top.
- **Empty search results** in Devices/Access are handled nicely — keep that
  pattern for the Logs filter too (currently just an empty area +
  `#logs-empty`).

---

## 5. Security hardening

The baseline is genuinely good (scrypt, Fernet-encrypted credentials, HttpOnly
SameSite cookies, path-traversal-safe static serving, login throttling,
per-user device ownership). Remaining items, roughly by severity:

1. **Device-supplied strings can reach `innerHTML`.** A monitored device is
   only semi-trusted — a compromised AP/switch controls its interface names,
   hostnames, and error text. `showIfChart` interpolates the interface name
   into HTML (`web/app.js:3007`), and several error paths interpolate
   `ex.message`, which echoes driver/device-derived text
   (`web/app.js:416,1318,1482,2584`). That's a stored-XSS path from LAN
   devices into an authenticated admin session. Route all of these through
   `textContent` (§3.5). Consider a strict CSP (`default-src 'self'`) as a
   backstop — the app is fully self-contained, so this is nearly free.
2. **SSH host keys are auto-accepted** (`AutoAddPolicy`,
   `backend/transports.py:59`) on every connection, permanently — a MITM on
   the LAN can capture device credentials. Do TOFU properly: record the host
   key on first add (the wizard is the natural place), verify thereafter, and
   surface a "host key changed" error.
3. **`X-Real-IP` is trusted unconditionally** (`backend/app.py:120`). When the
   container is exposed directly (the documented single-container deploy),
   an attacker defeats the login throttle by rotating the header, and log
   entries record spoofed IPs. Only honor it behind an explicit
   `HLHQ_TRUST_PROXY=1`.
4. **Session tokens are stored in plaintext** in the JSON doc; anyone with a
   backup of `/data` can replay a 30-day session. Store `sha256(token)` and
   compare hashes — one-line change on each side.
5. **CSRF** currently rests on `SameSite=Lax` alone, which does cover the
   JSON POSTs, but an `Origin`/`Sec-Fetch-Site` check on state-changing
   methods is cheap defense-in-depth for older browsers.
6. **Username enumeration via timing** in `auth.login` — see §2.7.

---

## 6. Suggested order of attack

| Priority | Item | Size |
|---|---|---|
| 1 | Bugs §1.1–1.4 | ~1 hr total |
| 2 | XSS hardening §5.1 + error-render helper §3.5 | small |
| 3 | `app.py` route table + error mapper (§2.1) | medium, high leverage |
| 4 | Store read-cache + batched poll write (§2.4.1–2) | small–medium, biggest perf win |
| 5 | Parallel polling (§2.5) | small |
| 6 | Hash routing / back button (§4.1) | small–medium, biggest UX win |
| 7 | Keyed list updates + ticking timestamps (§4.2) | medium |
| 8 | Split `devices.py` (§2.2) + `device_conn` (§2.3) | medium, mechanical |
| 9 | Split `app.js` into modules (§3.1) + helpers (§3.2–3.3) | medium, mechanical |
| 10 | Accessibility pass (§4.4), SSH TOFU (§5.2), history storage (§2.4.3) | ongoing |

Items 3, 8 and 9 are pure refactors — land them in separate commits from any
behavior change so the `_verify/` harness diffs stay reviewable.
