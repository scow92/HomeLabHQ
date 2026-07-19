#!/usr/bin/env python3
"""HomelabHQ startup wiring for the standard-library HTTP server."""
import os
import signal
import sys
import time
from pathlib import Path

# ``backend/http`` intentionally follows the Phase 3 layout.  When this file
# is executed directly, keep its directory off the front of sys.path while the
# standard-library ``http`` package is imported, then retain direct imports
# used by the pre-existing backend modules.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:] = [entry for entry in sys.path
               if os.path.abspath(entry or os.curdir) != HERE]
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from http.server import BaseHTTPRequestHandler
if HERE not in sys.path:
    sys.path.insert(1, HERE)

import api
import history
import logbuf
import poller
import store
from backend.http.handler import Handler
from backend.http.router import Router
from backend.http.hq_server import ThreadingHTTPServer
from backend.http.static import STATIC_TYPES

import drivers  # noqa: F401  # importing self-registers bundled drivers

WEB_DIR = os.environ.get("HLHQ_WEB_DIR", os.path.join(HERE, "..", "web"))
PORT = int(os.environ.get("HLHQ_PORT", "8770"))
MAX_JSON_BODY_BYTES = max(1, int(os.environ.get("HLHQ_MAX_JSON_BODY_BYTES", "1048576")))
ICON_HTTP_PORT = int(os.environ.get("HLHQ_ICON_HTTP_PORT", "8771"))
ICON_ASSETS = frozenset({
    "apple-touch-icon.png", "apple-touch-icon-precomposed.png", "icon-192.png",
    "icon-512.png", "icon-maskable-512.png", "icon-mark.svg", "favicon-32.png",
})
TRUST_PROXY = os.environ.get("HLHQ_TRUST_PROXY", "").lower() in ("1", "true", "yes")

try:
    ICON_VER = str(int(os.path.getmtime(os.path.join(WEB_DIR, "apple-touch-icon.png"))))
except OSError:
    ICON_VER = "1"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'; object-src 'none'")


def _configure_handler():
    Handler.router = Router(api.all_routes())
    Handler.web_dir = WEB_DIR
    Handler.csp = CSP
    Handler.max_json_body_bytes = MAX_JSON_BODY_BYTES
    Handler.trust_proxy = TRUST_PROXY
    Handler.icon_http_port = ICON_HTTP_PORT
    Handler.icon_ver = ICON_VER


_configure_handler()


def _tls_requested():
    if os.environ.get("HLHQ_TLS_CERT") and os.environ.get("HLHQ_TLS_KEY"):
        return True
    return os.environ.get("HLHQ_TLS", "").lower() in ("1", "true", "yes", "auto")


class _IconHandler(BaseHTTPRequestHandler):
    """Companion plain-HTTP server for iOS icon fetches with self-signed TLS."""
    server_version = "HomelabHQ/0.1"

    def log_message(self, *args):
        pass

    def _handle(self, head=False):
        name = os.path.basename(self.path.split("?", 1)[0])
        full = Path(WEB_DIR) / name
        if name in ICON_ASSETS and full.is_file():
            data = full.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(full.suffix.lower(), "application/octet-stream"))
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
        self._handle()

    def do_HEAD(self):
        self._handle(head=True)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if not store.secrets_isolated_from_agents():
        credential_count = len(store.load().get("credentials", {}))
        if credential_count and not os.environ.get("HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS"):
            print(f"REFUSING TO START: {store.SECRETS_DIR} holds {credential_count} credential(s) "
                  "without container isolation. Set HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS=1 "
                  "only for local development.", file=sys.stderr, flush=True)
            sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    scheme = "http"
    if _tls_requested():
        import ssl
        import tls
        certfile, keyfile = tls.ensure_cert()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        Handler.tls_enabled = True
        Handler.self_signed = tls.is_self_signed()
        scheme = "https"
        print(f"TLS: serving HTTPS using {certfile}"
              f"{' (self-signed)' if Handler.self_signed else ''}", flush=True)

    print(f"HomelabHQ backend listening on {scheme}://0.0.0.0:{PORT}  (data: {store.DATA_DIR})",
          flush=True)
    logbuf.log_note("info", f"backend started on {scheme}://0.0.0.0:{PORT}", "startup")
    if Handler.self_signed and ICON_HTTP_PORT:
        import threading
        try:
            icon_server = ThreadingHTTPServer(("0.0.0.0", ICON_HTTP_PORT), _IconHandler)
            threading.Thread(target=icon_server.serve_forever, daemon=True).start()
        except OSError as error:
            print(f"WARN: icon HTTP listener on :{ICON_HTTP_PORT} failed ({error})", flush=True)

    history.migrate_from_store()
    poller.start()

    def shutdown(signum, frame):
        import threading
        print("shutting down…", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
