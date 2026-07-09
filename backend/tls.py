"""TLS support so the app can serve HTTPS — required for web push and full PWA
behaviour, which browsers only allow in a secure context.

Cert resolution order:
  1. HLHQ_TLS_CERT + HLHQ_TLS_KEY env paths (bring your own — real/trusted cert).
  2. A drop-in mounted cert: /certs/nm.crt + /certs/nm.key (or tls.crt/tls.key).
  3. A self-signed cert auto-generated into the data dir on first run.

A self-signed cert makes the origin HTTPS, but the browser will warn until you
trust it; for painless push use a trusted cert (mkcert local CA, Let's Encrypt,
tailscale cert) via option 1 or 2.
"""
import datetime
import ipaddress
import os
import socket

from store import DATA_DIR

GEN_CERT = os.path.join(DATA_DIR, "tls_cert.pem")
GEN_KEY = os.path.join(DATA_DIR, "tls_key.pem")


def _configured_cert():
    c, k = os.environ.get("HLHQ_TLS_CERT"), os.environ.get("HLHQ_TLS_KEY")
    if c and k and os.path.exists(c) and os.path.exists(k):
        return c, k
    for base in ("/certs/nm", "/certs/tls"):
        if os.path.exists(base + ".crt") and os.path.exists(base + ".key"):
            return base + ".crt", base + ".key"
    return None


def default_hosts():
    hosts = ["localhost", "127.0.0.1"]
    hosts += [h.strip() for h in os.environ.get("HLHQ_TLS_HOSTS", "").split(",")
              if h.strip()]
    try:  # best-effort primary LAN IP so the SAN matches how you reach it
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        hosts.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return list(dict.fromkeys(hosts))


def ensure_cert(hosts=None):
    """Return (certfile, keyfile), generating a self-signed pair if needed."""
    conf = _configured_cert()
    if conf:
        return conf
    if os.path.exists(GEN_CERT) and os.path.exists(GEN_KEY):
        return GEN_CERT, GEN_KEY
    _generate_self_signed(GEN_CERT, GEN_KEY, hosts or default_hosts())
    return GEN_CERT, GEN_KEY


def _generate_self_signed(certfile, keyfile, hosts):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = []
    for h in hosts:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san.append(x509.DNSName(h))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HomelabHQ")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
