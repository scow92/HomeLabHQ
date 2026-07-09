"""OpenWrt router/AP over ubus (HTTP JSON-RPC).

OpenWrt exposes ubus over HTTP at /ubus: you log in with username+password to
get a session token, then call objects like `system board` / `system info`.
That login is device-specific, so it lives here (over the generic `http`
transport) rather than in the transport. Identified with high confidence when
`system board` reports the OpenWrt distribution.
"""
import re

from .base import Driver, Entity, SENSOR
from .registry import register

_NULL_SESSION = "00000000000000000000000000000000"

# Prometheus /metrics scrape (optional): some OpenWrt-flashed switches expose an
# exporter with SFP/optical module telemetry. We pull it best-effort and surface
# the SFP-related series as a table. Metric names/labels vary by exporter, so we
# match loosely and degrade to nothing when the page isn't there.
_SFP_HINT = re.compile(r"sfp|transceiver|xcvr|optic|dom_|laser|eeprom|module_temp", re.I)
_PROM_RE = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]?Inf)\s*(?:\d+)?$')
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_prom(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2) or "", m.group(3)))
    return out


def _label_summary(labels):
    d = dict(_LABEL_RE.findall(labels or ""))
    for k in ("port", "ifname", "name", "interface", "device", "module", "sfp"):
        if d.get(k):
            return d[k]
    return ", ".join(f"{k}={v}" for k, v in d.items())


def _metrics_tables(conn):
    path = getattr(conn, "metrics_path", None) or "/metrics"
    try:
        r = conn.get(path)
        text = r.text if r.status == 200 else ""
    except Exception:
        text = ""
    rows = []
    for name, labels, val in _parse_prom(text):
        if not (_SFP_HINT.search(name) or _SFP_HINT.search(labels)):
            continue
        try:
            v = float(val)
            vs = str(int(v)) if v == int(v) else f"{v:.2f}"
        except Exception:
            vs = val
        rows.append({"metric": name, "port": _label_summary(labels), "value": vs})
    if not rows:
        return []
    rows.sort(key=lambda r: (r["port"], r["metric"]))
    return [{
        "title": f"SFP / optics ({len(rows)})",
        "columns": [
            {"key": "metric", "label": "Metric"},
            {"key": "port", "label": "Port"},
            {"key": "value", "label": "Value"},
        ],
        "rows": rows,
    }]


def _fmt_speed(raw):
    """OpenWrt 'network.device status' reports speed as e.g. '10000F' (Mbit +
    duplex) or an int. Render it as a human rate; return '' when unknown/down."""
    if raw in (None, "", 0, "0"):
        return ""
    m = re.match(r"\s*(\d+)", str(raw))
    if not m:
        return str(raw)
    mbit = int(m.group(1))
    if mbit >= 1000 and mbit % 1000 == 0:
        return f"{mbit // 1000} Gbps"
    if mbit >= 1000:
        return f"{mbit / 1000:.1f} Gbps"
    return f"{mbit} Mbps"


def _ethtool_module(conn, session, port):
    """Best-effort SFP vendor/part from `ethtool -m <port>` over ubus file.exec.
    Returns '' when the exec object isn't permitted (rpcd acl) or ethtool is
    absent — the caller degrades to whatever `network.device status` exposed."""
    import base64
    res = _ubus(conn, session, "file", "exec",
                {"command": "/sbin/ethtool", "params": ["-m", port], "env": {}})
    if not res or res[0] != 0:
        return ""
    try:
        out = base64.b64decode((res[1] or {}).get("stdout", "")).decode(
            "utf-8", "replace")
    except Exception:
        return ""
    vend = re.search(r"Vendor name\s*:\s*(.+)", out, re.I)
    part = re.search(r"Vendor PN\s*:\s*(.+)", out, re.I)
    return " ".join(p.group(1).strip() for p in (vend, part) if p).strip()


# Physical port names to surface in the SFP/ports table (lanN/wanN/sfpN/ethN),
# skipping bridges/vlans/virtual devices which have no link speed.
_PHYS_RE = re.compile(r"^(lan|wan|sfp|eth|port)\d", re.I)


