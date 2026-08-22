"""Web push (VAPID) — subscriptions + delivery.

A per-instance VAPID keypair is generated on first use; the browser subscribes
with the public key, and the poller sends notifications on device state changes.
Delivery requires a secure context in the browser (HTTPS or localhost), so push
is only usable once HomelabHQ is behind TLS — the rest of the app works without
it.
"""
import base64
import copy
import fcntl
import json
import os
import secrets
import tempfile
import time
import threading

import logbuf
import store
from store import SECRETS_DIR, ensure_secrets_dir

VAPID_PRIV = os.path.join(SECRETS_DIR, "vapid_private.pem")
VAPID_PUB = os.path.join(SECRETS_DIR, "vapid_public.txt")
# VAPID 'sub' claim: a mailto:/https: URI identifying the sender. Apple's push
# service (web.push.apple.com) rejects the JWT with 403 BadJwtToken — dropping
# ALL iOS notifications — when the domain is a reserved/unresolvable pseudo-TLD
# like `.local`. The default therefore uses a real, DNS-valid domain; set
# HLHQ_VAPID_SUB to an address on a domain you control for production.
VAPID_SUB = os.environ.get("HLHQ_VAPID_SUB", "mailto:admin@example.com")
MAX_PUSH_SUBSCRIPTIONS_PER_USER = max(
    1, int(os.environ.get("HLHQ_MAX_PUSH_SUBSCRIPTIONS_PER_USER", "20")))
MAX_NOTIFICATIONS_PER_USER = max(
    20, int(os.environ.get("HLHQ_MAX_NOTIFICATIONS_PER_USER", "500")))
_metrics_lock = threading.Lock()
_vapid_lock = threading.Lock()
_metrics = {
    "attempts": 0,
    "sent": 0,
    "failures": 0,
    "lastAttemptAt": None,
    "lastFailureAt": None,
    "lastError": None,
}


def metrics():
    """Return safe, process-local push-delivery observations."""
    with _metrics_lock:
        return dict(_metrics)


def _record_delivery(result):
    now = int(time.time())
    failed = result["failed"] + result["removed"]
    with _metrics_lock:
        _metrics["attempts"] += 1
        _metrics["sent"] += result["sent"]
        _metrics["failures"] += failed
        _metrics["lastAttemptAt"] = now
        if failed:
            _metrics["lastFailureAt"] = now
            _metrics["lastError"] = logbuf.redact(result.get("error") or "subscription removed")


def _new_vapid_pair():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    return pem, pub_b64


def _read_vapid_pair():
    private_exists = os.path.lexists(VAPID_PRIV)
    public_exists = os.path.lexists(VAPID_PUB)
    if not private_exists and not public_exists:
        return None
    if private_exists != public_exists:
        raise RuntimeError("VAPID keypair is incomplete; restore both files from backup")

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        with open(VAPID_PRIV, "rb") as private_file:
            private_pem = private_file.read()
        with open(VAPID_PUB, encoding="ascii") as public_file:
            stored_public = public_file.read().strip()
        private_key = serialization.load_pem_private_key(private_pem, password=None)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise RuntimeError("VAPID keypair is invalid; restore both files from backup") from error

    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1):
        raise RuntimeError("VAPID keypair is invalid; restore both files from backup")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    derived_public = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()
    if stored_public != derived_public:
        raise RuntimeError("VAPID keypair does not match; restore both files from backup")
    for path in (VAPID_PRIV, VAPID_PUB):
        os.chmod(path, 0o600, follow_symlinks=False)
    return stored_public


