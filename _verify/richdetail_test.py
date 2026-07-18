"""Verify the rich detail() drivers (OPNsense / OpenWrt / Keeplink) and the
device reorder logic, against in-process mocks. Standalone:
`python3 _verify/richdetail_test.py` (no Docker, no pysnmp/cryptography needed).
"""
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/app/backend", os.path.join(HERE, "..", "backend")):
    if os.path.isdir(p):
        sys.path.insert(0, os.path.abspath(p))
try:
    import pysnmp  # noqa
except Exception:
    _m = types.ModuleType("snmp_backend"); _m.snmp = object()
    sys.modules.setdefault("snmp_backend", _m)
try:
    import cryptography  # noqa
except Exception:
    _c = types.ModuleType("crypto")
    _c.encrypt = lambda d: {"_id": dict(d)}; _c.decrypt = lambda b: dict(b["_id"])
    sys.modules.setdefault("crypto", _c)

import transports          # noqa: E402
import detect              # noqa: E402
import devices             # noqa: E402
from drivers import registry  # noqa: E402

fails = []
def ck(name, cond, got=None):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  (got {got!r})"))
    if not cond:
        fails.append(name)

def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ============================ OPNsense ======================================
OPS = {
    "/api/core/firmware/status": {"product": {"product_name": "OPNsense",
        "product_version": "24.7.5"}, "needs_reboot": "0",
        "upgrade_packages": [{"name": "openssl"}], "new_packages": []},
    "/api/diagnostics/system/systemResources": {"memory": {"total": "16000000000",
        "used": "4000000000"}},
    "/api/diagnostics/system/systemTime": {"uptime": "3 days 04:15",
        "loadavg": "0.52, 0.40, 0.30"},
    "/api/routes/gateway/status": {"items": [
        {"name": "WAN_GW", "address": "1.2.3.4", "status": "none",
         "status_translated": "Online", "delay": "5.2 ms", "loss": "0.0 %"},
        {"name": "WAN2", "status": "down", "status_translated": "Offline",
         "delay": "", "loss": "100.0 %"}]},
    "/api/diagnostics/cpu_usage/getCPUType": ["Intel N100 (4 cores, 4 threads)"],
    "/api/diagnostics/traffic/interface": {"interfaces": {
        "wan": {"device": "igc0", "bytes received": "1000000000", "bytes transmitted": "500000000"},
        "vlan10": {"device": "igc0.10", "bytes received": "10", "bytes transmitted": "10"},
        "loopback": {"device": "lo0", "bytes received": "9", "bytes transmitted": "9"}}},
    "/api/interfaces/overview/interfacesInfo": {"rows": [
        {"device": "igc0", "description": "WAN", "status": "up"},
        {"device": "igc1", "description": "LAN", "status": "up"}]},
}
class OpsH(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps(OPS.get(urlparse(self.path).path, {})).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)

_, ops_port = serve(OpsH)
ops_creds = {"apiKey": "k", "apiSecret": "s", "authStyle": "basic",
             "scheme": "http", "verifyTls": False}
res = detect.detect("api", "127.0.0.1", ops_port, ops_creds)
ck("OPNsense detected", res["candidates"] and res["candidates"][0]["driverId"] == "opnsense.firewall")
drv = registry.get("opnsense.firewall")
conn = transports.open_connection("api", "127.0.0.1", ops_port, ops_creds)
try:
    v = {e.key: (e.read() if e.read else None) for e in drv.entities(conn)}
    det = drv.detail(conn)
finally:
    conn.close()
ck("OPN mem_used ~25%", v.get("mem_used") == 25.0, v.get("mem_used"))
ck("OPN CPU normalizes load by core count", v.get("cpu") == 13.0, v.get("cpu"))
ck("OPN in_octets includes assigned VLANs but excludes loopback",
   v.get("in_octets") == 1000000010, v.get("in_octets"))
ck("OPN gateways_online=1", v.get("gateways_online") == 1, v.get("gateways_online"))
tbl = {t["title"].split(" (")[0]: t for t in det["tables"]}
ck("OPN Gateways table 2 rows", len(tbl["Gateways"]["rows"]) == 2)
ck("OPN Interfaces table 2 rows", len(tbl["Interfaces"]["rows"]) == 2)
ck("OPN iface rx humanized", tbl["Interfaces"]["rows"][0]["rx"].endswith("MB"),
   tbl["Interfaces"]["rows"][0]["rx"])


