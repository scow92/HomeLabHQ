"""Mock OpenWrt ubus endpoint for verifying the openwrt.ubus driver.

Speaks just enough JSON-RPC at /ubus: session login (checks username/password),
then system board / system info gated on the returned session token.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

USER, PASS = "root", "owrtpass"
SESSION = "abcdef0123456789abcdef0123456789"

BOARD = {"kernel": "6.1.0", "hostname": "OpenWrt-AP", "model": "TP-Link Archer C7",
         "board_name": "tplink,archer-c7-v2",
         "release": {"distribution": "OpenWrt", "version": "23.05.2",
                     "description": "OpenWrt 23.05.2 r23630"}}
INFO = {"uptime": 123456, "load": [9830, 8000, 7000],
        "memory": {"total": 256000000, "free": 128000000}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        params = req.get("params") or []
        result = [6]  # default: permission denied
        if len(params) >= 3:
            session, obj, method = params[0], params[1], params[2]
            args = params[3] if len(params) > 3 else {}
            if obj == "session" and method == "login":
                if args.get("username") == USER and args.get("password") == PASS:
                    result = [0, {"ubus_rpc_session": SESSION, "timeout": 300}]
            elif session == SESSION and obj == "system" and method == "board":
                result = [0, BOARD]
            elif session == SESSION and obj == "system" and method == "info":
                result = [0, INFO]
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1),
                           "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html><title>OpenWrt</title></html>")


HTTPServer(("0.0.0.0", 80), H).serve_forever()
