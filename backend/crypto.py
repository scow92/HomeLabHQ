"""Secret-at-rest encryption for device credentials.

Device passwords / API keys / SSH keys must not sit in the JSON store as
plaintext. We derive a Fernet key from a per-instance secret (generated once,
kept 0600 in the private data dir) and encrypt each credential blob with it.

Not used until devices land (Milestone 2), but the key material is bootstrapped
here so the rest of the app can depend on it from day one.
"""
import base64
import json
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from store import DATA_DIR

SECRET_FILE = os.path.join(DATA_DIR, "instance_secret")


def _instance_secret():
    """Return the raw instance secret, creating it on first use (0600)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            raw = f.read().strip()
        if raw:
            return base64.urlsafe_b64decode(raw)
    raw = os.urandom(32)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.urlsafe_b64encode(raw))
    return raw


def _fernet():
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"homelabhq-credential-v1",
        info=b"fernet-key",
    ).derive(_instance_secret())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(obj) -> str:
    """Encrypt a JSON-serializable credential object -> opaque token string."""
    return _fernet().encrypt(json.dumps(obj).encode()).decode()


def decrypt(token: str):
    """Decrypt a token produced by encrypt() back into the original object."""
    return json.loads(_fernet().decrypt(token.encode()).decode())
