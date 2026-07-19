"""Web push (VAPID) — subscriptions + delivery.

A per-instance VAPID keypair is generated on first use; the browser subscribes
with the public key, and the poller sends notifications on device state changes.
Delivery requires a secure context in the browser (HTTPS or localhost), so push
is only usable once HomelabHQ is behind TLS — the rest of the app works without
it.
"""
import base64
import json
import os
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
_metrics_lock = threading.Lock()
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


def _ensure_vapid():
    if os.path.exists(VAPID_PRIV) and os.path.exists(VAPID_PUB):
        return
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    ensure_secrets_dir()
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    fd = os.open(VAPID_PRIV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    with open(VAPID_PUB, "w") as f:
        f.write(pub_b64)


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


def unsubscribe(endpoint):
    store.update(lambda d: d["push_subs"].pop(endpoint, None))


def recipients_for_device(dev):
    """Owner + every admin (they oversee all devices)."""
    doc = store.load()
    ids = {dev.get("ownerId")}
    for u in doc["users"].values():
        if u.get("role") == "admin":
            ids.add(u["id"])
    return {i for i in ids if i}


def notify(user_ids, title, body, data=None):
    """Send a push to all subscriptions owned by user_ids (None = everyone).
    Prunes subscriptions the push service reports as gone (404/410)."""
    from pywebpush import webpush, WebPushException
    _ensure_vapid()
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    doc = store.load()
    sent, failed, dead, last_error = 0, 0, [], None
    for endpoint, rec in list(doc["push_subs"].items()):
        if user_ids is not None and rec.get("userId") not in user_ids:
            continue
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
                # explainable instead of silently swallowed.
                failed += 1
                last_error = f"{code or ''} {e}".strip()
        except Exception as e:
            failed += 1
            last_error = str(e)
    for endpoint in dead:
        unsubscribe(endpoint)
    res = {"sent": sent, "removed": len(dead), "failed": failed}
    if last_error:
        res["error"] = logbuf.redact(last_error)
    _record_delivery(res)
    if failed or dead:
        logbuf.log_event("warn", "push_delivery", source="push", sent=sent,
                         failed=failed, removed=len(dead), error=res.get("error"))
    else:
        logbuf.log_event("info", "push_delivery", source="push", sent=sent)
    return res
