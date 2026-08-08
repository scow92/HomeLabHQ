"""Transport connections: the wire a driver talks over.

A driver declares which transports it speaks; the detection pipeline opens the
matching connection and hands it to the driver's probe()/entities(). Each
connection type exposes only the primitives that make sense for it (SSH runs
commands, SNMP gets/walks OIDs), plus a common `.info()` banner used for cheap
fingerprinting before a full probe.

SSH uses paramiko; SNMP uses pysnmp; the "api" transport uses requests to talk
to an HTTP/REST API authenticated with an API key + secret. All are thin — the
interesting logic lives in the drivers.
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urljoin, urlparse

import requests

import store


class ConnectionError(Exception):
    pass


def _url_origin(url):
    parsed = urlparse(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class _CredentialSafeSession(requests.Session):
    """Keep redirects useful without forwarding device secrets elsewhere.

    Device web interfaces commonly redirect between paths, and some upgrade
    their own HTTP listener to HTTPS.  Those redirects remain transparent.  A
    redirect to another hostname is stopped before the next request is sent,
    while an origin change on the same host drops configured auth headers.
    """

    def __init__(self, sensitive_headers=()):
        super().__init__()
        self._sensitive_headers = {str(name).lower() for name in sensitive_headers if name}

    def get_redirect_target(self, response):
        target = super().get_redirect_target(response)
        if target:
            destination = urljoin(response.url, target)
            if urlparse(destination).hostname != urlparse(response.url).hostname:
                raise requests.exceptions.InvalidURL("cross-host redirect blocked")
        return target

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if _url_origin(prepared_request.url) == _url_origin(response.request.url):
            return
        for name in list(prepared_request.headers):
            if name.lower() in self._sensitive_headers:
                prepared_request.headers.pop(name, None)


class _TOFUHostKeyPolicy:
    """Trust-on-first-use SSH host key verification.

    paramiko's AutoAddPolicy accepts whatever key a server presents, every
    time, with nothing recorded — a LAN MITM (e.g. ARP spoofing the device's
    IP) can silently intercept the session and capture credentials. This pins
    the key the first time we connect to a given host:port (persisted in the
    store) and requires an exact match on every connection after that; a
    changed key raises instead of connecting, surfacing as a clear "host key
    changed" error rather than a hang or a generic auth failure.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def missing_host_key(self, client, hostname, key):
        import paramiko
        fingerprint = hashlib.sha256(key.asbytes()).hexdigest()
        key_type = key.get_name()
        pinned = store.ssh_host_key(self.host, self.port)
        if pinned is None:
            store.pin_ssh_host_key(self.host, self.port, key_type, fingerprint)
            return
        if pinned.get("keyType") != key_type or pinned.get("fingerprint") != fingerprint:
            raise paramiko.SSHException(
                f"SSH host key for {self.host}:{self.port} has changed since it "
                f"was first trusted (was {pinned.get('keyType')} "
                f"{pinned.get('fingerprint', '')[:16]}…, now {key_type} "
                f"{fingerprint[:16]}…) — this could mean a man-in-the-middle, or "
                f"that the device was reinstalled/replaced. If expected, remove "
                f"the pinned key for this host and reconnect.")


class Connection:
    transport = None
    host = None

    def info(self) -> str:
        """A cheap identifying banner (SSH server string / SNMP sysDescr)."""
        return ""

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---- SSH -------------------------------------------------------------------
class SSHConnection(Connection):
    transport = "ssh"

    def __init__(self, host, port=22, username=None, password=None,
                 private_key=None, timeout=8):
        self.host = host
        self.port = int(port or 22)
        self.username = username
        self.password = password
        self.private_key = private_key
        self.timeout = timeout
        self._client = None

    def connect(self):
        import io
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_TOFUHostKeyPolicy(self.host, self.port))
        kwargs = dict(hostname=self.host, port=self.port, username=self.username,
                      timeout=self.timeout, banner_timeout=self.timeout,
                      auth_timeout=self.timeout, allow_agent=False,
                      look_for_keys=False)
        if self.private_key:
            for loader in (paramiko.Ed25519Key, paramiko.ECDSAKey,
                           paramiko.RSAKey):
                try:
                    kwargs["pkey"] = loader.from_private_key(
                        io.StringIO(self.private_key))
                    break
                except Exception:
                    continue
        else:
            kwargs["password"] = self.password
        try:
            client.connect(**kwargs)
        except Exception as e:
            raise ConnectionError(f"SSH connect failed: {e}") from e
        self._client = client
        return self

    def info(self) -> str:
        try:
            return self._client.get_transport().remote_version or ""
        except Exception:
            return ""

    def run(self, cmd, timeout=12):
        """Run a command. Returns (exit_code, stdout, stderr)."""
        if not self._client:
            raise ConnectionError("not connected")
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