def _write_vapid_candidate(prefix, data):
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=os.path.dirname(VAPID_PRIV))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as candidate_file:
            fd = -1
            candidate_file.write(data)
            candidate_file.flush()
            os.fsync(candidate_file.fileno())
        return temporary
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _publish_vapid_pair(private_pem, public_key):
    private_tmp = _write_vapid_candidate(".vapid-private.", private_pem)
    public_tmp = None
    private_published = False
    try:
        public_tmp = _write_vapid_candidate(".vapid-public.", public_key.encode("ascii"))
        # Both complete files are durable before either path becomes visible.
        # Hard links provide no-clobber publication if a non-cooperating writer
        # appears despite the process and file locks.
        os.link(private_tmp, VAPID_PRIV)
        private_published = True
        os.link(public_tmp, VAPID_PUB)
        private_published = False
        directory_fd = os.open(os.path.dirname(VAPID_PRIV), os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if private_published:
            try:
                if os.path.samefile(private_tmp, VAPID_PRIV):
                    os.unlink(VAPID_PRIV)
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary in (private_tmp, public_tmp):
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def _ensure_vapid():
    ensure_secrets_dir()
    lock_path = os.path.join(os.path.dirname(VAPID_PRIV), ".vapid.lock")
    with _vapid_lock:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
        except Exception:
            os.close(lock_fd)
            raise
        with os.fdopen(lock_fd, "a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                existing = _read_vapid_pair()
                if existing is None:
                    _publish_vapid_pair(*_new_vapid_pair())
                    _read_vapid_pair()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def public_key() -> str:
    _ensure_vapid()
    with open(VAPID_PUB) as f:
        return f.read().strip()


def subscribe(user_id, subscription):
    endpoint = (subscription or {}).get("endpoint")
    if not endpoint:
        raise ValueError("subscription.endpoint required")
    rec = {"userId": user_id, "subscription": subscription,
           "created": int(time.time())}

    def _mut(document):
        subscriptions = document["push_subs"]
        subscriptions[endpoint] = rec
        owned = [key for key, value in subscriptions.items()
                 if value.get("userId") == user_id and key != endpoint]
        overflow = len(owned) + 1 - MAX_PUSH_SUBSCRIPTIONS_PER_USER
        if overflow > 0:
            for key in sorted(owned, key=lambda key: (
                    subscriptions[key].get("created", 0), key))[:overflow]:
                subscriptions.pop(key, None)

    store.update(_mut)


def unsubscribe(user_id, endpoint):
    """Remove an endpoint only when it belongs to the requesting user."""
    if not endpoint:
        raise ValueError("subscription.endpoint required")

    def _mut(document):
        record = document["push_subs"].get(endpoint)
        if not record or record.get("userId") != user_id:
            return False
        document["push_subs"].pop(endpoint)
        return True

    return store.update(_mut)


def subscription_status(user_id):
    """Return non-secret subscription state for one authenticated user."""
    records = store.load()["push_subs"].values()
    count = sum(record.get("userId") == user_id for record in records)
    return {"subscribed": count > 0, "subscriptionCount": count}


def _remove_endpoints(endpoints):
    """Prune provider-expired subscriptions without a request actor."""
    endpoints = set(endpoints)

    def _mut(document):
        for endpoint in endpoints:
            document["push_subs"].pop(endpoint, None)

    store.update(_mut)


def recipients_for_device(dev):
    """Owner + every admin (they oversee all devices)."""
    doc = store.load()
    ids = {dev.get("ownerId")}
    for u in doc["users"].values():
        if u.get("role") == "admin":
            ids.add(u["id"])
    return {i for i in ids if i}


def _notification_category(title, body, data):
    """Retain explicit event classes and conservatively classify legacy calls."""
    data = data or {}
    explicit = str(data.get("category") or "").strip().lower()
    if explicit:
        return explicit[:80]
    event_type = str(data.get("type") or "").strip().lower()
    key = str(data.get("key") or "").strip().lower()
    text = f"{title} {body}".lower()
    if event_type == "offline":
        return "host_offline"
    if event_type == "online":
        return "host_online"
    if event_type == "alert":
        if "cpu" in key:
            return "high_cpu"
        if key in {"mem", "memory", "mem_used", "mem_used_pct"} or "memory" in key:
            return "high_memory"
        if any(word in key for word in ("storage", "disk", "pool", "capacity")):
            return "storage_warning"
        return "alert"
    if event_type in {"available_updates", "updates"} or "updates" in text:
        return "available_updates"
    if event_type == "backup_failure" or ("backup" in text and "fail" in text):
        return "backup_failure"
    return event_type[:80] or "general"


def _unread_count(records, user_id):
    return sum(record.get("userId") == user_id and not record.get("readAt") and
               not record.get("dismissedAt") for record in records.values())


def _persist_notifications(user_ids, title, body, data):
    """Create one durable notification per recipient before push is attempted."""
    now = int(time.time())
    category = _notification_category(title, body, data)

    def mutate(document):
        recipients = (set(document["users"]) if user_ids is None else
                      {user_id for user_id in user_ids if user_id in document["users"]})
        created = {}
        records = document["notifications"]
        for user_id in sorted(recipients):
            notification_id = secrets.token_hex(12)
            record = {
                "id": notification_id,
                "userId": user_id,
                "title": str(title or "HomelabHQ")[:300],
                "body": str(body or "")[:2000],
                "category": category,
                "data": copy.deepcopy(data or {}),
                "createdAt": now,
                "readAt": None,
                "dismissedAt": None,
            }
            records[notification_id] = record
            created[user_id] = notification_id

        for user_id in recipients:
            owned = sorted(
                (record for record in records.values() if record.get("userId") == user_id),
                key=lambda record: (
                    bool(not record.get("dismissedAt") and not record.get("readAt")),
                    record.get("createdAt", 0), record.get("id", "")),
            )
            for record in owned[:max(0, len(owned) - MAX_NOTIFICATIONS_PER_USER)]:
                records.pop(record["id"], None)
        return {
            user_id: {"notificationId": notification_id,
                      "unreadCount": _unread_count(records, user_id)}
            for user_id, notification_id in created.items()
        }

    created = store.update(mutate)
    logbuf.log_event("info", "notification_created", source="push",
                     recipients=len(created), category=category)
    return created


def notification_center(user_id, limit=50):
    """Return every unread entry plus recent read entries for one owner."""
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError) as error:
        raise ValueError("notification limit must be an integer") from error
    records = store.load()["notifications"]
    owned = [copy.deepcopy(record) for record in records.values()
             if record.get("userId") == user_id and not record.get("dismissedAt")]
    owned.sort(key=lambda record: (record.get("createdAt", 0), record.get("id", "")),
               reverse=True)
    unread = [record for record in owned if not record.get("readAt")]
    recent_read = [record for record in owned if record.get("readAt")][:limit]
    selected = unread + [record for record in recent_read if record not in unread]
    selected.sort(key=lambda record: (record.get("createdAt", 0), record.get("id", "")),
                  reverse=True)
    for record in selected:
        record.pop("userId", None)
    return {"notifications": selected, "unreadCount": len(unread)}


def mark_notification_read(user_id, notification_id):
    now = int(time.time())

    def mutate(document):
        record = document["notifications"].get(notification_id)
        if not record or record.get("userId") != user_id:
            return None
        if not record.get("readAt"):
            record["readAt"] = now
        public = copy.deepcopy(record)
        public.pop("userId", None)
        return {"notification": public,
                "unreadCount": _unread_count(document["notifications"], user_id)}

    return store.update(mutate)


def dismiss_notification(user_id, notification_id):
    now = int(time.time())

    def mutate(document):
        record = document["notifications"].get(notification_id)
        if not record or record.get("userId") != user_id:
            return None
        record["readAt"] = record.get("readAt") or now
        record["dismissedAt"] = record.get("dismissedAt") or now
        return {"notificationId": notification_id,
                "unreadCount": _unread_count(document["notifications"], user_id)}

    return store.update(mutate)


def mark_all_notifications_read(user_id):
    now = int(time.time())

    def mutate(document):
        for record in document["notifications"].values():
            if (record.get("userId") == user_id and not record.get("dismissedAt") and
                    not record.get("readAt")):
                record["readAt"] = now
        return {"unreadCount": _unread_count(document["notifications"], user_id)}

    return store.update(mutate)


def notify(user_ids, title, body, data=None):
    """Send a push to all subscriptions owned by user_ids (None = everyone).
    Persist the in-app entry first and prune subscriptions the push service
    reports as gone (404/410). A provider failure never removes the entry."""
    persisted = _persist_notifications(user_ids, title, body, data)
    doc = store.load()
    subscriptions = [
        (endpoint, rec) for endpoint, rec in list(doc["push_subs"].items())
        if user_ids is None or rec.get("userId") in user_ids
    ]
    sent, failed, dead, last_error = 0, 0, [], None
    if subscriptions:
        try:
            from pywebpush import webpush, WebPushException
            _ensure_vapid()
        except Exception:
            webpush = None
            WebPushException = Exception
            failed = len(subscriptions)
            last_error = "push delivery could not be initialized"
    else:
        webpush = None
        WebPushException = Exception
    for endpoint, rec in subscriptions if webpush else ():
        recipient = persisted.get(rec.get("userId")) or {}
        payload_data = {**(data or {}), **recipient}
        payload = json.dumps({"title": title, "body": body, "data": payload_data})
        try:
            webpush(subscription_info=rec["subscription"], data=payload,
                    vapid_private_key=VAPID_PRIV,
                    vapid_claims={"sub": VAPID_SUB})
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(endpoint)  # gone for good — prune
            else:
                # e.g. Apple 403 BadJwtToken from a bad VAPID 'sub'. Don't prune
                # (the subscription is fine); surface it so a "sent: 0" is
                # explainable instead of silently swallowed. Provider exception
                # text can contain the secret subscription endpoint, so never
                # retain or log it.
                failed += 1
                last_error = (f"push provider rejected delivery ({code})" if code else
                              "push provider rejected delivery")
        except Exception:
            failed += 1
            last_error = "push delivery failed"
    if dead:
        _remove_endpoints(dead)
    res = {"sent": sent, "removed": len(dead), "failed": failed,
           "persisted": len(persisted)}
    if len(persisted) == 1:
        res["unreadCount"] = next(iter(persisted.values()))["unreadCount"]
    if last_error:
        res["error"] = logbuf.redact(last_error)
    _record_delivery(res)
    if failed or dead:
        logbuf.log_event("warn", "push_delivery", source="push", sent=sent,
                         failed=failed, removed=len(dead), error=res.get("error"))
    else:
        logbuf.log_event("info", "push_delivery", source="push", sent=sent)
    return res
