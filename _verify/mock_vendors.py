"""One mock server standing in for OPNsense, pfSense, UniFi, Proxmox, Synology
and TrueNAS, so their drivers can be verified end-to-end. Routes by path and
checks each vendor's auth scheme. Plain HTTP on :443 (tests pass scheme=http)."""
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# credentials the drivers must present
OPN_KEY, OPN_SEC = "opnkey", "opnsecret"
PF_KEY = "pfkey"
UNIFI_KEY = "unifikey"
PVE_TOKEN = "PVEAPIToken=root@pam!mon=pvesecret"
TN_KEY = "tnkey"
SYNO_USER, SYNO_PASS, SYNO_SID = "admin", "synopass", "SYNOSID123"

R = {
    "/api/core/firmware/status": {"product": {"product_name": "OPNsense",
        "product_version": "24.7.5"}, "needs_reboot": "0",
        "upgrade_packages": [{"name": "openssl"}], "new_packages": []},
    "/api/v2/status/system": {"data": {"platform": "pfSense", "netgate_id": "abc123",
        "version": "2.7.2", "uptime": "3 days 04:15", "cpu_load": 7, "mem_usage": 34}},
    "/proxy/network/integration/v1/sites": {"offset": 0, "limit": 25, "count": 1,
        "totalCount": 1, "data": [{"id": "site1", "name": "Default"}]},
    "/proxy/network/integration/v1/sites/site1/devices": {"totalCount": 7,
        "data": [{"id": str(i)} for i in range(7)]},
    "/api2/json/version": {"data": {"version": "8.2.2", "release": "8.2", "repoid": "x"}},
    "/api2/json/nodes": {"data": [
        {"node": "pve1", "status": "online", "uptime": 123456},
        {"node": "pve2", "status": "online", "uptime": 98765},
        {"node": "pve3", "status": "offline", "uptime": 0}]},
    "/api/v2.0/system/info": {"version": "TrueNAS-SCALE-24.04.2", "hostname": "truenas",
        "uptime_seconds": 123456.7, "loadavg": [0.5, 0.4, 0.3], "physmem": 34359738368},
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _basic(self, u, p):
        a = self.headers.get("Authorization", "")
        if a.startswith("Basic "):
            try:
                du, dp = base64.b64decode(a[6:]).decode().split(":", 1)
                return du == u and dp == p
            except Exception:
                return False
        return False

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        h = self.headers

        if path.startswith("/api/core/"):          # OPNsense: Basic
            return self._send(200 if self._basic(OPN_KEY, OPN_SEC) else 401, R.get(path, {}))
        if path.startswith("/api/v2/"):             # pfSense: X-API-Key
            return self._send(200 if h.get("X-API-Key") == PF_KEY else 401, R.get(path, {}))
        if path.startswith("/proxy/network/"):      # UniFi: X-API-KEY
            return self._send(200 if h.get("X-API-KEY") == UNIFI_KEY else 401, R.get(path, {}))
        if path.startswith("/api2/json/"):          # Proxmox: Authorization token
            return self._send(200 if h.get("Authorization") == PVE_TOKEN else 401, R.get(path, {}))
        if path.startswith("/api/v2.0/"):           # TrueNAS: Bearer
            return self._send(200 if h.get("Authorization") == f"Bearer {TN_KEY}" else 401, R.get(path, {}))
        if path == "/webapi/auth.cgi":              # Synology: login by query
            ok = q.get("account") == [SYNO_USER] and q.get("passwd") == [SYNO_PASS]
            return self._send(200, {"success": ok, "data": {"sid": SYNO_SID} if ok else {}})
        if path == "/webapi/entry.cgi":             # Synology: info by sid
            if q.get("_sid") == [SYNO_SID]:
                return self._send(200, {"success": True, "data": {"model": "DS920+",
                    "firmware_ver": "DSM 7.2.1-69057", "up_time": "864000", "sys_temp": 42}})
            return self._send(200, {"success": False})
        return self._send(200, {})                  # '/' and unknown -> reachable


HTTPServer(("0.0.0.0", 443), H).serve_forever()
