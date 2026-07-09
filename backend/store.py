"""Atomic, flock-guarded JSON document store.

Lifted from the Network-Manager NAC (server.py load_db/save_db) and generalized
into a small keyed store so every part of the app shares one durable file with
consistent locking. No DB engine yet — a single JSON document under /data, with
a shared/exclusive advisory lock so concurrent request threads and the poller
never tear each other's writes.
"""
import json
import os
import fcntl
import tempfile
import threading

DATA_DIR = os.environ.get("NM_DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "netmanager.json")
LOCK_FILE = os.path.join(DATA_DIR, "netmanager.lock")

# Process-local lock: fcntl gives us cross-process safety, this makes the
# read-modify-write in update() atomic across threads in *this* process too.
_local = threading.RLock()

_DEFAULT_DOC = {
    "users": {},        # id -> user record
    "sessions": {},     # token -> session record
    "devices": {},      # id -> device record  (populated in later milestones)
    "credentials": {},  # id -> encrypted credential blob
    "meta": {},         # instance-level settings
}


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load():
    """Return the whole document, filling in any missing top-level keys."""
    _ensure_dir()
    with open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)
        try:
            if os.path.exists(DB_FILE):
                with open(DB_FILE) as f:
                    doc = json.load(f)
            else:
                doc = {}
        except Exception:
            doc = {}
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    for k, v in _DEFAULT_DOC.items():
        doc.setdefault(k, json.loads(json.dumps(v)))
    return doc


def _write_locked(doc):
    data = json.dumps(doc, indent=2).encode()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix="netmanager_")
    try:
        os.write(tmp_fd, data)
        os.close(tmp_fd)
        os.replace(tmp_path, DB_FILE)
    except Exception:
        os.close(tmp_fd)
        os.unlink(tmp_path)
        raise


def save(doc):
    """Overwrite the whole document atomically under an exclusive lock."""
    _ensure_dir()
    with _local, open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            _write_locked(doc)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def update(mutator):
    """Read-modify-write the document atomically.

    `mutator(doc)` mutates the loaded doc in place; its return value (if any) is
    passed back to the caller. The whole read/modify/write runs under both the
    process-local RLock and the cross-process exclusive flock.
    """
    _ensure_dir()
    with _local, open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if os.path.exists(DB_FILE):
                with open(DB_FILE) as f:
                    doc = json.load(f)
            else:
                doc = {}
            for k, v in _DEFAULT_DOC.items():
                doc.setdefault(k, json.loads(json.dumps(v)))
            result = mutator(doc)
            _write_locked(doc)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
