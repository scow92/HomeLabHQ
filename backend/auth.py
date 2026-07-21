"""Multi-user auth: scrypt password hashing, users, and cookie sessions.

Password hashing is the scrypt scheme from the NAC (self-describing params so
they can change without invalidating existing hashes). Users and sessions live
in the shared JSON store. Roles are just "admin" and "member": admins manage
users, members manage their own devices.
"""
import base64
import collections
import hashlib
import hmac
import os
import secrets
import threading
import time

import store
from errors import Conflict, ValidationError

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 1 << 14, 8, 1
SESSION_TTL = 30 * 24 * 3600  # 30 days
COOKIE_NAME = "hlhq_session"
MIN_PASSWORD_LENGTH = 15
# A browser normally has one session per user.  This protects the single JSON
# document from an unbounded stream of abandoned logins while retaining the
# most recently-created sessions when an operator deliberately uses many.
MAX_SESSIONS = max(1, int(os.environ.get("HLHQ_MAX_SESSIONS", "10000")))

# Brute-force throttle, keyed by client IP. Only failed credentialed attempts
# count, so ordinary page loads never trip it.
_AUTH_FAIL_WINDOW = 300
_AUTH_FAIL_MAX = 10
_AUTH_FAIL_KEYS_MAX = max(100, int(os.environ.get("HLHQ_MAX_AUTH_FAILURE_KEYS", "10000")))
_auth_fails = collections.OrderedDict()  # ip -> [timestamps], least-recently-used first
_auth_fails_lock = threading.Lock()


