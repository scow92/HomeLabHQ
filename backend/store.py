"""Atomic, flock-guarded JSON document store.

Lifted from the Network-Manager NAC (server.py load_db/save_db) and generalized
into a small keyed store so every part of the app shares one durable file with
consistent locking. No DB engine yet — a single JSON document under /data, with
a shared/exclusive advisory lock so concurrent request threads and the poller
never tear each other's writes.
"""
import copy
import fcntl
import json
import os
import tempfile
import threading
import time

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
    stands between SECRETS_DIR and other processes on this host. A container
    qualifies regardless of its in-container UID; the production image runs a
    dedicated unprivileged account. A non-container local run shares its UID
    with other local processes, so file permissions alone cannot distinguish
    them."""
    return (os.path.exists("/.dockerenv") or os.environ.get("container") is not None
            or (hasattr(os, "getuid") and os.getuid() == 0))

# Process-local lock: fcntl gives us cross-process safety, this makes the
# read-modify-write in update() atomic across threads in *this* process too.
# load()'s in-memory cache (below) is guarded by the same lock.
_local = threading.RLock()

_DEFAULT_DOC = {
    "schemaVersion": 1,
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

# A deliberately small migration chain.  Keep migrations in this module so a
# backup can always be understood by the same code that writes it.  New schema
# changes must add one ``n -> n + 1`` function below instead of changing old
# documents opportunistically in feature modules.
SCHEMA_VERSION = 1

# These are capacity guardrails, not quotas.  They can be tightened by an
# operator without a code change, and prune least-recently-used records only
# when a new record is written.
MAX_SSH_HOST_KEYS = max(1, int(os.environ.get("HLHQ_MAX_SSH_HOST_KEYS", "1024")))

# Write observations are intentionally process-local: they make a slow or
# unexpectedly large JSON store diagnosable without putting monitoring data
# back into the document (which would cause another write).  Values are safe
# to expose to a future metrics endpoint.
_metrics = {
    "writes": 0,
    "no_op_updates": 0,
    "last_write_duration_ms": 0.0,
    "last_document_bytes": 0,
    "last_write_at": None,
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


def _migrate_v0_to_v1(doc):
    """Introduce the explicit top-level schema version.

    Pre-versioned HomelabHQ documents already use the same field layout, so
    this migration is intentionally data-preserving.  Missing optional maps
    retain the old store behaviour of becoming empty maps.
    """
    for key, default in _DEFAULT_DOC.items():
        if key != "schemaVersion":
            doc.setdefault(key, copy.deepcopy(default))
    doc["schemaVersion"] = 1


_MIGRATIONS = {0: _migrate_v0_to_v1}


def _validate_doc(doc, *, fill_missing=True):
    if not isinstance(doc, dict):
        raise StoreError("store document must be a JSON object")
    for key, default in _DEFAULT_DOC.items():
        value = doc.get(key)
        if value is None:
            if fill_missing:
                doc[key] = copy.deepcopy(default)
            else:
                raise StoreError(f"store field {key!r} is required")
        elif type(value) is not type(default):
            raise StoreError(f"store field {key!r} has an invalid type")
    return doc


def _migrate_doc(doc):
    """Validate and migrate a parsed document to ``SCHEMA_VERSION``.

    The returned ``changed`` flag lets startup persist a migration exactly
    once.  A newer document is rejected rather than silently downgraded.
    """
    if not isinstance(doc, dict):
        raise StoreError("store document must be a JSON object")
    version = doc.get("schemaVersion", 0)
    if type(version) is not int or version < 0:
        raise StoreError("store schema version is invalid")
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"store schema version {version} is newer than this HomelabHQ version")
    changed = False
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise StoreError(f"no migration from store schema version {version}")
        migration(doc)
        version += 1
        changed = True
    before = copy.deepcopy(doc)
    _validate_doc(doc)
    return doc, changed or doc != before


def _read_doc():
    """Read + parse the doc file from disk (caller holds the flock), filling
    in any missing top-level keys."""
    if not os.path.exists(DB_FILE):
        return _migrate_doc({})
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return _migrate_doc(json.load(f))
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
                doc, _ = _read_doc()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        _cache["doc"], _cache["mtime"] = doc, mtime
        return copy.deepcopy(doc)


def _write_locked(doc):
    _validate_doc(doc)
    data = json.dumps(doc, indent=2).encode("utf-8")
    started = time.monotonic()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix="homelabhq_")
    tmp_open = True
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            tmp_open = False  # the file object now owns (and closes) the fd
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
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
        _metrics.update({
            "writes": _metrics["writes"] + 1,
            "last_write_duration_ms": round((time.monotonic() - started) * 1000, 3),
            "last_document_bytes": len(data),
            "last_write_at": int(time.time()),
        })
    except Exception:
        if tmp_open:
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
            _cache["doc"], _cache["mtime"] = copy.deepcopy(doc), _file_mtime()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def ssh_host_key(host, port):
    """Return the pinned {"keyType", "fingerprint"} for host:port, or None if
    we've never connected there before (first connect pins it — see
    transports.SSHConnection)."""
    return load()["sshHostKeys"].get(f"{host}:{port}")


def pin_ssh_host_key(host, port, key_type, fingerprint):
    def _mut(doc):
        keys = doc["sshHostKeys"]
        keys[f"{host}:{port}"] = {
            "keyType": key_type, "fingerprint": fingerprint,
            "storedAt": int(time.time()),
        }
        _prune_oldest(keys, MAX_SSH_HOST_KEYS)
    update(_mut)


def _prune_oldest(records, limit, *, timestamp="storedAt"):
    """Bound a mapping in-place, retaining its most recently used records."""
    overflow = len(records) - limit
    if overflow <= 0:
        return 0
    oldest = sorted(records, key=lambda key: (
        records[key].get(timestamp, 0) if isinstance(records[key], dict) else 0, key))[:overflow]
    for key in oldest:
        records.pop(key, None)
    return len(oldest)


def batch_update(mutator):
    """Atomically mutate any related records in one JSON-document commit.

    This is an explicit name for callers that update several collections (for
    example a device and its credential).  It has the same contract as
    ``update`` and is intentionally not a separate transaction mechanism.
    """
    return update(mutator)


def metrics():
    """Return safe store-write observations for diagnostics and health checks."""
    with _local:
        return dict(_metrics)


def startup_integrity_check():
    """Validate the on-disk document and persist any pending schema migration.

    This runs before the HTTP server accepts traffic.  It deliberately raises
    ``StoreError`` on an unreadable or invalid existing store so an operator
    can restore ``homelabhq.json.bak`` (or a backup) without data loss.
    """
    _ensure_dir()
    with _local, open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc, migrated = _read_doc()
            if migrated:
                _write_locked(doc)
            _cache["doc"], _cache["mtime"] = copy.deepcopy(doc), _file_mtime()
            document_bytes = len(json.dumps(doc, indent=2).encode("utf-8"))
            _metrics["last_document_bytes"] = document_bytes
            return {
                "schemaVersion": doc["schemaVersion"],
                "migrated": migrated,
                "documentBytes": document_bytes,
            }
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


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
            doc, migrated = _read_doc()
            before = copy.deepcopy(doc)
            result = mutator(doc)
            _validate_doc(doc)
            if migrated or doc != before:
                _write_locked(doc)
            else:
                _metrics["no_op_updates"] += 1
            _cache["doc"], _cache["mtime"] = copy.deepcopy(doc), _file_mtime()
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