# ---- SNMP ------------------------------------------------------------------
# OIDs used across drivers.
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_IF_NUMBER = "1.3.6.1.2.1.2.1.0"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"


class SNMPConnection(Connection):
    transport = "snmp"

    def __init__(self, host, port=161, community="public", version="2c",
                 timeout=3, retries=1):
        self.host = host
        self.port = int(port or 161)
        self.community = community
        self.version = version
        self.timeout = timeout
        self.retries = retries
        self._descr = None

    def connect(self):
        # SNMP is connectionless; verify reachability by fetching sysDescr.
        descr = self.get(OID_SYS_DESCR)
        if descr is None:
            raise ConnectionError("no SNMP response (sysDescr)")
        self._descr = descr
        return self

    def info(self) -> str:
        return self._descr or (self.get(OID_SYS_DESCR) or "")

    def get(self, oid):
        return _snmp.get(self, oid)

    def walk(self, oid):
        return _snmp.walk(self, oid)


# The concrete pysnmp calls live in snmp_backend, isolated so the exact pysnmp
# API version is handled in one place.
from snmp_backend import snmp as _snmp  # noqa: E402


# ---- HTTP / REST API (key + secret) ----------------------------------------
class HTTPResponse:
    """A minimal, driver-friendly view of a requests.Response."""

    def __init__(self, resp, elapsed_ms):
        self.status = resp.status_code
        self.headers = dict(resp.headers)
        self.elapsed_ms = elapsed_ms
        self._resp = resp

    @property
    def text(self):
        return self._resp.text

    def json(self):
        try:
            return self._resp.json()
        except Exception:
            return None


class _BaseHTTPConnection(Connection):
    """Shared plumbing for the two HTTP-flavored transports (`api` and
    `http`): scheme-in-host parsing, base-URL assembly, the lazy
    `requests.Session`, request()/get()/info()/close(), and urllib3's
    TLS-verification warning suppression.

    Subclasses provide `_new_session()` (their auth wiring) and their own
    `connect()` — the two differ enough there (one verifies a 401/403 up
    front as an auth failure, the other can't assert auth from the response
    at all) that sharing it would just reintroduce a branch.
    """

    def __init__(self, host, port=None, scheme="https", base_path="",
                 verify_tls=True, probe_path="/", timeout=8):
        # Allow host to carry its own scheme, e.g. "http://10.0.0.1".
        if "://" in (host or ""):
            scheme, host = host.split("://", 1)
        host = (host or "").rstrip("/")
        base_path = ("/" + base_path.strip("/")) if base_path else ""
        netloc = host + (f":{port}" if port else "")
        self.host = host
        self.base_url = f"{scheme}://{netloc}{base_path}"
        self.verify_tls = bool(verify_tls)
        self.probe_path = probe_path or "/"
        self.timeout = timeout
        self._session = None
        self.last = None  # HTTPResponse from the most recent request

    def _new_session(self):
        """Build the requests.Session for this connection, with auth wired
        up. Overridden per transport."""
        raise NotImplementedError

    @property
    def session(self):
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def _disable_tls_warnings_if_needed(self):
        if not self.verify_tls:
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass

    def request(self, method, path, **kw):
        url = path if "://" in path else self.base_url + (
            path if path.startswith("/") else "/" + path)
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("verify", self.verify_tls)
        t0 = time.monotonic()
        try:
            resp = self.session.request(method, url, **kw)
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"HTTP request failed: {e}") from e
        self.last = HTTPResponse(resp, round((time.monotonic() - t0) * 1000))
        return self.last

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def info(self) -> str:
        if self.last is None:
            return ""
        return f"HTTP {self.last.status} {self.last.headers.get('Server', '')}".strip()

    def close(self):
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


class HTTPConnection(_BaseHTTPConnection):
    """An HTTP/REST API reached with an API key + secret.

    Auth styles:
      - "basic"  : HTTP Basic, key as username + secret as password
                   (OPNsense and many firewalls/switches work this way)
      - "bearer" : Authorization: Bearer <key>   (secret unused)
      - "header" : custom headers, default X-API-Key / X-API-Secret
    """
    transport = "api"

    def __init__(self, host, port=None, api_key=None, api_secret=None,
                 auth_style="basic", scheme="https", base_path="",
                 verify_tls=True, key_header="X-API-Key",
                 secret_header="X-API-Secret", probe_path="/", timeout=8):
        super().__init__(host, port=port, scheme=scheme, base_path=base_path,
                         verify_tls=verify_tls, probe_path=probe_path,
                         timeout=timeout)
        self.api_key = api_key
        self.api_secret = api_secret
        self.auth_style = (auth_style or "basic").lower()
        self.key_header = key_header
        self.secret_header = secret_header

    def _new_session(self):
        sensitive_headers = {"Authorization"}
        if self.auth_style == "header":
            sensitive_headers.update((self.key_header, self.secret_header))
        s = _CredentialSafeSession(sensitive_headers)
        if self.auth_style == "basic":
            s.auth = (self.api_key or "", self.api_secret or "")
        elif self.auth_style == "bearer":
            s.headers["Authorization"] = f"Bearer {self.api_key or ''}"
        elif self.auth_style == "header":
            if self.api_key:
                s.headers[self.key_header] = self.api_key
            if self.api_secret:
                s.headers[self.secret_header] = self.api_secret
        else:
            raise ConnectionError(f"unknown auth style: {self.auth_style}")
        return s

    def connect(self):
        self._disable_tls_warnings_if_needed()
        resp = self.request("GET", self.probe_path)
        # A response at all means the host is reachable. 401/403 means the
        # key/secret were rejected — surface that as an auth failure, not a
        # "detected" device.
        if resp.status in (401, 403):
            raise ConnectionError(f"API auth rejected (HTTP {resp.status})")
        return self


