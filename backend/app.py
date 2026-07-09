#!/usr/bin/env python3
"""NetManager backend — threading HTTP server.

Same shape as the NAC's server.py (stdlib http.server + ThreadingMixIn) but
organized around generic multi-user auth and, in later milestones, devices and
drivers. Serves the SPA shell from ../web and a small JSON API under /api.
"""
import json
import os
import signal
import sys
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

import auth
import store
import devices
import detect
import transports
import poller
import drivers  # noqa: F401  # importing self-registers all bundled drivers
from drivers import registry

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.environ.get("NM_WEB_DIR", os.path.join(HERE, "..", "web"))
PORT = int(os.environ.get("NM_PORT", "8770"))

# Set true in main() when serving HTTPS, so session cookies get the Secure flag.
TLS_ENABLED = False


def _tls_requested():
    if os.environ.get("NM_TLS_CERT") and os.environ.get("NM_TLS_KEY"):
        return True
    return os.environ.get("NM_TLS", "").lower() in ("1", "true", "yes", "auto")

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


def _match(path, prefix, suffix):
    """Return the id segment of /prefix<id><suffix>, or None. No empty ids."""
    if path.startswith(prefix) and path.endswith(suffix) and len(suffix):
        mid = path[len(prefix):len(path) - len(suffix)]
        return mid if mid and "/" not in mid else None
    return None


