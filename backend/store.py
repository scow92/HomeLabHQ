"""Atomic, flock-guarded JSON document store.

Lifted from the Network-Manager NAC (server.py load_db/save_db) and generalized
into a small keyed store so every part of the app shares one durable file with
consistent locking. No DB engine yet — a single JSON document under /data, with
a shared/exclusive advisory lock so concurrent request threads and the poller
never tear each other's writes.
"""
import copy
import json
import os
import fcntl
import tempfile
import threading

DATA_DIR = os.environ.get("HLHQ_DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "homelabhq.json")
LOCK_FILE = os.path.join(DATA_DIR, "homelabhq.lock")

# Raw key material (instance secret, TLS key, VAPID key) lives here, apart
# from the JSON store, so it's a single directory an operator can lock down
# (restrictive mount, backup exclusion, deny-listed from tooling) independent
# of the rest of the app data.
SECRETS_DIR = os.path.join(DATA_DIR, "secrets")


def ensure_secrets_dir():
    os.makedirs(SECRETS_DIR, exist_ok=True, mode=0o700)
    os.chmod(SECRETS_DIR, 0o700)
    return SECRETS_DIR


def secrets_isolated_from_agents() -> bool:
    """True when an OS privilege boundary — not just file permissions —
    stands between SECRETS_DIR and other processes on this host. Root is the
    only case that qualifies: the Docker deployment runs the app as root
    inside the container, so a non-root host process (an AI coding agent
    included) can't read the volume no matter what it tries. A non-root run
    (e.g. local/dev mode) shares its UID with everything else on the host,
    including any agent — file permissions alone can't tell them apart."""
    return hasattr(os, "getuid") and os.getuid() == 0

# Process-local lock: fcntl gives us cross-process safety, this makes the
# read-modify-write in update() atomic across threads in *this* process too.
# load()'s in-memory cache (below) is guarded by the same lock.
_local = threading.RLock()

_DEFAULT_DOC = {
    "users": {},        # id -> user record
    "sessions": {},     # sha256(token) -> session record (auth._token_hash)
    "devices": {},      # id -> device record  (populated in later milestones)
    "dashboards": {},   # id -> dashboard record (named group of devices)
    "credentials": {},  # id -> encrypted credential blob
    "push_subs": {},    # endpoint -> web-push subscription record
    "meta": {},         # instance-level settings
    "sshHostKeys": {},  # "host:port" -> {keyType, fingerprint} (TOFU pinning)
    "clientRosters": {},  # owner id -> MAC -> persistent Access roster record
}

# In-memory cache of the last doc this process read or wrote, keyed by the
# data file's mtime. This process is the store's only writer (update() holds
# _local + an exclusive flock for every write and refreshes the cache right
# after), so a request handler calling load() several times in a row — as
# get_device()/_credentials_for()/list_devices() often do — reparses the JSON
# doc only once instead of once per call. The mtime check is a defensive
# fallback in case the file ever changes out from under this process.
_cache = {"doc": None, "mtime": None}


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


class StoreError(RuntimeError):
    """A persistence failure that must never be treated as an empty store."""


def _validate_doc(doc):
    if not isinstance(doc, dict):
        raise StoreError("store document must be a JSON object")
    for key, default in _DEFAULT_DOC.items():
        value = doc.get(key)
        if value is None:
            doc[key] = copy.deepcopy(default)
        elif not isinstance(value, type(default)):
            raise StoreError(f"store field {key!r} has an invalid type")
    return doc


def _read_doc():
    """Read + parse the doc file from disk (caller holds the flock), filling
    in any missing top-level keys."""
    if not os.path.exists(DB_FILE):
        return _validate_doc({})
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return _validate_doc(json.load(f))
    except StoreError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        # An existing store is data, not an invitation to silently bootstrap a
        # replacement. Keep this error concise: the document can contain
        # encrypted credentials.
        raise StoreError("cannot read the existing store safely") from exc


def _file_mtime():
    try:
        return os.path.getmtime(DB_FILE)
    except OSError:
        return None


def load():
    """Return the whole document, filling in any missing top-level keys.

    Served from the in-memory cache when the file's mtime hasn't changed since
    it was last read/written by this process; callers get a fresh deep copy
    each time so they can't mutate each other's (or the cache's) state."""
    _ensure_dir()
    with _local:
        mtime = _file_mtime()
        if _cache["doc"] is not None and _cache["mtime"] == mtime:
            return copy.deepcopy(_cache["doc"])
        with open(LOCK_FILE, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                doc = _read_doc()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        _cache["doc"], _cache["mtime"] = doc, mtime
        return copy.deepcopy(doc)


def _write_locked(doc):
    _validate_doc(doc)
    data = json.dumps(doc, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix="homelabhq_")
    closed = False
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        closed = True
        # Keep the last validated document before replacing it.
        if os.path.exists(DB_FILE):
            backup = DB_FILE + ".bak"
            with open(DB_FILE, "rb") as source, open(backup + ".tmp", "wb") as dest:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(backup + ".tmp", backup)
        os.replace(tmp_path, DB_FILE)
        dir_fd = os.open(DATA_DIR, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if not closed:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def save(doc):
    """Overwrite the whole document atomically under an exclusive lock."""
    _ensure_dir()
    with _local, open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            # Refuse to overwrite an existing invalid/unreadable document even
            # through the full-document save() API.
            if os.path.exists(DB_FILE):
                _read_doc()
            _write_locked(doc)
            _cache["doc"], _cache["mtime"] = doc, _file_mtime()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def ssh_host_key(host, port):
    """Return the pinned {"keyType", "fingerprint"} for host:port, or None if
    we've never connected there before (first connect pins it — see
    transports.SSHConnection)."""
    return load()["sshHostKeys"].get(f"{host}:{port}")


def pin_ssh_host_key(host, port, key_type, fingerprint):
    def _mut(doc):
        doc["sshHostKeys"][f"{host}:{port}"] = {
            "keyType": key_type, "fingerprint": fingerprint}
    update(_mut)


def update(mutator):
    """Read-modify-write the document atomically.

    `mutator(doc)` mutates the loaded doc in place; its return value (if any) is
    passed back to the caller. The whole read/modify/write runs under both the
    process-local RLock and the cross-process exclusive flock. Always reads
    fresh from disk (never the cache) since it's about to write; refreshes the
    cache afterward so the next load() doesn't reparse what this call just
    wrote.
    """
    _ensure_dir()
    with _local, open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = _read_doc()
            result = mutator(doc)
            _write_locked(doc)
            _cache["doc"], _cache["mtime"] = doc, _file_mtime()
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
