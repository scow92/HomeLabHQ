"""Protocol plumbing that dispatches declarative API routes."""
import time
import sys
import uuid
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse

import auth
import logbuf
import poller
import services
import store
import transports
from context import Actor
from errors import ApplicationError, UpstreamUnavailable, ValidationError

from .requests import Request, decode_json
from .responses import error_response, json_response, write_response
from .router import AuthPolicy, Router
from .static import serve_certificate, serve_static


DEFAULT_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; base-uri 'self'; "
               "form-action 'self'; frame-ancestors 'self'; object-src 'none'")


class Handler(BaseHTTPRequestHandler):
    """Small adapter from ``BaseHTTPRequestHandler`` to the application API."""
    server_version = "HomelabHQ/0.1"
    router = Router()
    web_dir = "web"
    csp = DEFAULT_CSP
    max_json_body_bytes = 1_048_576
    trust_proxy = False
    tls_enabled = False
    self_signed = False
    icon_http_port = 0
    icon_ver = "1"

    def log_message(self, *args):
        pass

    def _begin_request(self, route_name=""):
        self._t0 = time.monotonic()
        self._request_id = uuid.uuid4().hex
        self._route_name = route_name

    def client_ip(self):
        if self.trust_proxy:
            forwarded = self.headers.get("X-Real-IP")
            if forwarded:
                return forwarded
        return self.client_address[0]

    # Compatibility aliases keep transport details private while allowing
    # existing integrations to use the old helper names during the migration.
    _client_ip = client_ip

    def token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw)
            morsel = cookies.get(auth.COOKIE_NAME)
            return morsel.value if morsel else None
        except Exception:
            return None

    _token = token

    def current_user(self):
        return auth.user_for_token(self.token())

    _current_user = current_user

    def set_session_cookie(self, token):
        secure = "; Secure" if self.tls_enabled else ""
        return ("Set-Cookie", f"{auth.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
                f"{secure}; Max-Age={auth.SESSION_TTL}")

    _set_session_cookie = set_session_cookie

    def clear_session_cookie(self):
        secure = "; Secure" if self.tls_enabled else ""
        return ("Set-Cookie", f"{auth.COOKIE_NAME}=; HttpOnly; Path=/; SameSite=Lax{secure}; "
                "Max-Age=0")

    _clear_session_cookie = clear_session_cookie

    def _record_response(self, code):
        try:
            path = urlparse(self.path).path
            if path in logbuf.LOG_SKIP_PATHS:
                return
            started = getattr(self, "_t0", None)
            entry = {"ts": time.time(), "ip": self.client_ip(), "method": self.command,
                     "path": path, "status": code,
                     "ms": int((time.monotonic() - started) * 1000) if started else None,
                     "request_id": getattr(self, "_request_id", None),
                     "route": getattr(self, "_route_name", None) or "static"}
            if code >= 400:
                entry["error"] = "request failed"
            logbuf.log_event("error" if code >= 500 else "warn" if code >= 400 else "info",
                             "request", source="http", **entry)
        except Exception:
            pass

    def _send_json(self, code, obj, extra_headers=None, head=False):
        return write_response(self, json_response(obj, code, extra_headers or ()), head=head)

    def _send_application_error(self, error):
        response = error_response(error)
        return self._send_json(response.status, response.value)

    def _read_json(self):
        # Keep the old module-level setting patchable during the migration.
        app = sys.modules.get("app")
        max_bytes = getattr(app, "MAX_JSON_BODY_BYTES", self.max_json_body_bytes)
        return decode_json(self, max_bytes)

    def _serve_static(self, path, head=False):
        """Compatibility entry point; static delivery itself lives in static.py."""
        app = sys.modules.get("app")
        web_dir = getattr(app, "WEB_DIR", self.web_dir)
        root = Path(web_dir).resolve()
        try:
            (root / unquote(path).lstrip("/")).resolve().relative_to(root)
        except (ValueError, OSError):
            return self._send_json(403, {"error": "forbidden"}, head=head)
        return serve_static(self, path, web_dir, self.csp, self._rewrite_apple_icon, head=head)

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            host = self.headers.get("Host", "")
            return origin in (f"http://{host}", f"https://{host}")
        site = self.headers.get("Sec-Fetch-Site")
        return site in ("same-origin", "none") if site is not None else True

    def _actor_for(self, policy):
        if policy is AuthPolicy.PUBLIC:
            return None
        user = self.current_user()
        if not user:
            from errors import AuthenticationRequired
            raise AuthenticationRequired()
        actor = Actor.from_user(user)
        if policy is AuthPolicy.ADMIN:
            services.require_admin(actor)
        return actor

    def _dispatch(self, method, path):
        resolved = self.router.resolve(method, path)
        if resolved is None:
            self._route_name = "not-found"
            return self._send_json(404, {"error": "not found"})
        route, params = resolved
        self._route_name = route.name
        try:
            request = Request(self, path, params=params, actor=self._actor_for(route.auth))
            response = route.endpoint(request)
            return write_response(self, response)
        except ApplicationError as error:
            return self._send_application_error(error)
        except transports.ConnectionError as error:
            return self._send_application_error(UpstreamUnavailable(str(error)))
        except ValueError as error:
            return self._send_application_error(ValidationError(str(error)))
        except Exception:
            return self._send_json(500, {"error": "internal server error"})

    def _rewrite_apple_icon(self, data):
        if not (self.self_signed and self.icon_http_port):
            return data
        host = self.headers.get("Host", "").split(":")[0]
        if not host:
            return data
        base = f"http://{host}:{self.icon_http_port}".encode()
        version = f"?v={self.icon_ver}".encode()
        return data.replace(b'rel="apple-touch-icon" href="/apple-touch-icon.png"',
                            b'rel="apple-touch-icon" href="' + base +
                            b'/apple-touch-icon.png' + version + b'"')

    def do_GET(self):
        self._begin_request()
        path = urlparse(self.path).path
        if path == "/healthz":
            self._route_name = "healthz"
            return self._send_json(200, {"ok": True})
        if path == "/readyz":
            self._route_name = "readyz"
            return self._ready_response()
        if path in ("/homelabhq.crt", "/nac.crt"):
            return serve_certificate(self)
        if path.startswith("/api/"):
            return self._dispatch("GET", path)
        return self._serve_static(path)

    def do_HEAD(self):
        self._begin_request()
        path = urlparse(self.path).path
        if path == "/healthz":
            self._route_name = "healthz"
            return self._send_json(200, {"ok": True}, head=True)
        if path == "/readyz":
            self._route_name = "readyz"
            return self._ready_response(head=True)
        if path in ("/homelabhq.crt", "/nac.crt"):
            return serve_certificate(self, head=True)
        if path.startswith("/api/"):
            return self._send_json(405, {"error": "method not allowed"}, head=True)
        return self._serve_static(path, head=True)

    def _mutating(self, method):
        self._begin_request()
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send_json(404, {"error": "not found"})
        if not self._same_origin():
            return self._send_json(403, {"error": "cross-origin request blocked"})
        return self._dispatch(method, path)

    def _ready_response(self, head=False):
        """Readiness needs durable storage and a functioning poll loop.

        Keep the public response intentionally small: detailed device/push
        observations remain in structured logs and the administrator log view.
        """
        try:
            store.load()
            store_ready = True
        except Exception:
            store_ready = False
        state = poller.status()
        ready = store_ready and state["ready"]
        return self._send_json(200 if ready else 503, {
            "ok": ready,
            "store": "ready" if store_ready else "unavailable",
            "poller": "ready" if state["ready"] else "starting",
        }, head=head)

    def do_POST(self):
        return self._mutating("POST")

    def do_DELETE(self):
        return self._mutating("DELETE")

    def do_PATCH(self):
        return self._mutating("PATCH")
