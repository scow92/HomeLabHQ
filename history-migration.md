# Plan: move history storage out of the main JSON doc (REVIEW.md §2.4.3)

> **Status: ✅ Implemented.** `backend/history.py` exists with the per-device
> file design described below, the migration ran, and call sites (poller,
> `read_detail`, `/api/devices/{id}/history`, `delete_device`) were updated.
> `HISTORY_MAX` is still 120 points (~2h) — the retention bump and the
> chart time-range picker this unblocks (§8 / refactor.md §3.1) have not
> been built yet. This document is kept as a historical design record.

## 1. Problem

Everything lives in one JSON document (`/data/homelabhq.json`, `store.py`).
History dominates its size and its churn:

- **Per-entity history** — `dev["history"][key] = [[ts, value], …]`, capped at
  `HISTORY_MAX = 120` points per numeric entity (`poller._apply_record`).
- **Per-interface history** — `dev["ifHistory"][ifname] = {name, rx: […], tx: […]}`,
  two more 120-point series per interface. A 24-port switch alone contributes
  up to 48 × 120 points.
- The poller rewrites the **whole document once per cycle** (the batched write
  from §2.4.2), serialized with `indent=2`.

Consequences:

- A ~10-device network with a few switches easily makes the doc **several MB**;
  90 %+ of that is history arrays.
- Every `store.load()` cache miss reparses all of it, and every unrelated
  `store.update()` (login, session write, rename, NAC edit) **re-serializes and
  rewrites megabytes** to change a few hundred bytes.
- Auth/session reads pay for chart data they never touch.
- Retention can't grow: 120 points ≈ 2 h at the 60 s interval. Anything longer
  multiplies the main doc further, so history depth is currently hostage to
  the storage design.

The read cache (§2.4.1) and batched poll write (§2.4.2) already landed; this is
the third step.

## 2. Goals / non-goals

**Goals**

1. Device records in the main doc shrink to configuration + latest state
   (a few hundred bytes each); the main doc stops growing with time.
2. History writes stop rewriting unrelated data; unrelated writes stop
   rewriting history.
3. No API contract changes: `/api/devices/<id>/history`,
   `/api/devices/<id>/detail` (its `history` / `ifHistory` fields) and the
   frontend keep working unchanged.
4. One-time, automatic, idempotent migration on upgrade; safe rollback.
5. Leave the door open to longer retention (the actual user-visible win) and
   to SQLite later (§2.4.4) without another API change.

**Non-goals (this milestone)**

- No retention increase yet (keep `HISTORY_MAX = 120`; bumping it becomes a
  one-line change afterwards).
- No downsampling/aggregation tiers.
- No SQLite yet — see §8.

## 3. Chosen design: one compact history file per device

`/data/history/<device_id>.json`, written with `separators=(",", ":")` (no
indent), containing exactly what today lives on the device record:

```json
{"history": {"cpu": [[1750000000, 12.5], …]},
 "ifHistory": {"eth0": {"name": "WAN", "rx": [[…]], "tx": [[…]]}}}
```

Why per-device files rather than a single `history.json`:

- The poller writes results **per device**; a shared file would recreate the
  same rewrite-everything problem one level down.
- A torn/corrupt file loses one device's 2 h chart, not everything.
- `delete_device` becomes `os.unlink`, and per-device migration/rollback is
  trivial.
- Detail reads (`read_detail`) load one small file, not all history.

Why JSON-per-device rather than append-only logs (JSONL/CSV): the series are
ring buffers (trim to last 120), so append-only needs compaction machinery;
rewriting a ~50–200 KB file per device per cycle is cheap and keeps atomic
`os.replace` semantics we already trust in `store._write_locked`.

## 4. New module: `backend/history.py`

Mirrors `store.py`'s conventions (atomic tmp+`os.replace` writes, defensive
reads), but simpler locking:

```python
HIST_DIR = os.path.join(store.DATA_DIR, "history")

def load(dev_id) -> dict          # {"history": {}, "ifHistory": {}} on miss/corruption
def save(dev_id, doc) -> None     # atomic compact write
def update(dev_id, mutator)       # read-modify-write under the per-process lock
def delete(dev_id) -> None        # unlink, ignore missing
def series(dev_id, key) -> list   # convenience for the /history endpoint
```

