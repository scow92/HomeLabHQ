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

import store
from store import DATA_DIR

VAPID_PRIV = os.path.join(DATA_DIR, "vapid_private.pem")
VAPID_PUB = os.path.join(DATA_DIR, "vapid_public.txt")
VAPID_SUB = os.environ.get("HLHQ_VAPID_SUB", "mailto:admin@homelabhq.local")


def _ensure_vapid():
    if os.path.exists(VAPID_PRIV) and os.path.exists(VAPID_PUB):
        return
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    os.makedirs(DATA_DIR, exist_ok=True)
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
    store.update(lambda d: d["push_subs"].__setitem__(endpoint, rec))


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
    sent, dead = 0, []
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
                dead.append(endpoint)
        except Exception:
            pass
    for endpoint in dead:
        unsubscribe(endpoint)
    return {"sent": sent, "removed": len(dead)}
