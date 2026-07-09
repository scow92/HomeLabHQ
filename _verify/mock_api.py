"""Tiny mock device API for verifying the 'api' transport.

GET / requires either HTTP Basic (key=KEY, secret=SECRET) OR the headers
X-API-Key: KEY and X-API-Secret: SECRET. On success returns JSON; otherwise 401.
"""
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY, SECRET = "abc123key", "s3cr3t-value"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                u, p = base64.b64decode(auth[6:]).decode().split(":", 1)
                if u == KEY and p == SECRET:
                    return True
            except Exception:
                pass
        if (self.headers.get("X-API-Key") == KEY and
                self.headers.get("X-API-Secret") == SECRET):
            return True
        return False

    def do_GET(self):
        if not self._ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Basic realm=device")
            self.end_headers()
            return
        body = b'{"name":"mock-firewall","version":"1.2.3","status":"ok","ports":8}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Server", "MockDeviceAPI/1.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 9000), H).serve_forever()
