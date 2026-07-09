"""Keeplink (Realtek-based) web-smart switch over HTTP.

These cheap managed switches have no API or SSH — just an HTML web UI whose
login is a cookie set to md5(username + password). Authenticated CGI pages
expose the data; we read the MAC forwarding table (/mac.cgi?page=fwd_tbl),
whose rows are [VLAN, MAC, FID, type, PORT], to derive learned-MAC and
active-port counts.
"""
import re

from .base import Driver, Entity, SENSOR
from .registry import register

_MAC_RE = re.compile(
    r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


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
        def _table():
            return _parse(_fwd_table(conn))

        def reachable():
            return _fwd_table(conn).status < 400

        def mac_count():
            return len(_table()[0])

        def active_ports():
            return len(_table()[1])

        return [
            Entity("reachable", "Reachable", SENSOR, read=reachable),
            Entity("mac_count", "Learned MACs", SENSOR, read=mac_count),
            Entity("active_ports", "Active ports", SENSOR, read=active_ports),
        ]


register(KeeplinkSwitch())
