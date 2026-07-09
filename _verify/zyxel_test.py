"""End-to-end check for the Zyxel AP driver against an in-process mock.

Stands up a tiny HTTP server that mimics the AP web UI — a CSRF cookie on GET,
a 302 on the login POST, and `zysh-cgi` replies carrying the `zyshdata0 = [...]`
literal for each `show ...` command — then drives the real transport + driver
through detect -> entities -> detail.

Runs standalone: `python3 _verify/zyxel_test.py` (no Docker needed).
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/app/backend", os.path.join(HERE, "..", "backend")):
    if os.path.isdir(p):
        sys.path.insert(0, os.path.abspath(p))

# The http/Zyxel path never touches SNMP, but transports imports snmp_backend
# (-> pysnmp) at module load. Stub it so this test runs without pysnmp present.
try:
    import pysnmp  # noqa: F401
except Exception:
    import types
    _stub = types.ModuleType("snmp_backend")
    _stub.snmp = object()
    sys.modules.setdefault("snmp_backend", _stub)

import detect        # noqa: E402
import transports    # noqa: E402
from drivers import registry  # noqa: E402
import drivers        # noqa: F401,E402  # self-registers zyxel.ap

USER, PASS = "admin", "zyxelpass"

STATIONS = [
    {"_MAC": "aa:bb:cc:dd:ee:01", "_Band": "2.4GHz", "_SSID": "Home",
     "_Capability": "11ax", "_RSSI_dBm": -58, "_TxRate": "286M", "_RxRate": "286M", "_VapIdx": "1"},
    {"_MAC": "aa:bb:cc:dd:ee:02", "_Band": "5GHz", "_SSID": "Home-5G",
     "_Capability": "11ac", "_RSSI_dBm": -47, "_TxRate": "866M", "_RxRate": "780M", "_VapIdx": "2"},
]

# command string -> the zyshdata0 list literal it returns
CMDS = {
    "show version": [{"_model": "NWA50AX", "_firmware_version": "6.29(ABYW.3)",
                      "_system_name": "AP-Office"}],
    "show system uptime": [{"_system_uptime": "12:34:56"}],
    "show cpu status": [{"_CPU_utilization": "7 %"}],
    "show mem status": [{"_memory_usage": "41%"}],
    "show wireless-hal station number": [{"_Slot1": "3", "_Slot2": "5"}],
    "show wireless-hal current channel": [{"_Slot1": "6", "_Slot2": "36"}],
    "show wireless-hal station info": [{"_index": STATIONS}],
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Set-Cookie", "csrftok=tok123; Path=/")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>Zyxel</title><body>login</body></html>")

    def do_POST(self):
        path = urlparse(self.path).path
        form = parse_qs(self._body())
        if path == "/":
            ok = form.get("username") == [USER] and (
                form.get("pwd") == [PASS] or form.get("password") == [PASS])
            self.send_response(302 if ok else 200)
            if ok:
                self.send_header("Set-Cookie", "authtok=OK; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path == "/cgi-bin/zysh-cgi":
            authed = "authtok=OK" in (self.headers.get("Cookie") or "")
            cmd = (form.get("cmd") or [""])[0].strip()
            if authed and cmd in CMDS:
                payload = f"zyshdata0 = {CMDS[cmd]!r};"
            else:
                payload = "loginpage"  # no zyshdata -> parse fails, as on the real AP
            body = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


fails = []


def check(name, cond, got=None):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   (got: {got!r})"))
    if not cond:
        fails.append(name)


srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

CREDS = {"username": USER, "password": PASS, "scheme": "http", "verifyTls": False}

# ---- 1. detection recognises the AP -----------------------------------------
res = detect.detect("http", "127.0.0.1", port, CREDS)
top = res["candidates"][0] if res["candidates"] else {}
check("zyxel.ap is the top candidate", top.get("driverId") == "zyxel.ap", top)
check("confidence is high (model parsed)", top.get("confidence", 0) >= 0.9, top)

# ---- 2. wrong password does not authenticate --------------------------------
bad = detect.detect("http", "127.0.0.1", port,
                    {**CREDS, "password": "nope"})
bad_top = bad["candidates"][0]["driverId"] if bad["candidates"] else None
check("bad password -> zyxel.ap not detected", bad_top != "zyxel.ap", bad_top)

# ---- 3. entities read live scalar sensors -----------------------------------
ents = detect.enumerate_entities("http", "127.0.0.1", port, CREDS, "zyxel.ap")
keys = {e["key"] for e in ents}
check("entities expose model/cpu/clients/uptime",
      {"model", "cpu", "clients", "uptime", "channel_5"} <= keys, keys)

drv = registry.get("zyxel.ap")
conn = transports.open_connection("http", "127.0.0.1", port, CREDS)
try:
    vals = {e.key: (e.read() if e.read else None) for e in drv.entities(conn)}
finally:
    conn.close()
check("model reads NWA50AX", vals.get("model") == "NWA50AX", vals.get("model"))
check("cpu reads 7", vals.get("cpu") == 7, vals.get("cpu"))
check("mem reads 41", vals.get("mem") == 41, vals.get("mem"))
check("clients total 3+5=8", vals.get("clients") == 8, vals.get("clients"))
check("channel_5 reads 36", vals.get("channel_5") == "36", vals.get("channel_5"))

# ---- 4. detail() returns overview + radios + clients tables -----------------
conn = transports.open_connection("http", "127.0.0.1", port, CREDS)
try:
    det = drv.detail(conn)
finally:
    conn.close()
tables = {t["title"].split(" (")[0]: t for t in det.get("tables", [])}
# Overview identity (model/clients/…) is exposed via entities now, not detail.info,
# so the detail view can render + customize it; detail() only carries tables.
check("detail() carries no duplicate info block", not det.get("info"), det.get("info"))
check("Radios table present with 2 rows",
      "Radios" in tables and len(tables["Radios"]["rows"]) == 2, list(tables))
clients_t = tables.get("Connected clients")
check("Connected clients table has 2 rows",
      clients_t and len(clients_t["rows"]) == 2, clients_t and len(clients_t["rows"]))
check("client row carries MAC + RSSI",
      clients_t and clients_t["rows"][0]["mac"] == "AA:BB:CC:DD:EE:01"
      and clients_t["rows"][0]["rssi"] == -58,
      clients_t and clients_t["rows"][0])

srv.shutdown()
print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