**Locking story:** the poller thread is the only writer (one write per device
per cycle); request threads only read. A single process-local `threading.Lock`
plus atomic `os.replace` is sufficient — readers can never observe a torn
file. Keep a **shared flock on the existing `homelabhq.lock`** around writes
only if we want to stay robust against a second process being pointed at the
same `/data` (cheap; recommended for parity with `store.py`). No per-file
flocks needed.

**Caching:** none initially. Files are small and read only by the detail
view/history endpoints (one device at a time, every 20 s while a modal is
open). If profiling ever says otherwise, add the same mtime-keyed cache
`store.load()` uses.

## 5. Call-site changes

| Site | Today | After |
| --- | --- | --- |
| `poller._apply_record` | mutates `dev["history"]` / `dev["ifHistory"]` inside the doc mutator | pure: **returns** `{key: (ts, value)}` samples + interface counters; no history keys on the device record |
| `poller._record_all` | one `store.update` for everything | unchanged `store.update` for state/alerts; then, outside the store lock, `history.update(dev_id, …)` per polled device appending samples and trimming to `HISTORY_MAX` |
| `devices.read_detail` | `dev.get("history")` / `dev.get("ifHistory")` | `h = history.load(dev_id)`; response shape identical |
| `app.py /api/devices/<id>/history` | reads `dev["history"][key]` | `history.series(dev_id, key)` |
| `devices.delete_device` | doc-only | also `history.delete(dev_id)` |
| `devices._public` | already excludes history | unchanged (verify nothing else round-trips the raw record: `dashboards`, `push`, `_verify/*`) |

Ordering per cycle: state write first (UI freshness), then history appends.
Both are idempotent-ish; a crash between them costs at most one chart point.

## 6. Migration & rollback

On startup (`store` bootstrap or `app.main()` before `poller.start()`):

1. Load the doc; for every device carrying `history`/`ifHistory` keys, write
   `history/<id>.json`, then strip those keys from the record.
2. Persist the stripped doc in one `store.update`.
3. Idempotent: devices without the keys are skipped; re-running is a no-op.
   Don't overwrite an existing newer history file (poller may have written
   already on a previous partial migration) — merge by preferring the file.

**Rollback:** downgrading the code simply loses chart history (2 h of data)
— device config, users, NAC state are untouched. Acceptable; document it in
the release notes. No reverse migration needed.

## 7. Related data with the same growth shape (follow-ups, same pattern)

- **Client roster events** (`meta.nacClients[*].events`, added with the
  Access online/offline feature): bounded at 50/client, so it's fine in the
  main doc for now, but it's the next candidate for `history/clients.json`
  if per-client history depth is ever increased.
- **`sshHostKeys` / `push_subs`**: small, stay put.

## 8. Phase 2 (optional, later): SQLite

If retention beyond a few hours is wanted (the natural next ask once the UI
grows a time-range picker), move `history.py`'s internals to a single
`history.db` (`WAL` mode, table `samples(dev_id, key, ts, value)` +
`if_samples`), keeping the module API identical so no call site changes
again. That removes trimming logic (a `DELETE … WHERE ts <` sweep), makes
range queries cheap, and drops file-per-device management. The module
boundary introduced in this milestone is what makes that swap safe.

## 9. Testing & verification

- New `_verify/history_store_test.py`: poll → samples land in
  `history/<id>.json` and NOT in the main doc; trim at `HISTORY_MAX`;
  `read_detail`/`/history` shapes unchanged; `delete_device` unlinks;
  migration moves legacy keys and is idempotent.
- Re-run `m4_test` (poller/notification paths) and `plumbing_test`.
- Manual: upgrade a data dir carrying real history → charts still render,
  doc size drops; watch one poll cycle's write pattern (`strace`/mtime) to
  confirm the main doc no longer rewrites megabytes per cycle.

## 10. Effort & order

Small-to-medium, mostly mechanical: `history.py` (~80 lines) + poller/read
call sites + migration (~40 lines) + tests. Land as a single behavior-neutral
PR, separate from any retention bump, so the `_verify` diffs stay reviewable
(matches REVIEW.md §7's sequencing note).
