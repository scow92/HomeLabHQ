"""Multi-user auth: scrypt password hashing, users, and cookie sessions.

Password hashing is the scrypt scheme from the NAC (self-describing params so
they can change without invalidating existing hashes). Users and sessions live
in the shared JSON store. Roles are just "admin" and "member": admins manage
users, members manage their own devices.
"""
import base64
import hashlib
import hmac
import secrets
import time

import store

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 1 << 14, 8, 1
SESSION_TTL = 30 * 24 * 3600  # 30 days
COOKIE_NAME = "hlhq_session"

# Brute-force throttle, keyed by client IP. Only failed credentialed attempts
# count, so ordinary page loads never trip it.
_AUTH_FAIL_WINDOW = 300
_AUTH_FAIL_MAX = 10
_auth_fails = {}  # ip -> [timestamps]


# ---- password hashing -------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r),
                            p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---- throttling -------------------------------------------------------------
def login_locked(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _auth_fails.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
    _auth_fails[ip] = fails
    return len(fails) >= _AUTH_FAIL_MAX


def record_login_fail(ip: str):
    _auth_fails.setdefault(ip, []).append(time.time())


# ---- users ------------------------------------------------------------------
def has_any_user() -> bool:
    return bool(store.load().get("users"))


def create_user(username: str, password: str, role: str = "member") -> dict:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("username and password are required")
    if role not in ("admin", "member"):
        raise ValueError("invalid role")

    def _mut(doc):
        for u in doc["users"].values():
            if u["username"].lower() == username.lower():
                raise ValueError("username already taken")
        uid = secrets.token_hex(8)
        rec = {
            "id": uid,
            "username": username,
            "passHash": hash_password(password),
            "role": role,
            "created": int(time.time()),
        }
        doc["users"][uid] = rec
        return rec

    return _public_user(store.update(_mut))


def list_users() -> list:
    return [_public_user(u) for u in store.load()["users"].values()]


def delete_user(uid: str):
    def _mut(doc):
        doc["users"].pop(uid, None)
        # drop that user's sessions too
        for tok in [t for t, s in doc["sessions"].items() if s["userId"] == uid]:
            doc["sessions"].pop(tok, None)
    store.update(_mut)


def set_password(uid: str, password: str):
    def _mut(doc):
        if uid in doc["users"]:
            doc["users"][uid]["passHash"] = hash_password(password)
    store.update(_mut)


def _public_user(u: dict) -> dict:
    """User record safe to send to the client (no hash)."""
    return {"id": u["id"], "username": u["username"], "role": u["role"]}


# ---- sessions ---------------------------------------------------------------
def login(username: str, password: str):
    """Return (token, public_user) on success, or (None, None)."""
    doc = store.load()
    for u in doc["users"].values():
        if u["username"].lower() == (username or "").strip().lower():
            if verify_password(password, u["passHash"]):
                token = secrets.token_urlsafe(32)
                rec = {"userId": u["id"], "created": int(time.time()),
                       "expires": int(time.time()) + SESSION_TTL}
                store.update(lambda d: d["sessions"].__setitem__(token, rec))
                return token, _public_user(u)
            break
    return None, None


def logout(token: str):
    if token:
        store.update(lambda d: d["sessions"].pop(token, None))


def user_for_token(token: str):
    """Return the public user for a valid, unexpired session token, else None."""
    if not token:
        return None
    doc = store.load()
    sess = doc["sessions"].get(token)
    if not sess:
        return None
    if sess["expires"] < time.time():
        store.update(lambda d: d["sessions"].pop(token, None))
        return None
    u = doc["users"].get(sess["userId"])
    return _public_user(u) if u else None
