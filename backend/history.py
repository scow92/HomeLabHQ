"""Per-device history store.

Numeric entity history and per-interface rx/tx history used to live inline on
the device record in `store.py`'s single JSON doc, dominating its size and
making every unrelated write (login, session, rename) rewrite megabytes of
chart data. This module gives each device its own small, compact-JSON file
under `/data/history/<id>.json` — the poller is the only writer (one write per
device per cycle), request threads only read, so a process-local lock plus
atomic `os.replace` is all the safety this needs (mirrors `store.py`'s
tmp+replace convention, without its read cache — these files are small and
read one at a time).
"""
import fcntl
import json
import os
import tempfile
import threading
import time

import store

HIST_DIR = os.path.join(store.DATA_DIR, "history")

_local = threading.Lock()

_DEFAULT_DOC = {"history": {}, "ifHistory": {}, "historyLong": {}, "online": []}

# Long-range retention: alongside the full-resolution `history` (per-poll,
# ~2h), `historyLong` keeps one point per LONG_INTERVAL for LONG_MAX points
# (~7 days at 5 min). Counters (octets) stay correct when sampled sparsely —
# the UI derives rates from deltas — and gauges become a 5-min sample, which
# is plenty for a 24h/7d chart. Served via series(..., rng=...) for the
# chart time-range picker.
LONG_INTERVAL = 300
LONG_MAX = 2016
RANGE_WINDOWS = {"24h": 24 * 3600, "7d": 7 * 24 * 3600}

# Per-poll reachability series ([ts, 0|1]) behind the detail view's 24h
# availability strip: ~24h at the default 60s interval.
ONLINE_MAX = 1440


def _path(dev_id):
    return os.path.join(HIST_DIR, f"{dev_id}.json")


def _ensure_dir():
    os.makedirs(HIST_DIR, exist_ok=True)


def load(dev_id):
    """Return {"history": {...}, "ifHistory": {...}} for one device, or the
    empty shape on a missing/corrupt file."""
    try:
        with open(_path(dev_id)) as f:
            doc = json.load(f)
    except Exception:
        return json.loads(json.dumps(_DEFAULT_DOC))
    doc.setdefault("history", {})
    doc.setdefault("ifHistory", {})
    doc.setdefault("historyLong", {})
    doc.setdefault("online", [])
    return doc


def _write_locked(dev_id, doc):
    _ensure_dir()
    data = json.dumps(doc, separators=(",", ":")).encode()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=HIST_DIR, prefix="hist_")
    try:
        os.write(tmp_fd, data)
        os.close(tmp_fd)
        os.replace(tmp_path, _path(dev_id))
    except Exception:
        os.close(tmp_fd)
        os.unlink(tmp_path)
        raise


def save(dev_id, doc):
    """Overwrite one device's history file atomically."""
    _ensure_dir()
    with _local, open(store.LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            _write_locked(dev_id, doc)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def update(dev_id, mutator):
    """Read-modify-write one device's history file atomically.

    `mutator(doc)` mutates the loaded `{"history", "ifHistory"}` doc in
    place; its return value (if any) is passed back to the caller. Shares the
    same cross-process flock `store.py` uses so a second process pointed at
    the same /data dir can't tear a write either.
    """
    with _local, open(store.LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = load(dev_id)
            result = mutator(doc)
            _write_locked(dev_id, doc)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def delete(dev_id):
    """Remove a device's history file. A no-op if it never existed."""
    try:
        os.unlink(_path(dev_id))
    except FileNotFoundError:
        pass


def series(dev_id, key, rng=None):
    """Convenience for the /history endpoint: one entity's stored series.
    `rng` of "24h"/"7d" reads the downsampled long series windowed to that
    range; anything else returns the full-resolution recent series."""
    doc = load(dev_id)
    window = RANGE_WINDOWS.get(rng or "")
    if window:
        cutoff = time.time() - window
        return [p for p in doc.get("historyLong", {}).get(key, [])
                if p[0] >= cutoff]
    return doc.get("history", {}).get(key, [])


def migrate_from_store():
    """One-time, idempotent migration of legacy embedded history.

    Older device records carried `history`/`ifHistory` inline. On startup,
    move any device still carrying those keys into its own history file and
    strip them from the main doc. Safe to call on every boot: devices already
    migrated have neither key and are skipped. If a history file already
    exists for a device (e.g. the poller already wrote one on a previous,
    interrupted migration), keep it and just discard the redundant embedded
    copy rather than overwriting newer data.
    """
    # Collect what needs saving while the store's exclusive lock is held, but
    # don't call save() from inside the mutator: it takes the same cross-process
    # flock (store.LOCK_FILE) store.update() is already holding, and flock
    # doesn't nest across file descriptors even within one process — that
    # would deadlock. Write the files after store.update() releases the lock.
    to_save = {}

    def _mut(doc):
        moved = []
        for dev_id, dev in doc["devices"].items():
            legacy_hist = dev.pop("history", None)
            legacy_ifh = dev.pop("ifHistory", None)
            if legacy_hist is None and legacy_ifh is None:
                continue
            if not os.path.exists(_path(dev_id)):
                to_save[dev_id] = {"history": legacy_hist or {},
                                   "ifHistory": legacy_ifh or {}}
            moved.append(dev_id)
        return moved

    moved = store.update(_mut)
    for dev_id, doc in to_save.items():
        save(dev_id, doc)
    return moved