# ============================ OpenWrt =======================================
OW_USER, OW_PASS, OW_SESSION = "root", "owrtpass", "s" * 32
OW = {
    ("system", "board"): {"hostname": "OpenWrt-AP", "model": "GL-MT300",
        "release": {"distribution": "OpenWrt", "description": "OpenWrt 23.05"}},
    ("system", "info"): {"uptime": 12345, "load": [9830, 8000, 7000],
        "memory": {"total": 256000000, "free": 128000000}},
    ("network.device", "status"): {
        "eth0": {"up": True, "macaddr": "aa:bb:cc:dd:ee:ff",
                 "statistics": {"rx_bytes": 2000, "tx_bytes": 1000}},
        "lo": {"up": True, "statistics": {"rx_bytes": 5, "tx_bytes": 5}}},
}
class OwH(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            # Other http-transport drivers probe this host with non-JSON bodies
            # during detect(); answer benignly instead of crashing the handler.
            req = {}
        params = req.get("params") or []
        result = [6]
        if len(params) >= 3:
            session, obj, method = params[0], params[1], params[2]
            args = params[3] if len(params) > 3 else {}
            if obj == "session" and method == "login":
                if args.get("username") == OW_USER and args.get("password") == OW_PASS:
                    result = [0, {"ubus_rpc_session": OW_SESSION}]
            elif session == OW_SESSION and (obj, method) in OW:
                result = [0, OW[(obj, method)]]
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if urlparse(self.path).path == "/metrics":
            text = (
                "# HELP sfp_module_temperature_celsius SFP temp\n"
                "sfp_module_temperature_celsius{port=\"sfp1\"} 41.5\n"
                "sfp_module_voltage_volts{port=\"sfp1\"} 3.31\n"
                "sfp_tx_power_dbm{port=\"sfp1\"} -2.10\n"
                "node_network_receive_bytes_total{device=\"eth0\"} 12345\n").encode()
            self.send_response(200); self.send_header("Content-Length", str(len(text)))
            self.end_headers(); self.wfile.write(text); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"<title>OpenWrt</title>")

_, ow_port = serve(OwH)
ow_creds = {"username": OW_USER, "password": OW_PASS, "scheme": "http", "verifyTls": False}
res = detect.detect("http", "127.0.0.1", ow_port, ow_creds)
ck("OpenWrt detected", any(c["driverId"] == "openwrt.ubus" for c in res["candidates"]))
drv = registry.get("openwrt.ubus")
conn = transports.open_connection("http", "127.0.0.1", ow_port, ow_creds)
try:
    v = {e.key: (e.read() if e.read else None) for e in drv.entities(conn)}
    ifaces = drv.interfaces(conn)
    det = drv.detail(conn)
finally:
    conn.close()
ck("OW in_octets excludes lo", v.get("in_octets") == 2000, v.get("in_octets"))
ck("OW out_octets excludes lo", v.get("out_octets") == 1000, v.get("out_octets"))
ck("OW interfaces() 2 entries w/ raw counters",
   len(ifaces) == 2 and any(i["device"] == "eth0" and i["rx"] == 2000 for i in ifaces), ifaces)
iftab = next((t for t in det["tables"] if t.get("interfaces")), None)
ck("OW interfaces table tagged + 2 rows", iftab and len(iftab["rows"]) == 2, iftab)
eth0 = next((r for r in iftab["rows"] if r["device"] == "eth0"), {})
ck("OW eth0 up + rx humanized", eth0.get("status") == "up" and eth0.get("rx", "").endswith("KB"), eth0)
sfp = next((t for t in det["tables"] if t["title"].startswith("SFP")), None)
ck("OW SFP table from /metrics (3 rows, filtered)", sfp and len(sfp["rows"]) == 3, sfp)
ck("OW SFP port label", sfp and sfp["rows"][0]["port"] == "sfp1", sfp and sfp["rows"][0])