# ---- HTTP web UI (username + password) -------------------------------------
class HTTPWebConnection(_BaseHTTPConnection):
    """A device's HTML web UI reached with a username + password.

    Unlike the `api` transport (standard Basic/Bearer auth verified up front),
    web-UI login is device-specific — some set a hashed cookie, some POST a
    form, some use Basic. So this transport only provides the HTTP plumbing
    (a browser-like session + a couple of login helpers) and leaves the actual
    login to the driver's probe()/entities(), which know their device's scheme.
    """
    transport = "http"

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
           "Gecko/20100101 Firefox/140.0")

    def __init__(self, host, port=None, username=None, password=None,
                 scheme="http", base_path="", verify_tls=True,
                 probe_path="/", timeout=12, metrics_path=None):
        super().__init__(host, port=port, scheme=scheme, base_path=base_path,
                         verify_tls=verify_tls, probe_path=probe_path,
                         timeout=timeout)
        self.username = username
        self.password = password
        # Optional Prometheus /metrics path a driver may scrape for extra data.
        self.metrics_path = metrics_path or None

    def _new_session(self):
        s = _CredentialSafeSession()
        s.headers.update({"User-Agent": self._UA, "Connection": "close"})
        return s

    def connect(self):
        self._disable_tls_warnings_if_needed()
        # Reachability only — a web UI usually returns 200 even for the login
        # page, so we don't (can't) assert auth here; drivers confirm that.
        self.request("GET", self.probe_path)
        return self

    # -- login helpers a driver can call --
    def set_cookie(self, name, value):
        hostname = urlparse(self.base_url).hostname
        self.session.cookies.set(name, value, domain=hostname, path="/")

    def login_md5_cookie(self, cookie_name="admin"):
        """Realtek-style web-smart-switch login (Keeplink et al.): the session
        cookie is md5(username + password)."""
        digest = hashlib.md5(
            ((self.username or "") + (self.password or "")).encode()).hexdigest()
        self.set_cookie(cookie_name, digest)
        return digest

    def login_form(self, path, fields):
        return self.request("POST", path, data=fields)


# ---- factory ---------------------------------------------------------------
def open_connection(transport, host, port=None, credentials=None, timeout=8):
    """Open and return a connected Connection for the given transport."""
    creds = credentials or {}
    if transport == "ssh":
        conn = SSHConnection(
            host, port=port or 22, username=creds.get("username"),
            password=creds.get("password"),
            private_key=creds.get("privateKey"), timeout=timeout)
        return conn.connect()
    if transport == "snmp":
        conn = SNMPConnection(
            host, port=port or 161,
            community=creds.get("community", "public"),
            version=creds.get("version", "2c"), timeout=min(timeout, 4))
        return conn.connect()
    if transport == "api":
        conn = HTTPConnection(
            host, port=port, api_key=creds.get("apiKey"),
            api_secret=creds.get("apiSecret"),
            auth_style=creds.get("authStyle", "basic"),
            scheme=creds.get("scheme", "https"),
            base_path=creds.get("basePath", ""),
            verify_tls=creds.get("verifyTls", True),
            key_header=creds.get("keyHeader", "X-API-Key"),
            secret_header=creds.get("secretHeader", "X-API-Secret"),
            probe_path=creds.get("probePath", "/"), timeout=timeout)
        return conn.connect()
    if transport == "http":
        conn = HTTPWebConnection(
            host, port=port, username=creds.get("username"),
            password=creds.get("password"),
            scheme=creds.get("scheme", "http"),
            base_path=creds.get("basePath", ""),
            verify_tls=creds.get("verifyTls", True),
            probe_path=creds.get("probePath", "/"),
            metrics_path=creds.get("metricsPath"), timeout=timeout)
        return conn.connect()
    raise ConnectionError(f"unsupported transport: {transport}")
