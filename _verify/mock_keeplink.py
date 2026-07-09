"""Mock Keeplink switch for verifying the 'http' transport + keeplink driver.

Reproduces the real device's observable behaviour (per the existing NAC's
scanner.py): the session cookie `admin` must equal md5(username+password), and
GET /mac.cgi?page=fwd_tbl then returns an HTML table whose rows are
[VLAN, MAC, FID, type, PORT]. Wrong/absent cookie -> a login page (no MAC rows).
"""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

USER, PASS = "admin", "switchpw"
EXPECT = hashlib.md5((USER + PASS).encode()).hexdigest()

# MACs on ports 1, 2, 3 and 9 (9 = uplink). -> 4 MACs, 4 active ports.
ROWS = [
    ("10", "AA:BB:CC:00:00:01", "1", "dynamic", "1"),
    ("10", "AA:BB:CC:00:00:02", "1", "dynamic", "2"),
    ("20", "AA:BB:CC:00:00:03", "1", "dynamic", "3"),
    ("10", "AA:BB:CC:00:00:09", "1", "dynamic", "9"),
]

LOGIN_PAGE = (b"<html><head><title>Login</title></head><body>"
              b"<form action='/login.cgi'>Password:</form></body></html>")


def _fwd_html():
    trs = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in ROWS)
    return (
        "<html><head><title>MAC Forwarding Table</title></head><body>"
        "<table><tr><th>VLAN</th><th>MAC</th><th>FID</th>"
        "<th>type</th><th>PORT</th></tr>" + trs +
        "</table></body></html>").encode()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cookie_ok(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "admin" and v == EXPECT:
                    return True
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/mac.cgi" and parse_qs(u.query).get("page") == ["fwd_tbl"]:
            body = _fwd_html() if self._cookie_ok() else LOGIN_PAGE
        else:
            body = (b"<html><head><title>Keeplink</title></head>"
                    b"<body>menu</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Server", "KeeplinkHTTP")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 80), H).serve_forever()