# ============================ Keeplink ======================================
KL_PAGES = {
    "/port.cgi": "<table>"
        "<tr><td>Port 1</td><td>Enable</td><td>Auto</td><td>1000M Full</td></tr>"
        "<tr><td>Port 2</td><td>Enable</td><td>Auto</td><td>Link Down</td></tr>"
        "</table>",
    "/pse_port.cgi": "<table>"
        "<tr><td>Port 1</td><td>Enable</td><td>On</td><td>802.3af</td>"
        "<td>3.5</td><td>54.0</td><td>65</td></tr></table>",
    "/pse_system.cgi": '<input name="pse_con_pwr" value="12.5">',
    "/port.cgi?stats": "id=port0-txgood>100< id=port0-rxgood>200< "
        "id=port0-txbad>0< id=port0-rxbad>3<",
    "/mac.cgi": "<table><tr><td>10</td><td>AA:BB:CC:DD:EE:01</td>"
        "<td>1</td><td>dynamic</td><td>Port 1</td></tr></table>",
    "/info.cgi": "<tr><td>Firmware Version</td><td>1.2.3-KL</td></tr>",
}
class KlH(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        key = u.path
        if u.path == "/port.cgi" and q.get("page") == ["stats"]:
            key = "/port.cgi?stats"
        body = KL_PAGES.get(key, "<html></html>").encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

_, kl_port = serve(KlH)
kl_creds = {"username": "admin", "password": "pw", "scheme": "http", "verifyTls": False}
res = detect.detect("http", "127.0.0.1", kl_port, kl_creds)
ck("Keeplink detected", any(c["driverId"] == "keeplink.switch" for c in res["candidates"]),
   [c["driverId"] for c in res["candidates"]])
drv = registry.get("keeplink.switch")
conn = transports.open_connection("http", "127.0.0.1", kl_port, kl_creds)
try:
    v = {e.key: (e.read() if e.read else None) for e in drv.entities(conn)}
    det = drv.detail(conn)
finally:
    conn.close()
ck("KL mac_count=1", v.get("mac_count") == 1, v.get("mac_count"))
ck("KL ports_up=1", v.get("ports_up") == 1, v.get("ports_up"))
ck("KL poe_total=12.5", v.get("poe_total") == 12.5, v.get("poe_total"))
ck("KL firmware", v.get("firmware") == "1.2.3-KL", v.get("firmware"))
klt = {t["title"].split(" (")[0]: t for t in det["tables"]}
ck("KL Ports table 2 rows", len(klt["Ports"]["rows"]) == 2)
p1 = klt["Ports"]["rows"][0]
ck("KL Port 1 PoE On + pkts", p1["poe"].startswith("On") and p1["rx_pkts"] == 200 and p1["errors"] == 3, p1)
ck("KL Learned MACs table 1 row", len(klt["Learned MACs"]["rows"]) == 1)


# ============================ reorder =======================================
dev_a = devices.create_device("u1", "10.0.0.1", "api", ops_port, ops_creds,
                              "opnsense.firewall", name="A")
dev_b = devices.create_device("u1", "10.0.0.2", "api", ops_port, ops_creds,
                              "opnsense.firewall", name="B")
order0 = [d["name"] for d in devices.list_devices("u1")]
ck("initial order A,B", order0 == ["A", "B"], order0)
devices.reorder("u1", [dev_b["id"], dev_a["id"]])
order1 = [d["name"] for d in devices.list_devices("u1")]
ck("reordered B,A", order1 == ["B", "A"], order1)
ck("other user can't reorder mine",
   devices.reorder("u2", [dev_a["id"], dev_b["id"]]) == 0)


# ==================== per-interface history + hidden ========================
import poller  # noqa: E402
ow_dev = devices.create_device("u1", "127.0.0.1", "http", ow_port, ow_creds,
                              "openwrt.ubus", name="switch")
pr = devices.poll_read(ow_dev["id"])
ck("poll_read returns interfaces", len(pr.get("interfaces", [])) == 2, pr.get("interfaces"))
poller.poll_once()  # records ifHistory for every device
det = devices.read_detail(ow_dev["id"])
ifh = det.get("ifHistory", {})
ck("ifHistory recorded eth0 rx", ifh.get("eth0", {}).get("rx"), list(ifh))
devices.update_device(ow_dev["id"], hidden_interfaces=["lo"])
ck("hiddenInterfaces persisted",
   devices.get_device(ow_dev["id"]).get("hiddenInterfaces") == ["lo"])
ck("hiddenInterfaces in public record",
   devices.read_detail(ow_dev["id"])["device"]["hiddenInterfaces"] == ["lo"])

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
