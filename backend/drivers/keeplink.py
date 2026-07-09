"""Keeplink (Realtek-based) web-smart switch over HTTP.

These cheap managed switches have no API or SSH — just an HTML web UI whose
login is a cookie set to md5(username + password). Authenticated CGI pages
expose the data; we read the MAC forwarding table (/mac.cgi?page=fwd_tbl),
whose rows are [VLAN, MAC, FID, type, PORT], to derive learned-MAC and
active-port counts.
"""
import re
import time

from .base import Driver, Entity, SENSOR
from .registry import register

_MAC_RE = re.compile(
    r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# port.cgi state rows: Port N | state | (link) | actual-speed.
# <td> matched with optional attributes so firmware variants still parse.
_PORT_RE = re.compile(
    r"<td[^>]*>(Port \d+)</td>\s*<td[^>]*>(\w+)</td>\s*<td[^>]*>[^<]*</td>"
    r"\s*<td[^>]*>([^<]+)</td>", re.IGNORECASE)
# pse_port.cgi PoE rows: Port N | state | on | type | power | voltage | current
_POE_RE = re.compile(
    r"<td[^>]*>(Port \d+)</td>\s*<td[^>]*>(\w+)</td>\s*<td[^>]*>(\w+)</td>\s*"
    r"<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*"
    r"<td[^>]*>([^<]*)</td>", re.IGNORECASE)
_STAT_RE = re.compile(r"id=port(\d+)-(txgood|rxgood|txbad|rxbad)>([\d-]+)<")
_POE_TOTAL_RE = re.compile(r'name="pse_con_pwr"\s+value="([\d.]+)"')
_FW_RE = re.compile(r"Firmware Version.*?<td[^>]*>(.*?)</td>", re.DOTALL)


def _f(s):
    try:
        return float(str(s).strip())
    except Exception:
        return 0.0


def _pv(s):
    """Realtek counters come as 'hi-lo' 32-bit halves (or a plain int)."""
    parts = s.split("-")
    try:
        return int(parts[0]) * 4294967296 + int(parts[1]) if len(parts) == 2 \
            else int(s)
    except Exception:
        return 0


def _pnum(name):
    m = re.search(r"\d+", name or "")
    return int(m.group()) if m else 0


def _fwd_table(conn):
    """Authenticate (md5 cookie) and fetch the MAC forwarding table page."""
    conn.login_md5_cookie("admin")
    conn.session.headers["Referer"] = conn.base_url + "/menu.cgi"
    return conn.get("/mac.cgi", params={"page": "fwd_tbl"})


def _parse(resp):
    """Return (mac_set, port_set) parsed from a fwd_tbl page."""
    text = resp.text or ""
    macs, ports = set(), set()
    for row in _ROW_RE.findall(text):
        m = _MAC_RE.search(row)
        if not m:
            continue
        macs.add(m.group(0).upper())
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row)]
        # PORT is the last small-integer cell (<=48); other numeric columns
        # (VLAN/FID) can be larger.
        port = None
        h = re.search(r"[Pp]ort\s*(\d+)", row)
        if h:
            port = int(h.group(1))
        else:
            for cell in reversed(cells):
                if re.fullmatch(r"\d{1,2}", cell) and 0 <= int(cell) <= 48:
                    port = int(cell)
                    break
        if port is not None:
            ports.add(port)
    return macs, ports


def _snapshot(conn):
    """Log in (md5 cookie) and fetch the pages the rich view needs, cached
    briefly on the conn so entities() + detail() share one round of requests."""
    cached = getattr(conn, "_kl_snap", None)
    if cached and (time.time() - cached[0]) < 5:
        return cached[1]
    conn.login_md5_cookie("admin")
    conn.session.headers["Referer"] = conn.base_url + "/menu.cgi"

    def g(path, **params):
        try:
            return conn.get(path, params=params or None).text or ""
        except Exception:
            return ""

    snap = {
        "port": g("/port.cgi"),
        "poe_port": g("/pse_port.cgi"),
        "poe_sys": g("/pse_system.cgi"),
        "stats": g("/port.cgi", page="stats"),
        "mac": g("/mac.cgi", page="fwd_tbl"),
        "info": g("/info.cgi"),
    }
    conn._kl_snap = (time.time(), snap)
    return snap


def _ports(snap):
    """Merge link state/speed, PoE, and packet counters into per-port rows."""
    speeds = {}
    for pname, state, actual in _PORT_RE.findall(snap["port"]):
        actual = actual.strip()
        speeds[pname] = {"state": state, "speed": actual,
                         "up": actual != "Link Down"}
    poe = {}
    for pname, _st, on, ptype, power, _v, _c in _POE_RE.findall(snap["poe_port"]):
        poe[pname] = {"on": on == "On", "type": ptype.strip(), "power": _f(power)}
    stats = {}
    for idx, kind, val in _STAT_RE.findall(snap["stats"]):
        name = f"Port {int(idx) + 1}"
        d = stats.setdefault(name, {"tx": 0, "rx": 0, "bad": 0})
        v = _pv(val)
        if kind == "txgood":
            d["tx"] = v
        elif kind == "rxgood":
            d["rx"] = v
        else:
            d["bad"] += v

    rows = []
    for name in sorted(set(speeds) | set(poe), key=_pnum):
        sp, pe, stt = speeds.get(name, {}), poe.get(name, {}), stats.get(name, {})
        if pe.get("on"):
            poe_txt = "On" + (f" {pe['power']:.1f}W" if pe.get("power") else "")
        elif pe:
            poe_txt = "Off"
        else:
            poe_txt = "–"
        rows.append({
            "port": name,
            "up": "Yes" if sp.get("up") else "No",
            "speed": sp.get("speed") or "–",
            "poe": poe_txt,
            "rx_pkts": stt.get("rx", 0),
            "tx_pkts": stt.get("tx", 0),
            "errors": stt.get("bad", 0),
        })
    return rows


