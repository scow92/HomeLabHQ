"""Mock MikroTik RouterOS REST endpoint for verifying the mikrotik.routeros
driver. GET /rest/system/resource requires HTTP Basic (admin/mtpass)."""
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

USER, PASS = "admin", "mtpass"
RESOURCE = {
    "architecture-name": "arm", "board-name": "RB750Gr3", "cpu-load": "5",
    "free-memory": "200000000", "total-memory": "268435456",
    "uptime": "1w2d3h4m5s", "version": "7.13.2", "platform": "MikroTik",
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        a = self.headers.get("Authorization", "")
        if a.startswith("Basic "):
            try:
                u, p = base64.b64decode(a[6:]).decode().split(":", 1)
                return u == USER and p == PASS
            except Exception:
                return False
        return False

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Basic realm=RouterOS")
            self.end_headers()
            return
        body = (json.dumps(RESOURCE).encode()
                if self.path == "/rest/system/resource" else b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 443), H).serve_forever()
