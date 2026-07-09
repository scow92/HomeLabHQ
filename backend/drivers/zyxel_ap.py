"""Zyxel WiFi access point (NWA / WAX / NAP series) over its web UI.

These APs have no public REST API — management is the `zysh-cgi` endpoint behind
the web login. You POST a form to log in (a CSRF cookie is handed out on the
first GET), then POST CLI commands to `/cgi-bin/zysh-cgi`; each reply embeds a
`zyshdata0 = [ {...} ];` JavaScript array literal we parse out. This is a
read-only view (writes/deauth need SSH on current firmware), which is all the
detail view needs.

Ported from the private NAC's proven AP integration: the login flow and the
`show ...` command set are the same, wrapped here in the curated-driver shape so
it slots into detection, polling (scalar sensors) and the rich detail() view
(radios + a connected-clients table).
"""
import ast
import re
import time

from .base import Driver, Entity, SENSOR
from .registry import register

_ZYSHDATA_RE = re.compile(r"zyshdata0\s*=\s*(\[.*?\]);", re.DOTALL)
_SNAP_TTL = 5  # seconds; one detail/poll fans out to ~7 commands, share them


def _parse(text):
    """Extract and eval the zyshdata0 array literal from a zysh-cgi reply."""
    m = _ZYSHDATA_RE.search(text or "")
    if not m:
        return None
    try:
        return ast.literal_eval(m.group(1))
    except Exception:
        return None


def _login(conn):
    """Log into the AP web UI on conn's session. Best-effort: real proof of a
    working session is a subsequent zysh command that parses."""
    conn.get("/")  # hands out the csrftok cookie
    csrf = ""
    try:
        csrf = conn.session.cookies.get("csrftok", "") or ""
    except Exception:
        pass
    try:
        conn.request("POST", "/", allow_redirects=False,
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     data={"username": conn.username or "", "pwd": conn.password or "",
                           "password": conn.password or "", "CSRFToken": csrf})
    except Exception:
        pass


def _zysh(conn, cmd):
    """POST a single CLI command and return its parsed zyshdata list (or None)."""
    r = conn.request("POST", "/cgi-bin/zysh-cgi",
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     data={"filter": "js2", "cmd": cmd})
    return _parse(r.text)


def _snapshot(conn):
    """Log in (once) and read every field the driver needs in one shot, cached
    briefly on the connection so entities() + detail() don't re-run commands."""
    cached = getattr(conn, "_zyxel_snap", None)
    if cached and (time.time() - cached[0]) < _SNAP_TTL:
        return cached[1]

    _login(conn)

    def first(cmd):
        try:
            d = _zysh(conn, cmd)
            return d[0] if d else {}
        except Exception:
            return {}

    def index(cmd, key):
        try:
            d = _zysh(conn, cmd)
            return (d[0].get(key) if d else None) or []
        except Exception:
            return []

    snap = {
        "version": first("show version"),
        "uptime": first("show system uptime"),
        "cpu": first("show cpu status"),
        "mem": first("show mem status"),
        "clients": first("show wireless-hal station number"),
        "channel": first("show wireless-hal current channel"),
        "stations": index("show wireless-hal station info", "_index"),
    }
    conn._zyxel_snap = (time.time(), snap)
    return snap


def _pct(raw):
    """'12 %' / '45%' / 12 -> 12 (int), or None."""
    if raw is None:
        return None
    m = re.search(r"-?\d+", str(raw))
    return int(m.group(0)) if m else None


def _model(snap):
    return (snap["version"].get("_model") or "").strip()


def _fw(snap):
    return (snap["version"].get("_firmware_version") or "").strip()