def _mac_rows(snap):
    text = snap["mac"] or ""
    out, seen = [], set()
    for row in _ROW_RE.findall(text):
        m = _MAC_RE.search(row)
        if not m:
            continue
        mac = m.group(0).upper()
        if mac in seen:
            continue
        seen.add(mac)
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row)]
        midx = next((i for i, c in enumerate(cells) if _MAC_RE.search(c)), -1)
        after = cells[midx + 1:] if midx >= 0 else cells
        port, vlan, nums = "", "", []
        for c in after:
            if not port and re.search(r"port", c, re.IGNORECASE):
                port = c
            elif re.fullmatch(r"\d{1,4}", c) and 1 <= int(c) <= 4094:
                nums.append(c)
        if nums:
            vlan = nums[0]
            if not port and len(nums) >= 2:
                port = "Port " + nums[1]
        out.append({"mac": mac, "vlan": vlan or "–", "port": port or "–"})
    return out


def _poe_total(snap):
    m = _POE_TOTAL_RE.search(snap["poe_sys"] or "")
    return _f(m.group(1)) if m else None


def _firmware(snap):
    m = _FW_RE.search(snap["info"] or "")
    return m.group(1).strip() if m else None


class KeeplinkSwitch(Driver):
    id = "keeplink.switch"
    display_name = "Keeplink web-smart switch (HTTP)"
    transports = ["http"]

    def probe(self, conn) -> float:
        try:
            resp = _fwd_table(conn)
        except Exception:
            return 0.0
        if resp.status != 200:
            return 0.0
        text = resp.text or ""
        macs, _ports = _parse(resp)
        if macs:
            # Authenticated and returning the characteristic MAC table.
            return 0.8
        # The fwd_tbl page structure is present but empty / not yet learned,
        # or auth failed and we got the login page. Weakly positive only if it
        # still looks like the Keeplink MAC page.
        if "fwd_tbl" in text or "mac.cgi" in text:
            return 0.35
        return 0.0

    def entities(self, conn):
        def snap():
            return _snapshot(conn)

        def ports_up():
            return sum(1 for p in _ports(snap()) if p["up"] == "Yes")

        return [
            Entity("mac_count", "Learned MACs", SENSOR,
                   read=lambda: len(_mac_rows(snap()))),
            Entity("ports_up", "Ports up", SENSOR, read=ports_up),
            Entity("poe_total", "PoE draw", SENSOR, unit="W",
                   read=lambda: _poe_total(snap())),
            Entity("firmware", "Firmware", SENSOR,
                   read=lambda: _firmware(snap())),
        ]

    def clients(self, conn):
        snap = _snapshot(conn)
        rows = _mac_rows(snap)
        # A port that has learned many MACs is an uplink/trunk (or an AP
        # downlink) — it carries the whole upstream network, not one attached
        # device, and those MACs are already reported by the AP/other sources.
        # Keep only ports with a handful of MACs = directly-attached wired gear,
        # so the clients view isn't flooded with infrastructure/duplicate MACs.
        from collections import Counter
        counts = Counter(r["port"] for r in rows if r["port"] != "–")
        TRUNK = 4
        out = []
        for r in rows:
            port = r["port"]
            if port != "–" and counts[port] > TRUNK:
                continue
            where = " · ".join(x for x in (
                port if port != "–" else "",
                ("VLAN " + r["vlan"]) if r["vlan"] not in ("–", "") else "") if x)
            out.append({
                "mac": r["mac"], "ip": "", "hostname": "",
                "kind": "wired", "signal": None, "where": where,
            })
        return out

    def detail(self, conn) -> dict:
        snap = _snapshot(conn)
        tables = []
        ports = _ports(snap)
        if ports:
            tables.append({
                "title": f"Ports ({sum(1 for p in ports if p['up'] == 'Yes')} up)",
                "columns": [
                    {"key": "port", "label": "Port"},
                    {"key": "up", "label": "Up"},
                    {"key": "speed", "label": "Speed"},
                    {"key": "poe", "label": "PoE"},
                    {"key": "rx_pkts", "label": "Rx pkts"},
                    {"key": "tx_pkts", "label": "Tx pkts"},
                    {"key": "errors", "label": "Errors"},
                ],
                "rows": ports,
            })
        macs = _mac_rows(snap)
        if macs:
            tables.append({
                "title": f"Learned MACs ({len(macs)})",
                "columns": [
                    {"key": "mac", "label": "MAC"},
                    {"key": "vlan", "label": "VLAN"},
                    {"key": "port", "label": "Port"},
                ],
                "rows": macs,
            })
        return {"tables": tables}


register(KeeplinkSwitch())