def _sfp_table(conn):
    """Per-physical-port link/speed/module table — the 'SFP details' view.

    Built from ubus `network.device status`: each port yields link state, the
    negotiated speed, and (when the firmware/optic exposes it) the transceiver
    vendor+part, either from the port's `sfp` object or an `ethtool -m` probe.
    A port with an optic module — or a fibre-class speed — is flagged Fibre.
    """
    session = _login(conn)
    if not session:
        return []
    res = _ubus(conn, session, "network.device", "status", {})
    devs = res[1] if res and res[0] == 0 and isinstance(res[1], dict) else {}
    rows = []
    for name in sorted(devs):
        d = devs[name]
        if not isinstance(d, dict) or not _PHYS_RE.match(name):
            continue
        up = bool(d.get("carrier") if "carrier" in d else d.get("up"))
        speed = _fmt_speed(d.get("speed")) if up else ""
        sfp = d.get("sfp") or {}
        module = " ".join(s for s in (
            (sfp.get("vendor_name") or "").strip(),
            (sfp.get("vendor_pn") or "").strip()) if s).strip()
        if not module and up:
            module = _ethtool_module(conn, session, name)
        mbit = 0
        mm = re.match(r"\s*(\d+)", str(d.get("speed") or ""))
        if mm:
            mbit = int(mm.group(1))
        ptype = "Fibre" if (sfp or module or mbit >= 10000) else (
            "Copper" if up else "–")
        rows.append({
            "port": name,
            "link": "Up" if up else "Down",
            "speed": speed or "–",
            "type": ptype,
            "module": module or "–",
        })
    if not rows:
        return []
    return [{
        "title": f"Ports / SFP ({sum(1 for r in rows if r['link'] == 'Up')} up)",
        "columns": [
            {"key": "port", "label": "Port"},
            {"key": "link", "label": "Link"},
            {"key": "speed", "label": "Speed"},
            {"key": "type", "label": "Type"},
            {"key": "module", "label": "Module"},
        ],
        "rows": rows,
    }]


def _hbytes(n):
    try:
        n = int(n)
    except Exception:
        return "–"
    units = ["B", "KB", "MB", "GB", "TB"]
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


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

        def netdev():
            # ubus network.device status (no name) returns {device: {...}}.
            return _call("network.device", "status")

        def _agg(field):
            # Sum only real front-panel ports. Skip loopback, bridges (br*),
            # VLAN sub-interfaces (contain a dot) and the 'switch' pseudo-device
            # — those carry the SAME frames as the physical ports, so counting
            # them would double- or triple-count the switch's traffic.
            total, seen = 0, False
            for name, d in (netdev() or {}).items():
                if not isinstance(d, dict):
                    continue
                n = name.lower()
                if n == "lo" or n.startswith(("br", "bond")) or "." in n \
                        or n == "switch":
                    continue
                st = d.get("statistics") or {}
                if field in st:
                    total += int(st.get(field) or 0)
                    seen = True
            return total if seen else None

        return [
            Entity("hostname", "Hostname", SENSOR, read=hostname),
            Entity("model", "Model", SENSOR, read=model),
            Entity("release", "OpenWrt release", SENSOR, read=release),
            Entity("uptime", "Uptime", SENSOR, unit="s", read=uptime),
            Entity("load1", "Load average (1m)", SENSOR, read=load1),
            Entity("mem_used", "Memory used", SENSOR, unit="%", read=mem_used_pct),
            Entity("in_octets", "Traffic in", SENSOR, unit="bytes",
                   read=lambda: _agg("rx_bytes")),
            Entity("out_octets", "Traffic out", SENSOR, unit="bytes",
                   read=lambda: _agg("tx_bytes")),
        ]

    def actions(self):
        return [{"name": "reboot", "label": "Reboot", "danger": True,
                 "confirm": True}]

    def run_action(self, conn, name, args):
        if name != "reboot":
            raise ValueError(f"unsupported action: {name}")
        session = _login(conn)
        if not session:
            raise ValueError("login failed")
        # ubus system reboot — schedules an orderly reboot on the device.
        res = _ubus(conn, session, "system", "reboot")
        if not res or res[0] != 0:
            raise ValueError("reboot call was rejected by the device")
        return {"ok": True, "message": "Reboot command sent to the router."}

    def interfaces(self, conn):
        session = _login(conn)
        res = _ubus(conn, session, "network.device", "status", {}) if session else None
        devs = res[1] if res and res[0] == 0 and isinstance(res[1], dict) else {}
        out = []
        for name, d in devs.items():
            if not isinstance(d, dict) or name.lower() == "lo":
                continue  # loopback carries no real traffic
            st = d.get("statistics") or {}
            out.append({
                "device": name,
                "name": name,
                "status": "up" if d.get("up") else "down",
                "mac": d.get("macaddr") or "–",
                "rx": int(st.get("rx_bytes") or 0) if "rx_bytes" in st else None,
                "tx": int(st.get("tx_bytes") or 0) if "tx_bytes" in st else None,
            })
        out.sort(key=lambda r: r["device"])
        return out

    def detail(self, conn) -> dict:
        ifaces = self.interfaces(conn)
        tables = []
        if ifaces:
            tables.append({
                "title": f"Interfaces ({len(ifaces)})",
                "interfaces": True,
                "idKey": "device",
                "columns": [
                    {"key": "device", "label": "Device"},
                    {"key": "status", "label": "Status"},
                    {"key": "mac", "label": "MAC"},
                    {"key": "rx", "label": "In"},
                    {"key": "tx", "label": "Out"},
                ],
                "rows": [{
                    "device": f["device"], "status": f["status"],
                    "mac": f["mac"], "rx": _hbytes(f["rx"]), "tx": _hbytes(f["tx"]),
                } for f in ifaces],
            })
        try:
            tables += _sfp_table(conn)
        except Exception:
            pass
        tables += _metrics_tables(conn)
        return {"tables": tables}


register(OpenWrtRouter())
