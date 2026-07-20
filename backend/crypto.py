"""Secret-at-rest encryption for device credentials.

Device passwords / API keys / SSH keys must not sit in the JSON store as
plaintext. We derive a Fernet key from a per-instance secret (generated once,
kept 0600 in DATA_DIR/secrets) and encrypt each credential blob with it.

Not used until devices land (Milestone 2), but the key material is bootstrapped
here so the rest of the app can depend on it from day one.
"""
import base64
import binascii
import json
import os
import tempfile

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from store import SECRETS_DIR, ensure_secrets_dir

SECRET_FILE = os.path.join(SECRETS_DIR, "instance_secret")
_SECRET_BYTES = 32


def _read_instance_secret():
    try:
        with open(SECRET_FILE, "rb") as secret_file:
            encoded = secret_file.read().strip()
    except FileNotFoundError:
        return None

    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError("instance secret is invalid; restore it from backup") from error
    if len(raw) != _SECRET_BYTES:
        raise RuntimeError("instance secret is invalid; restore it from backup")
    return raw


def _new_instance_secret():
    return os.urandom(_SECRET_BYTES)


def _instance_secret():
    """Return the raw instance secret, publishing it atomically on first use."""
    ensure_secrets_dir()
    existing = _read_instance_secret()
    if existing is not None:
        return existing

    # Prepare a complete, durable file before publishing it.  A hard link is a
    # no-clobber atomic create: concurrent request threads or processes may each
    # prepare a candidate, but only one becomes the instance key and every loser
    # reads that winner instead of returning a key that was never persisted.
    candidate = _new_instance_secret()
    fd, temporary = tempfile.mkstemp(prefix=".instance_secret.", dir=SECRETS_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as secret_file:
            fd = -1
            secret_file.write(base64.urlsafe_b64encode(candidate))
            secret_file.flush()
            os.fsync(secret_file.fileno())
        try:
            os.link(temporary, SECRET_FILE)
            selected = candidate
        except FileExistsError:
            selected = _read_instance_secret()
            if selected is None:  # pragma: no cover - link existence guarantees a path
                raise RuntimeError("instance secret disappeared during creation")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    directory_fd = os.open(SECRETS_DIR, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return selected


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