# ---- password hashing -------------------------------------------------------
def validate_password(password: str):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


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
    ip = str(ip or "")[:128]
    now = time.time()
    with _auth_fails_lock:
        fails = [t for t in _auth_fails.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
        if fails:
            _auth_fails[ip] = fails
            _auth_fails.move_to_end(ip)
        else:
            _auth_fails.pop(ip, None)
        return len(fails) >= _AUTH_FAIL_MAX


def record_login_fail(ip: str):
    ip = str(ip or "")[:128]
    with _auth_fails_lock:
        _auth_fails.setdefault(ip, []).append(time.time())
        _auth_fails.move_to_end(ip)
        while len(_auth_fails) > _AUTH_FAIL_KEYS_MAX:
            _auth_fails.popitem(last=False)


def clear_login_fails(ip: str):
    with _auth_fails_lock:
        _auth_fails.pop(str(ip or "")[:128], None)


# ---- users ------------------------------------------------------------------
def has_any_user() -> bool:
    return bool(store.load().get("users"))


def create_user(username: str, password: str, role: str = "member") -> dict:
    username = (username or "").strip()
    if not username:
        raise ValidationError("username is required")
    validate_password(password)
    if role not in ("admin", "member"):
        raise ValidationError("invalid role")

    def _mut(doc):
        for u in doc["users"].values():
            if u["username"].lower() == username.lower():
                raise Conflict("username already taken")
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


def create_initial_admin(username: str, password: str) -> dict:
    """Atomically prove setup is incomplete, then create its only first admin."""
    username = (username or "").strip()
    if not username:
        raise ValidationError("username is required")
    validate_password(password)

    def _mut(doc):
        if doc["users"]:
            raise Conflict("already set up")
        uid = secrets.token_hex(8)
        rec = {"id": uid, "username": username,
               "passHash": hash_password(password), "role": "admin",
               "created": int(time.time())}
        doc["users"][uid] = rec
        return rec

    return _public_user(store.update(_mut))


def list_users() -> list:
    return [_public_user(u) for u in store.load()["users"].values()]


def delete_user(uid: str):
    """Safely deprovision a user without cascading monitoring resources.

    A confirmed attempt always revokes sessions and push subscriptions. The
    account itself remains while it owns devices or dashboards so an operator
    must deliberately remove that configuration first. Once the primary
    resources are gone, the account-local Access roster is removed with the
    user.
    """
    def _mut(doc):
        target = doc["users"].get(uid)
        if not target:
            return None
        if target["role"] == "admin":
            admins = sum(1 for user in doc["users"].values()
                         if user["role"] == "admin")
            if admins <= 1:
                raise Conflict("cannot delete last admin")

        for tok in [t for t, s in doc["sessions"].items()
                    if s.get("userId") == uid]:
            doc["sessions"].pop(tok, None)
        for endpoint in [key for key, subscription in doc["push_subs"].items()
                         if subscription.get("userId") == uid]:
            doc["push_subs"].pop(endpoint, None)

        blockers = {
            "devices": sum(device.get("ownerId") == uid
                           for device in doc["devices"].values()),
            "dashboards": sum(dashboard.get("ownerId") == uid
                              for dashboard in doc["dashboards"].values()),
        }
        if any(blockers.values()):
            return blockers

        doc["clientRosters"].pop(uid, None)
        doc["users"].pop(uid, None)
        return {}

    blockers = store.update(_mut)
    if blockers:
        owned = ", ".join(
            f"{count} {kind[:-1] if count == 1 else kind}"
            for kind, count in blockers.items() if count
        )
        raise Conflict(
            f"cannot delete user while they own {owned}; "
            "delete those resources first"
        )


def set_password(uid: str, current_password: str, password: str,
                 current_token: str | None = None) -> int:
    """Change a password and revoke every session except the requesting one."""
    validate_password(password)
    new_hash = hash_password(password)
    current_session = _token_hash(current_token) if current_token else None

    def _mut(doc):
        user = doc["users"].get(uid)
        if not user or not verify_password(current_password, user.get("passHash", "")):
            raise ValidationError("current password is incorrect")
        user["passHash"] = new_hash
        revoked = 0
        for token in [token for token, session in doc["sessions"].items()
                      if session.get("userId") == uid and token != current_session]:
            doc["sessions"].pop(token, None)
            revoked += 1
        return revoked

    return store.update(_mut)


def _public_user(u: dict) -> dict:
    """User record safe to send to the client (no hash)."""
    return {"id": u["id"], "username": u["username"], "role": u["role"]}


# ---- sessions ---------------------------------------------------------------
# A syntactically-valid scrypt hash that never matches any real password.
# login() verifies against this when the username doesn't exist, so a miss
# costs the same scrypt CPU time as a wrong-password attempt on a real
# account — otherwise "no such user" answers measurably faster than "wrong
# password" and becomes a timing oracle for username enumeration.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def _token_hash(token: str) -> str:
    """Sessions are keyed by sha256(token), not the token itself, so a leaked
    or backed-up copy of the JSON store can't be replayed as a live session
    cookie — the raw token only ever exists in the client's cookie and the
    request that presents it."""
    return hashlib.sha256(token.encode()).hexdigest()


def login(username: str, password: str):
    """Return (token, public_user) on success, or (None, None)."""
    doc = store.load()
    username_norm = (username or "").strip().lower()
    match = next((u for u in doc["users"].values()
                  if u["username"].lower() == username_norm), None)
    ok = verify_password(password, match["passHash"] if match else _DUMMY_HASH)
    if not (match and ok):
        return None, None
    token = secrets.token_urlsafe(32)
    rec = {"userId": match["id"], "created": int(time.time()),
           "expires": int(time.time()) + SESSION_TTL}

    def _mut(d):
        d["sessions"][_token_hash(token)] = rec
        # Opportunistically sweep expired sessions on every login, since
        # they're otherwise only pruned when that exact token is presented
        # again — an abandoned session would linger in the store forever.
        now = int(time.time())
        for tok in [t for t, s in d["sessions"].items() if s.get("expires", 0) < now]:
            del d["sessions"][tok]
        overflow = len(d["sessions"]) - MAX_SESSIONS
        if overflow > 0:
            oldest = sorted(d["sessions"], key=lambda key: (
                d["sessions"][key].get("created", 0), key))[:overflow]
            for tok in oldest:
                del d["sessions"][tok]

    store.update(_mut)
    return token, _public_user(match)


def logout(token: str):
    if token:
        store.update(lambda d: d["sessions"].pop(_token_hash(token), None))


def user_for_token(token: str):
    """Return the public user for a valid, unexpired session token, else None."""
    if not token:
        return None
    doc = store.load()
    key = _token_hash(token)
    sess = doc["sessions"].get(key)
    if not sess:
        return None
    if sess["expires"] < time.time():
        store.update(lambda d: d["sessions"].pop(key, None))
        return None
    u = doc["users"].get(sess["userId"])
    return _public_user(u) if u else None