def _owns(user, dev):
    """A user may act on their own devices; admins on any."""
    return user["role"] == "admin" or dev.get("ownerId") == user["id"]


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "NetManager/0.1"

    # ---- plumbing -----------------------------------------------------------
    def log_message(self, *a):
        pass  # keep the console quiet; add real logging later

    def _client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            m = c.get(auth.COOKIE_NAME)
            return m.value if m else None
        except Exception:
            return None

    def _current_user(self):
        return auth.user_for_token(self._token())

    def _set_session_cookie(self, token):
        # HttpOnly + SameSite=Lax; Secure when we're serving HTTPS.
        secure = "; Secure" if TLS_ENABLED else ""
        return ("Set-Cookie",
                f"{auth.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
                f"{secure}; Max-Age={auth.SESSION_TTL}")

    def _clear_session_cookie(self):
        secure = "; Secure" if TLS_ENABLED else ""
        return ("Set-Cookie",
                f"{auth.COOKIE_NAME}=; HttpOnly; Path=/; SameSite=Lax{secure}; "
                f"Max-Age=0")

    # ---- dispatch -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_post(path)
        return self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_delete(path)
        return self._send_json(404, {"error": "not found"})

    # ---- API: GET -----------------------------------------------------------
    def _api_get(self, path):
        if path == "/api/session":
            user = self._current_user()
            return self._send_json(200, {
                "authenticated": bool(user),
                "needsSetup": not auth.has_any_user(),
                "user": user,
            })

        user = self._current_user()
        if not user:
            return self._send_json(401, {"error": "unauthenticated"})

        if path == "/api/users":
            if user["role"] != "admin":
                return self._send_json(403, {"error": "admin only"})
            return self._send_json(200, {"users": auth.list_users()})

        if path == "/api/drivers":
            # Catalogue for the setup wizard: transports and known drivers.
            drvs = [{"id": d.id, "displayName": d.display_name,
                     "transports": d.transports} for d in registry.all_drivers()]
            transports_avail = sorted({t for d in drvs for t in d["transports"]})
            return self._send_json(200, {"drivers": drvs,
                                         "transports": transports_avail})

        if path == "/api/push/vapid":
            try:
                import push
                return self._send_json(200, {"publicKey": push.public_key()})
            except Exception as e:
                return self._send_json(503, {"error": f"push unavailable: {e}"})

        if path == "/api/devices":
            return self._send_json(200, {"devices": devices.list_devices(
                user["id"], is_admin=user["role"] == "admin")})

        # /api/devices/<id>/history?key=<k> — stored history for one entity
        h = _match(path, "/api/devices/", "/history")
        if h:
            dev = devices.get_device(h)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            key = (parse_qs(urlparse(self.path).query).get("key") or [None])[0]
            series = (dev.get("history") or {}).get(key, []) if key else {}
            return self._send_json(200, {"key": key, "series": series})

        # /api/devices/<id>/state — live read of the device's sensors
        m = _match(path, "/api/devices/", "/state")
        if m:
            dev = devices.get_device(m)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                return self._send_json(200, devices.read_state(m))
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        return self._send_json(404, {"error": "not found"})

    # ---- API: POST ----------------------------------------------------------
    def _api_post(self, path):
        body = self._read_json()

        if path == "/api/setup":
            # First-run: create the initial admin. Refused once any user exists.
            if auth.has_any_user():
                return self._send_json(409, {"error": "already set up"})
            try:
                auth.create_user(body.get("username"), body.get("password"),
                                 role="admin")
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            token, u = auth.login(body.get("username"), body.get("password"))
            return self._send_json(200, {"user": u},
                                   extra_headers=[self._set_session_cookie(token)])

        if path == "/api/login":
            ip = self._client_ip()
            if auth.login_locked(ip):
                return self._send_json(429, {"error": "too many attempts"})
            token, u = auth.login(body.get("username"), body.get("password"))
            if not token:
                auth.record_login_fail(ip)
                return self._send_json(401, {"error": "invalid credentials"})
            return self._send_json(200, {"user": u},
                                   extra_headers=[self._set_session_cookie(token)])

        if path == "/api/logout":
            auth.logout(self._token())
            return self._send_json(200, {"ok": True},
                                   extra_headers=[self._clear_session_cookie()])

        # everything below requires a session
        user = self._current_user()
        if not user:
            return self._send_json(401, {"error": "unauthenticated"})

        if path == "/api/users":
            if user["role"] != "admin":
                return self._send_json(403, {"error": "admin only"})
            try:
                rec = auth.create_user(body.get("username"), body.get("password"),
                                       role=body.get("role", "member"))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"user": rec})

        if path == "/api/account/password":
            if not body.get("password"):
                return self._send_json(400, {"error": "password required"})
            auth.set_password(user["id"], body["password"])
            return self._send_json(200, {"ok": True})

        # ---- web push ----
        if path == "/api/push/subscribe":
            try:
                import push
                push.subscribe(user["id"], body.get("subscription"))
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"ok": True})

        if path == "/api/push/unsubscribe":
            try:
                import push
                push.unsubscribe(body.get("endpoint"))
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"ok": True})

        if path == "/api/push/test":
            try:
                import push
                res = push.notify({user["id"]}, "NetManager test",
                                  "Push notifications are working.")
            except Exception as e:
                return self._send_json(503, {"error": str(e)})
            return self._send_json(200, res)

        # ---- device setup wizard ----
        if path == "/api/devices/detect":
            # Probe a device and rank matching drivers by confidence.
            try:
                result = detect.detect(
                    body.get("transport"), body.get("host"),
                    body.get("port"), body.get("credentials"))
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, result)

        if path == "/api/devices/entities":
            # List the entities a chosen driver exposes on this device.
            try:
                ents = detect.enumerate_entities(
                    body.get("transport"), body.get("host"), body.get("port"),
                    body.get("credentials"), body.get("driverId"))
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"entities": ents})

        if path == "/api/devices":
            try:
                rec = devices.create_device(
                    owner_id=user["id"], host=body.get("host"),
                    transport=body.get("transport"), port=body.get("port"),
                    credentials=body.get("credentials"),
                    driver_id=body.get("driverId"), name=body.get("name"),
                    entities=body.get("entities"))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"device": rec})

        return self._send_json(404, {"error": "not found"})

    # ---- API: DELETE --------------------------------------------------------
    def _api_delete(self, path):
        user = self._current_user()
        if not user:
            return self._send_json(401, {"error": "unauthenticated"})

        if path == "/api/users":
            if user["role"] != "admin":
                return self._send_json(403, {"error": "admin only"})
            uid = (parse_qs(urlparse(self.path).query).get("id") or [None])[0]
            if not uid:
                return self._send_json(400, {"error": "id required"})
            if uid == user["id"]:
                return self._send_json(400, {"error": "cannot delete yourself"})
            admins = [u for u in auth.list_users() if u["role"] == "admin"]
            target = next((u for u in auth.list_users() if u["id"] == uid), None)
            if target and target["role"] == "admin" and len(admins) <= 1:
                return self._send_json(400, {"error": "cannot delete last admin"})
            auth.delete_user(uid)
            return self._send_json(200, {"ok": True})

        if path == "/api/devices":
            dev_id = (parse_qs(urlparse(self.path).query).get("id") or [None])[0]
            if not dev_id:
                return self._send_json(400, {"error": "id required"})
            dev = devices.get_device(dev_id)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            devices.delete_device(dev_id)
            return self._send_json(200, {"ok": True})

        return self._send_json(404, {"error": "not found"})

    # ---- static -------------------------------------------------------------
    def _serve_static(self, path):
        if path == "/" or not path:
            path = "/index.html"
        # normalize and prevent traversal outside WEB_DIR
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)):
            return self._send_json(403, {"error": "forbidden"})
        if not os.path.isfile(full):
            # SPA fallback: serve index.html for client-side routes
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(full):
                return self._send_json(404, {"error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        ctype = _STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except Exception:
            return self._send_json(500, {"error": "read failed"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    # Never buffer stdout: container runtimes read logs from the pipe, and a
    # buffered "listening" line makes `docker compose up` look dead on start.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    global TLS_ENABLED
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    scheme = "http"
    if _tls_requested():
        import ssl
        import tls
        certfile, keyfile = tls.ensure_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        TLS_ENABLED = True
        scheme = "https"
        print(f"TLS: serving HTTPS using {certfile}", flush=True)

    print(f"NetManager backend listening on {scheme}://0.0.0.0:{PORT}  "
          f"(data: {store.DATA_DIR})", flush=True)
    poller.start()

    # Shut down cleanly on SIGTERM (what `docker stop`/compose sends) so we exit
    # 0 instead of hanging out the grace period and getting SIGKILLed (137).
    def _shutdown(signum, frame):
        print("shutting down…", flush=True)
        # serve_forever() is blocking in the main thread; stop it from a helper
        # thread so this handler returns promptly.
        import threading
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        srv.serve_forever()
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