class ZyxelAP(Driver):
    id = "zyxel.ap"
    display_name = "Zyxel WiFi access point (web UI)"
    transports = ["http"]

    def probe(self, conn) -> float:
        try:
            snap = _snapshot(conn)
        except Exception:
            return 0.0
        # Only Zyxel's zysh-cgi emits the zyshdata0 literal we just parsed, so a
        # parse of any command is already a strong fingerprint.
        parsed_anything = any(snap[k] for k in ("version", "cpu", "channel")) \
            or bool(snap["stations"])
        if not parsed_anything:
            return 0.0
        return 0.9 if _model(snap) else 0.7

    def entities(self, conn):
        def snap():
            return _snapshot(conn)

        def model():
            return _model(snap()) or None

        def firmware():
            return _fw(snap()) or None

        def uptime():
            return (snap()["uptime"].get("_system_uptime") or "").strip() or None

        def cpu():
            return _pct(snap()["cpu"].get("_CPU_utilization"))

        def mem():
            return _pct(snap()["mem"].get("_memory_usage"))

        def clients_24():
            return _pct(snap()["clients"].get("_Slot1"))

        def clients_5():
            return _pct(snap()["clients"].get("_Slot2"))

        def clients_total():
            a, b = clients_24(), clients_5()
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        def channel_24():
            return snap()["channel"].get("_Slot1") or None

        def channel_5():
            return snap()["channel"].get("_Slot2") or None

        return [
            Entity("model", "Model", SENSOR, read=model),
            Entity("firmware", "Firmware", SENSOR, read=firmware),
            Entity("uptime", "Uptime", SENSOR, read=uptime),
            Entity("cpu", "CPU", SENSOR, unit="%", read=cpu),
            Entity("mem", "Memory", SENSOR, unit="%", read=mem),
            Entity("clients", "Clients", SENSOR, read=clients_total),
            Entity("clients_24", "Clients 2.4 GHz", SENSOR, read=clients_24),
            Entity("clients_5", "Clients 5 GHz", SENSOR, read=clients_5),
            Entity("channel_24", "Channel 2.4 GHz", SENSOR, read=channel_24),
            Entity("channel_5", "Channel 5 GHz", SENSOR, read=channel_5),
        ]

    def detail(self, conn) -> dict:
        # Overview fields (model/firmware/uptime/cpu/mem/clients/channels) are
        # exposed as entities, so the detail view renders them from there and
        # lets the user customize which show. detail() only adds the structured
        # tables entities can't express: the per-radio and per-client breakdowns.
        snap = _snapshot(conn)
        c24 = _pct(snap["clients"].get("_Slot1"))
        c5 = _pct(snap["clients"].get("_Slot2"))
        ch24 = snap["channel"].get("_Slot1") or "–"
        ch5 = snap["channel"].get("_Slot2") or "–"

        radios = {
            "title": "Radios",
            "columns": [
                {"key": "band", "label": "Band"},
                {"key": "channel", "label": "Channel"},
                {"key": "clients", "label": "Clients"},
            ],
            "rows": [
                {"band": "2.4 GHz", "channel": ch24, "clients": c24 or 0},
                {"band": "5 GHz", "channel": ch5, "clients": c5 or 0},
            ],
        }

        rows = []
        for st in snap["stations"]:
            rows.append({
                "mac": (st.get("_MAC") or "").upper(),
                "band": st.get("_Band") or "",
                "ssid": st.get("_SSID") or "",
                "phy": st.get("_Capability") or "",
                "rssi": st.get("_RSSI_dBm"),
                "tx": st.get("_TxRate") or "",
                "rx": st.get("_RxRate") or "",
            })
        clients = {
            "title": f"Connected clients ({len(rows)})",
            "columns": [
                {"key": "mac", "label": "MAC"},
                {"key": "band", "label": "Band"},
                {"key": "ssid", "label": "SSID"},
                {"key": "phy", "label": "PHY"},
                {"key": "rssi", "label": "Signal", "unit": "dBm"},
                {"key": "tx", "label": "Tx"},
                {"key": "rx", "label": "Rx"},
            ],
            "rows": rows,
        }
        return {"tables": [radios, clients]}


register(ZyxelAP())
