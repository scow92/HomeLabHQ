#!/usr/bin/env python3
"""HomelabHQ backend — threading HTTP server.

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
import dashboards
import detect
import transports
import poller
import drivers  # noqa: F401  # importing self-registers all bundled drivers
from drivers import registry

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.environ.get("HLHQ_WEB_DIR", os.path.join(HERE, "..", "web"))
PORT = int(os.environ.get("HLHQ_PORT", "8770"))

# Companion plain-HTTP port used ONLY to serve the Home-Screen icons, and only
# when the origin is HTTPS with a self-signed cert. iOS fetches the
# apple-touch-icon through IconServices, which won't validate a self-signed cert
# even after you've trusted it in Safari — so an HTTPS-only icon silently fails
# to install and the phone shows a blank/generic tile. Serving the icon over
# plain HTTP takes cert validation out of that fetch. A real/trusted cert needs
# none of this and serves icons over HTTPS as usual. Set 0 to disable.
ICON_HTTP_PORT = int(os.environ.get("HLHQ_ICON_HTTP_PORT", "8771"))

# Public icon assets safe to expose over plain HTTP (basenames under WEB_DIR).
ICON_ASSETS = frozenset({
    "apple-touch-icon.png", "apple-touch-icon-precomposed.png",
    "icon-192.png", "icon-512.png", "icon-maskable-512.png",
    "icon-mark.svg", "favicon-32.png",
})

# Cache-buster appended to the apple-touch-icon URL. iOS caches Home-Screen
# icons system-wide keyed by URL, so a regenerated icon at the same URL keeps
# showing the stale tile even after remove + re-add. Derived from the icon
# file's mtime so it bumps automatically whenever the icon is re-rendered.
try:
    ICON_VER = str(int(os.path.getmtime(
        os.path.join(WEB_DIR, "apple-touch-icon.png"))))
except OSError:
    ICON_VER = "1"

# Set true in main() when serving HTTPS, so session cookies get the Secure flag.
TLS_ENABLED = False

# Set in main(): True only when serving HTTPS with a generated self-signed cert.
# Gates the plain-HTTP icon workaround above.
SELF_SIGNED = False


def _tls_requested():
    if os.environ.get("HLHQ_TLS_CERT") and os.environ.get("HLHQ_TLS_KEY"):
        return True
    return os.environ.get("HLHQ_TLS", "").lower() in ("1", "true", "yes", "auto")

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


def _valid_dashboard(user, dash_id):
    """None/empty is always valid (Unassigned); otherwise the dashboard must
    exist and be owned by the user (admins: any)."""
    if not dash_id:
        return True
    dash = dashboards.get(dash_id)
    return bool(dash) and _owns(user, dash)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "HomelabHQ/0.1"

    # ---- plumbing -----------------------------------------------------------
    def log_message(self, *a):
        pass  # keep the console quiet; add real logging later

    def _client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _send_json(self, code, obj, extra_headers=None, head=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        if not head:
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
        # The server's TLS certificate, offered for download so it can be
        # installed + trusted on a device (iOS requires this before it will
        # accept web push from a self-signed origin). It's public material.
        if path in ("/homelabhq.crt", "/nac.crt"):
            return self._serve_cert()
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._serve_static(path)

    def do_HEAD(self):
        # Mirror do_GET for the resources that answer HEAD meaningfully (static
        # assets, the cert, healthz). Some clients and crawlers probe an icon
        # with HEAD before GET; returning 501 broke them. API reads aren't
        # exposed over HEAD — answer with headers only.
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send_json(200, {"ok": True}, head=True)
        if path in ("/homelabhq.crt", "/nac.crt"):
            return self._serve_cert(head=True)
        if path.startswith("/api/"):
            return self._send_json(405, {"error": "method not allowed"},
                                   head=True)
        return self._serve_static(path, head=True)

    def _serve_cert(self, head=False):
        try:
            import tls
            certfile, _ = tls.ensure_cert()
            with open(certfile, "rb") as f:
                data = f.read()
        except Exception as e:
            return self._send_json(500, {"error": f"no certificate: {e}"},
                                   head=head)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Disposition",
                         "attachment; filename=homelabhq.crt")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

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

    def do_PATCH(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_patch(path)
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

        # /api/clients — aggregated network-wide client list (APs + switches).
        if path == "/api/clients":
            try:
                return self._send_json(200, devices.list_clients(
                    user["id"], is_admin=user["role"] == "admin"))
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        # /api/drivers?transport=<t> — curated drivers, optionally filtered to a
        # transport, so the UI can offer to re-point a mis-detected device.
        if path == "/api/drivers":
            t = (parse_qs(urlparse(self.path).query).get("transport") or [None])[0]
            drvs = registry.for_transport(t) if t else registry.all_drivers()
            return self._send_json(200, {"drivers": sorted(
                [{"id": d.id, "displayName": d.display_name,
                  "transports": d.transports} for d in drvs],
                key=lambda d: d["displayName"])})

        if path == "/api/dashboards":
            return self._send_json(200, {"dashboards": dashboards.list_dashboards(
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

        # /api/devices/<id>/series?metric=&id= — time-series behind a clickable
        # detail-table cell (e.g. a disk's temperature history).
        sr = _match(path, "/api/devices/", "/series")
        if sr:
            dev = devices.get_device(sr)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            metric = (q.get("metric") or [None])[0]
            ident = (q.get("id") or [None])[0]
            try:
                series = devices.read_series(sr, metric, ident)
                return self._send_json(200, {"metric": metric, "id": ident,
                                             "series": series})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        # /api/devices/<id>/firewall/all — every firewall rule, for the picker
        fa = _match(path, "/api/devices/", "/firewall/all")
        if fa:
            dev = devices.get_device(fa)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                return self._send_json(200, {"rules": devices.firewall_all(fa)})
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        # /api/devices/<id>/nac/interfaces — interfaces the NAC rule can attach to
        ni = _match(path, "/api/devices/", "/nac/interfaces")
        if ni:
            dev = devices.get_device(ni)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                return self._send_json(200, {"interfaces": devices.nac_interfaces(ni)})
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        # /api/devices/<id>/nac/aliases — existing firewall aliases (reuse picker)
        nal = _match(path, "/api/devices/", "/nac/aliases")
        if nal:
            dev = devices.get_device(nal)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                return self._send_json(200, {"aliases": devices.nac_aliases(nal)})
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        # /api/devices/<id>/detail — rich drill-down (overview + tables + history)
        d = _match(path, "/api/devices/", "/detail")
        if d:
            dev = devices.get_device(d)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                return self._send_json(200, devices.read_detail(d))
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
                res = push.notify({user["id"]}, "HomelabHQ test",
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
            drv = registry.get(body.get("driverId"))
            supports_binding = bool(getattr(drv, "supports_binding", False))
            nac_supported = bool(getattr(drv, "nac_supported", False))
            return self._send_json(200, {"entities": ents,
                                         "supportsBinding": supports_binding,
                                         "nacSupported": nac_supported})

        if path == "/api/devices":
            dash_id = body.get("dashboardId")
            if not _valid_dashboard(user, dash_id):
                return self._send_json(400, {"error": "unknown dashboard"})
            try:
                rec = devices.create_device(
                    owner_id=user["id"], host=body.get("host"),
                    transport=body.get("transport"), port=body.get("port"),
                    credentials=body.get("credentials"),
                    driver_id=body.get("driverId"), name=body.get("name"),
                    entities=body.get("entities"), dashboard_id=dash_id,
                    ap_binding=bool(body.get("apBinding")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            resp = {"device": rec}
            warn = rec.pop("bindingWarning", None)
            if warn:
                resp["bindingWarning"] = warn
            return self._send_json(200, resp)

        if path == "/api/dashboards":
            try:
                rec = dashboards.create(user["id"], body.get("name"))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"dashboard": rec})

        if path == "/api/devices/reorder":
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                return self._send_json(400, {"error": "ids must be a list"})
            n = devices.reorder(user["id"], ids,
                                is_admin=user["role"] == "admin")
            return self._send_json(200, {"reordered": n})

        # /api/devices/<id>/action — run a named driver action (e.g. force-roam)
        a = _match(path, "/api/devices/", "/action")
        if a:
            dev = devices.get_device(a)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                result = devices.run_action(
                    a, body.get("action"), body.get("args") or {})
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, result)

        # /api/devices/<id>/firewall/toggle — enable/disable one managed rule
        ft = _match(path, "/api/devices/", "/firewall/toggle")
        if ft:
            dev = devices.get_device(ft)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                result = devices.firewall_toggle(
                    ft, body.get("uuid"), bool(body.get("enabled")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, result)

        # /api/devices/<id>/firewall/rules — replace the managed rule list
        fr = _match(path, "/api/devices/", "/firewall/rules")
        if fr:
            dev = devices.get_device(fr)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                rules = devices.firewall_set_managed(fr, body.get("rules") or [])
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, {"rules": rules})

        # /api/devices/<id>/nac/setup — create the allow-list alias + rules
        ns = _match(path, "/api/devices/", "/nac/setup")
        if ns:
            dev = devices.get_device(ns)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                if body.get("mode") == "existing":
                    # Reuse a pre-existing alias (e.g. Network Manager's):
                    # membership-only, no rules created, nothing seeded.
                    rec = devices.nac_setup_existing(ns, body.get("existingUuid"))
                    return self._send_json(200, {"device": rec, "seeded": 0})
                seed = []
                if body.get("seedExisting"):
                    # Approve every currently-seen client so enabling default-deny
                    # later doesn't cut off existing devices.
                    try:
                        cl = devices.list_clients(user["id"],
                                                  is_admin=user["role"] == "admin")
                        seed = [c["mac"] for c in cl.get("clients", []) if c.get("mac")]
                    except Exception:
                        seed = []
                rec = devices.nac_setup(ns, body.get("alias"),
                                        body.get("interface"), seed)
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, {"device": rec, "seeded": len(seed)})

        # /api/devices/<id>/nac/approve — approve/revoke one client MAC
        na = _match(path, "/api/devices/", "/nac/approve")
        if na:
            dev = devices.get_device(na)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                result = devices.nac_approve(na, body.get("mac"),
                                             bool(body.get("approved")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, result)

        # /api/devices/<id>/nac/enforcement — master default-deny switch
        ne = _match(path, "/api/devices/", "/nac/enforcement")
        if ne:
            dev = devices.get_device(ne)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                rec = devices.nac_set_enforcement(ne, bool(body.get("enabled")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except transports.ConnectionError as e:
                return self._send_json(502, {"error": str(e)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(200, {"device": rec})

        # /api/nac/ignore — dismiss a client until it's seen again
        if path == "/api/nac/ignore":
            try:
                return self._send_json(200, devices.nac_ignore(body.get("mac")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})

        # /api/devices/<id>/binding — enable/disable roam-binding for this AP
        bg = _match(path, "/api/devices/", "/binding")
        if bg:
            dev = devices.get_device(bg)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                rec, warn = devices.set_ap_binding(bg, bool(body.get("enabled")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            if rec is None:
                return self._send_json(404, {"error": "not found"})
            resp = {"device": rec}
            if warn:
                resp["bindingWarning"] = warn
            return self._send_json(200, resp)

        # /api/devices/<id>/bind-client — lock/unlock a client MAC to this AP
        b = _match(path, "/api/devices/", "/bind-client")
        if b:
            dev = devices.get_device(b)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            try:
                rec = devices.set_client_binding(
                    b, body.get("mac"), bool(body.get("bound")))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            if rec is None:
                return self._send_json(404, {"error": "not found"})
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

        if path == "/api/dashboards":
            dash_id = (parse_qs(urlparse(self.path).query).get("id") or [None])[0]
            if not dash_id:
                return self._send_json(400, {"error": "id required"})
            dash = dashboards.get(dash_id)
            if not dash or not _owns(user, dash):
                return self._send_json(404, {"error": "not found"})
            dashboards.delete(dash_id)  # devices in it become unassigned
            return self._send_json(200, {"ok": True})

        return self._send_json(404, {"error": "not found"})

    # ---- API: PATCH ---------------------------------------------------------
    def _api_patch(self, path):
        user = self._current_user()
        if not user:
            return self._send_json(401, {"error": "unauthenticated"})
        body = self._read_json()

        # /api/devices/<id> — rename, move to a dashboard, or set enabled entities
        if path.startswith("/api/devices/"):
            dev_id = path[len("/api/devices/"):]
            if not dev_id or "/" in dev_id:
                return self._send_json(404, {"error": "not found"})
            dev = devices.get_device(dev_id)
            if not dev or not _owns(user, dev):
                return self._send_json(404, {"error": "not found"})
            kw = {}
            if "name" in body:
                kw["name"] = body.get("name")
            if "dashboardId" in body:
                if not _valid_dashboard(user, body.get("dashboardId")):
                    return self._send_json(400, {"error": "unknown dashboard"})
                kw["dashboard_id"] = body.get("dashboardId")
            if "entities" in body:
                kw["entities"] = body.get("entities")
            if "hiddenInterfaces" in body:
                kw["hidden_interfaces"] = body.get("hiddenInterfaces")
            if "driverId" in body:
                kw["driver_id"] = body.get("driverId")
            if "alerts" in body:
                kw["alerts"] = body.get("alerts")
            try:
                rec = devices.update_device(dev_id, **kw)
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            return self._send_json(200, {"device": rec})

        # /api/dashboards/<id> — rename / reorder
        if path.startswith("/api/dashboards/"):
            dash_id = path[len("/api/dashboards/"):] or None
            if not dash_id or "/" in dash_id:
                return self._send_json(404, {"error": "not found"})
            dash = dashboards.get(dash_id)
            if not dash or not _owns(user, dash):
                return self._send_json(404, {"error": "not found"})
            kw = {}
            if "name" in body:
                kw["name"] = body.get("name")
            if "order" in body:
                kw["order"] = body.get("order")
            rec = dashboards.update(dash_id, **kw)
            return self._send_json(200, {"dashboard": rec})

        return self._send_json(404, {"error": "not found"})

    # ---- static -------------------------------------------------------------
    def _serve_static(self, path, head=False):
        if path == "/" or not path:
            path = "/index.html"
        # normalize and prevent traversal outside WEB_DIR
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)):
            return self._send_json(403, {"error": "forbidden"}, head=head)
        if not os.path.isfile(full):
            # SPA fallback: serve index.html for client-side routes
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(full):
                return self._send_json(404, {"error": "not found"}, head=head)
        ext = os.path.splitext(full)[1].lower()
        ctype = _STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except Exception:
            return self._send_json(500, {"error": "read failed"}, head=head)
        if os.path.basename(full) == "index.html":
            data = self._rewrite_apple_icon(data)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _rewrite_apple_icon(self, data):
        """When the origin is HTTPS-with-self-signed, point the apple-touch-icon
        at the companion plain-HTTP port so iOS's IconServices can fetch it
        without hitting the untrusted cert (see ICON_HTTP_PORT). Host is taken
        from the request so nothing is hardcoded; no-op for trusted certs."""
        if not (SELF_SIGNED and ICON_HTTP_PORT):
            return data
        host = self.headers.get("Host", "").split(":")[0]
        if not host:
            return data
        base = f"http://{host}:{ICON_HTTP_PORT}".encode()
        ver = f"?v={ICON_VER}".encode()
        return data.replace(
            b'rel="apple-touch-icon" href="/apple-touch-icon.png"',
            b'rel="apple-touch-icon" href="' + base + b'/apple-touch-icon.png'
            + ver + b'"')


class _IconHandler(BaseHTTPRequestHandler):
    """Tiny plain-HTTP server for the Home-Screen icon assets only, so iOS can
    fetch the apple-touch-icon without validating the self-signed cert (see
    ICON_HTTP_PORT). Only the ICON_ASSETS whitelist is served; anything else
    301s to the real HTTPS origin."""
    server_version = "HomelabHQ/0.1"

    def log_message(self, *a):
        pass

    def _handle(self, head=False):
        name = os.path.basename(urlparse(self.path).path)
        full = os.path.join(WEB_DIR, name)
        if name in ICON_ASSETS and os.path.isfile(full):
            ext = os.path.splitext(full)[1].lower()
            ctype = _STATIC_TYPES.get(ext, "application/octet-stream")
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if not head:
                self.wfile.write(data)
            return
        host = self.headers.get("Host", "").split(":")[0] or "localhost"
        self.send_response(301)
        self.send_header("Location", f"https://{host}:{PORT}{self.path}")
        self.end_headers()

    def do_GET(self):
        self._handle(head=False)

    def do_HEAD(self):
        self._handle(head=True)


def main():
    # Never buffer stdout: container runtimes read logs from the pipe, and a
    # buffered "listening" line makes `docker compose up` look dead on start.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    global TLS_ENABLED, SELF_SIGNED
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
        SELF_SIGNED = tls.is_self_signed()
        scheme = "https"
        print(f"TLS: serving HTTPS using {certfile}"
              f"{' (self-signed)' if SELF_SIGNED else ''}", flush=True)

    print(f"HomelabHQ backend listening on {scheme}://0.0.0.0:{PORT}  "
          f"(data: {store.DATA_DIR})", flush=True)

    # Companion plain-HTTP icon listener — only needed for the self-signed case
    # so iOS can install the Home-Screen icon (see ICON_HTTP_PORT).
    if SELF_SIGNED and ICON_HTTP_PORT:
        import threading
        try:
            icon_srv = ThreadingHTTPServer(("0.0.0.0", ICON_HTTP_PORT),
                                           _IconHandler)
            threading.Thread(target=icon_srv.serve_forever,
                             daemon=True).start()
            print(f"Home-Screen icons also served over plain HTTP on "
                  f":{ICON_HTTP_PORT} (self-signed iOS workaround)", flush=True)
        except OSError as e:
            print(f"WARN: icon HTTP listener on :{ICON_HTTP_PORT} failed ({e}); "
                  f"iOS Home-Screen icon may not install", flush=True)

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
