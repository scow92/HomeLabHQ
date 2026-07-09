"""OpenWrt router/AP over ubus (HTTP JSON-RPC).

OpenWrt exposes ubus over HTTP at /ubus: you log in with username+password to
get a session token, then call objects like `system board` / `system info`.
That login is device-specific, so it lives here (over the generic `http`
transport) rather than in the transport. Identified with high confidence when
`system board` reports the OpenWrt distribution.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_NULL_SESSION = "00000000000000000000000000000000"


def _ubus(conn, session, obj, method, params=None):
    """One ubus call. Returns the [code, data] result list, or None."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "call",
               "params": [session, obj, method, params or {}]}
    try:
        r = conn.request("POST", "/ubus", json=payload)
    except Exception:
        return None
    data = r.json() or {}
    return data.get("result")


def _login(conn):
    res = _ubus(conn, _NULL_SESSION, "session", "login",
                {"username": conn.username or "", "password": conn.password or ""})
    if res and res[0] == 0:
        return (res[1] or {}).get("ubus_rpc_session")
    return None


class OpenWrtRouter(Driver):
    id = "openwrt.ubus"
    display_name = "OpenWrt router / AP (ubus)"
    transports = ["http"]

    def probe(self, conn) -> float:
        session = _login(conn)
        if not session:
            return 0.0
        board = _ubus(conn, session, "system", "board")
        if board and board[0] == 0:
            rel = (board[1].get("release") or {}).get("distribution", "")
            return 0.9 if "openwrt" in rel.lower() else 0.6
        return 0.3  # authenticated ubus but no board info

    def entities(self, conn):
        cache = {}

        def _session():
            if "s" not in cache:
                cache["s"] = _login(conn)
            return cache["s"]

        def _call(obj, method):
            key = obj + "." + method
            if key not in cache:
                res = _ubus(conn, _session(), obj, method)
                cache[key] = res[1] if res and res[0] == 0 else {}
            return cache[key]

        def board():
            return _call("system", "board")

        def info():
            return _call("system", "info")

        def hostname():
            return board().get("hostname")

        def model():
            return board().get("model")

        def release():
            return (board().get("release") or {}).get("description")

        def uptime():
            return info().get("uptime")

        def load1():
            load = info().get("load") or []
            return round(load[0] / 65536.0, 2) if load else None

        def mem_used_pct():
            mem = info().get("memory") or {}
            total, free = mem.get("total"), mem.get("free")
            if total:
                return round((total - free) / total * 100, 1)
            return None

        return [
            Entity("hostname", "Hostname", SENSOR, read=hostname),
            Entity("model", "Model", SENSOR, read=model),
            Entity("release", "OpenWrt release", SENSOR, read=release),
            Entity("uptime", "Uptime", SENSOR, unit="s", read=uptime),
            Entity("load1", "Load average (1m)", SENSOR, read=load1),
            Entity("mem_used", "Memory used", SENSOR, unit="%", read=mem_used_pct),
        ]


register(OpenWrtRouter())
