"""OPNsense firewall via its REST API (api transport, Basic key:secret).

Enter the OPNsense API **key as the API key** and **secret as the API secret**
(OPNsense uses HTTP Basic where key=user, secret=password). Identified from
/api/core/firmware/status.

Beyond firmware/update status this reads the same live picture the Network
Manager shows: system uptime/load/memory, gateway health, and per-interface
throughput — surfaced as pollable scalar sensors (incl. aggregate in/out byte
counters that chart as throughput) plus detail() tables for gateways and
interfaces. Parsing is defensive so minor OPNsense version differences degrade
to None/omitted rather than erroring.
"""
import re
import time

from netutil import is_private_ip

from .base import Driver, Entity, SENSOR
from .registry import register

_FW = "/api/core/firmware/status"
_ARP = "/api/diagnostics/interface/getArp"
_LEASES = "/api/dhcpv4/leases/searchLease"
_KEA = "/api/kea/leases4/search"
_RES = "/api/diagnostics/system/systemResources"
_TIME = "/api/diagnostics/system/systemTime"
# Static CPU description, e.g. "Intel(R) N100 (4 cores, 4 threads)" — we only
# read the core count from it, to turn the 1-minute load average into a CPU %.
_CPUTYPE = "/api/diagnostics/cpu_usage/getCPUType"
_GW = "/api/routes/gateway/status"
# NB: getInterfaceStatistics reports 0 bytes for VLAN sub-interfaces on current
# OPNsense — the per-interface byte counters that match the dashboard live in
# the traffic/interface diagnostic, so we read throughput from there.
_TRAFFIC = "/api/diagnostics/traffic/interface"
_IFINFO = "/api/interfaces/overview/interfacesInfo"
# Firewall filter rules — the section that mirrors Network Manager's rule
# toggles (list / enable-disable / rename, never delete).
_FILTER_GET = "/api/firewall/filter/getRule/"
_FILTER_TOGGLE = "/api/firewall/filter/toggleRule/"
_FILTER_SEARCH = "/api/firewall/filter/searchRule"
_FILTER_APPLY = "/api/firewall/filter/apply"

# Pseudo/loopback interfaces to leave out of aggregate throughput.
_SKIP_IF = ("lo0", "pflog0", "pfsync0", "enc0")


def _get(conn, path):
    try:
        r = conn.get(path)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


def _snapshot(conn):
    """Fetch every endpoint the driver needs once, cached briefly on the conn."""
    cached = getattr(conn, "_ops_snap", None)
    if cached and (time.time() - cached[0]) < 5:
        return cached[1]
    snap = {
        "fw": _get(conn, _FW) or {},
        "res": _get(conn, _RES) or {},
        "time": _get(conn, _TIME) or {},
        "gw": _get(conn, _GW) or {},
        "traffic": _get(conn, _TRAFFIC) or {},
        "ifinfo": _get(conn, _IFINFO) or {},
    }
    conn._ops_snap = (time.time(), snap)
    return snap


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def _i(v):
    try:
        return int(v)
    except Exception:
        return 0


def _hbytes(n):
    n = _i(n)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


def _load1(t):
    la = t.get("loadavg")
    if isinstance(la, (list, tuple)) and la:
        return _f(la[0])
    if isinstance(la, str):
        m = re.search(r"[\d.]+", la)
        return _f(m.group(0)) if m else None
    return None


def _ncpu(conn):
    """CPU core count, parsed once from getCPUType and cached on the conn (it's
    static hardware). Falls back to 1 so a CPU % can always be computed."""
    n = getattr(conn, "_ops_ncpu", None)
    if n is not None:
        return n
    n = 1
    data = _get(conn, _CPUTYPE)
    text = data[0] if isinstance(data, list) and data else (
        data if isinstance(data, str) else "")
    m = re.search(r"(\d+)\s*cores?", str(text))
    if m:
        n = max(1, int(m.group(1)))
    conn._ops_ncpu = n
    return n


def _cpu_pct(conn, t):
    """CPU utilisation as a percentage: the 1-minute load average normalised by
    core count (load == core count means ~100% busy), capped at 100."""
    la = _load1(t)
    if la is None:
        return None
    return round(min(100.0, la / _ncpu(conn) * 100), 1)


def _mem_used_pct(res):
    mem = res.get("memory") or {}
    total, used = _i(mem.get("total")), _i(mem.get("used"))
    return round(used / total * 100, 1) if total else None


def _traffic_by_dev(snap):
    """Map interface device (e.g. 'vlan0.20') -> {ident, rx, tx} from the
    traffic/interface diagnostic. These byte counters match the OPNsense
    dashboard and, unlike getInterfaceStatistics, are correct for VLANs."""
    out = {}
    for ident, rec in ((snap.get("traffic") or {}).get("interfaces") or {}).items():
        if not isinstance(rec, dict):
            continue
        dev = rec.get("device") or ident
        out[dev] = {"ident": ident,
                    "rx": _i(rec.get("bytes received")),
                    "tx": _i(rec.get("bytes transmitted"))}
    return out


def _gw_online(snap):
    items = (snap["gw"] or {}).get("items") or []
    if not items:
        return None
    return sum(1 for g in items if str(g.get("status", "")).lower() not in ("down",))


def _pie(title, slices, total):
    """Usage-donut spec the UI renders as a pie: `slices` is a list of
    (label, value_bytes, tone); center shows the used% (everything not 'free')."""
    rows = [{"label": lbl, "value": val, "text": _hbytes(val), "tone": tone}
            for lbl, val, tone in slices if val is not None]
    used = sum(r["value"] for r in rows if r["tone"] != "free")
    pct = round(used / total * 100) if total else 0
    return {"kind": "pie", "title": title, "slices": rows,
            "center": f"{pct}%", "centerLabel": "used",
            "totalText": _hbytes(total) + " total"}


def _mem_pie(res):
    """Physical-memory breakdown donut: allocated / ZFS ARC / free. OPNsense
    (FreeBSD) reports ARC inside 'used', so allocated is used minus ARC."""
    mem = res.get("memory") or {}
    total = _i(mem.get("total"))
    if not total:
        return None
    used = _i(mem.get("used"))
    arc = _i(mem.get("arc"))
    free = max(0, total - used)
    allocated = max(0, used - arc)
    slices = [("Allocated", allocated, "used")]
    if arc > 0:
        slices.append(("ZFS ARC", arc, "cache"))
    slices.append(("Free", free, "free"))
    return _pie("Physical memory", slices, total)


def _totals(snap):
    """Aggregate in/out bytes across the assigned interfaces (LAN, WAN, VLANs…),
    skipping loopback/pseudo devices."""
    rx = tx = 0
    seen = False
    for dev, v in _traffic_by_dev(snap).items():
        if dev in _SKIP_IF:
            continue
        rx += v["rx"]
        tx += v["tx"]
        seen = True
    return (rx, tx) if seen else (None, None)


class OPNsense(Driver):
    id = "opnsense.firewall"
    display_name = "OPNsense firewall (API)"
    transports = ["api"]

    def probe(self, conn) -> float:
        d = _get(conn, _FW)
        if not isinstance(d, dict):
            return 0.0
        prod = (d.get("product") or {})
        name = str(prod.get("product_name") or d.get("product_name") or "")
        if "opnsense" in name.lower() or "product_version" in prod or \
                "product_version" in d:
            return 0.88
        return 0.3

    def entities(self, conn):
        def snap():
            return _snapshot(conn)

        def _prod(key):
            d = snap()["fw"]
            return (d.get("product") or {}).get(key) or d.get(key)

        return [
            Entity("product", "Product", SENSOR,
                   read=lambda: _prod("product_name") or "OPNsense"),
            Entity("version", "Version", SENSOR,
                   read=lambda: _prod("product_version")),
            Entity("uptime", "Uptime", SENSOR,
                   read=lambda: snap()["time"].get("uptime")),
            Entity("cpu", "CPU", SENSOR, unit="%",
                   read=lambda: _cpu_pct(conn, snap()["time"])),
            Entity("mem_used", "Memory used", SENSOR, unit="%",
                   read=lambda: _mem_used_pct(snap()["res"])),
            Entity("gateways_online", "Gateways online", SENSOR,
                   read=lambda: _gw_online(snap())),
            Entity("in_octets", "Traffic in", SENSOR, unit="bytes",
                   read=lambda: _totals(snap())[0]),
            Entity("out_octets", "Traffic out", SENSOR, unit="bytes",
                   read=lambda: _totals(snap())[1]),
            Entity("updates", "Pending updates", SENSOR,
                   read=lambda: len(snap()["fw"].get("upgrade_packages") or [])
                   + len(snap()["fw"].get("new_packages") or [])),
            Entity("needs_reboot", "Needs reboot", SENSOR,
                   read=lambda: str(snap()["fw"].get("needs_reboot", "0")) == "1"),
        ]

    # ---- firewall filter rules (Network Manager-style toggle section) -------
    def firewall_rule_states(self, conn, managed):
        """Live enabled-state for the user's managed filter rules. `managed` is
        the device's stored [{uuid,name,renamed?}] list; returns [{uuid,name,
        renamed,descr,enabled,error?}] (enabled None when a rule can't be read —
        e.g. deleted on the firewall). `descr` is the live OPNsense rule name and
        `renamed` marks a label the user gave it here, so the UI can prefer the
        real rule name unless the user overrode it. Only reads getRule."""
        out = []
        for r in managed or []:
            uuid = (r or {}).get("uuid")
            if not uuid:
                continue
            name = (r or {}).get("name") or uuid
            renamed = bool((r or {}).get("renamed"))
            rd = _get(conn, _FILTER_GET + uuid)
            rule = rd.get("rule") if isinstance(rd, dict) else None
            if not rule:
                out.append({"uuid": uuid, "name": name, "renamed": renamed,
                            "enabled": None, "error": "not found on firewall"})
                continue
            out.append({"uuid": uuid, "name": name, "renamed": renamed,
                        "descr": (rule.get("description") or "").strip(),
                        "enabled": str(rule.get("enabled", "0")) == "1"})
        return out

    def firewall_all_rules(self, conn):
        """Every filter rule on the firewall, for the add-rule picker:
        [{uuid, label, enabled}] in OPNsense's own order."""
        try:
            r = conn.request("POST", _FILTER_SEARCH,
                             json={"current": 1, "rowCount": 2000})
            data = r.json() if r.status == 200 else {}
        except Exception:
            data = {}
        out = []
        for row in (data.get("rows") or []):
            uuid = row.get("uuid")
            if not uuid:
                continue
            out.append({
                "uuid": uuid,
                "label": (row.get("description") or "").strip() or "(no description)",
                "enabled": str(row.get("enabled", "0")) == "1",
            })
        return out

    def firewall_toggle(self, conn, uuid, enabled):
        """Set a filter rule enabled/disabled (idempotent explicit-state form)
        and apply the change set so it takes effect. Returns {uuid, enabled}
        with the resulting state. Never deletes."""
        if not uuid:
            raise ValueError("uuid required")
        state = "1" if enabled else "0"
        r = conn.request("POST", _FILTER_TOGGLE + uuid + "/" + state, json={})
        if r.status != 200:
            raise ValueError(f"toggle failed (HTTP {r.status})")
        body = r.json() or {}
        # OPNsense answers {"result": "Enabled"|"Disabled", "changed": bool}.
        result = str(body.get("result", "")).lower()
        now = result == "enabled" if result in ("enabled", "disabled") else enabled
        # Apply the pending change set (the toggle alone doesn't reload the ruleset).
        conn.request("POST", _FILTER_APPLY, json={})
        return {"uuid": uuid, "enabled": bool(now)}

    def _dhcp_names(self, conn):
        """MAC -> hostname from DHCP leases (ISC or Kea), authoritative when
        present. Empty on setups without lease data (static addressing/Kea off)."""
        names = {}
        for ep in (_LEASES, _KEA):
            data = _get(conn, ep) or {}
            for row in (data.get("rows") or []):
                mac = (row.get("mac") or row.get("hwaddr") or "").strip().upper()
                host = (row.get("hostname") or row.get("client-hostname") or "").strip()
                if mac and host:
                    names[mac] = host
        return names

    def clients(self, conn):
        """LAN hosts from the firewall's ARP table (+ DHCP hostnames when
        available) — the authoritative MAC↔IP map for the whole network, used to
        fill in IPs/names for devices the switches see only by MAC."""
        arp = _get(conn, _ARP)
        if not isinstance(arp, list):
            return []
        names = self._dhcp_names(conn)
        out = []
        for e in arp:
            if not isinstance(e, dict):
                continue
            ip = (e.get("ip") or "").strip()
            mac = (e.get("mac") or "").strip().upper()
            if not mac or not ip or e.get("expired"):
                continue
            # Skip the firewall's own interface addresses (permanent entries) and
            # anything WAN-side; we want LAN client devices.
            if e.get("permanent") or not is_private_ip(ip):
                continue
            out.append({
                "mac": mac, "ip": ip,
                "hostname": names.get(mac) or (e.get("hostname") or "").strip(),
                "vendor": (e.get("manufacturer") or "").strip(),
                "kind": "wired", "signal": None,
                "where": e.get("intf_description") or e.get("intf") or "",
                # A DHCP/lease hostname is authoritative and should win over a
                # reverse-DNS guess from another source.
                "hostname_authoritative": mac in names,
            })
        return out

    def interfaces(self, conn):
        """Assigned interfaces (LAN, WAN, VLANs, WireGuard…) with the correct
        byte counters from traffic/interface, so the poller can chart each one
        and the user can hide the noise."""
        snap = _snapshot(conn)
        traffic = _traffic_by_dev(snap)
        desc, status = {}, {}
        for r in (snap["ifinfo"] or {}).get("rows") or []:
            dev = r.get("device") or ""
            if dev:
                desc[dev] = r.get("description") or ""
                status[dev] = r.get("status") or ""
        out = []
        for dev, v in traffic.items():
            if dev in _SKIP_IF:
                continue
            out.append({
                "device": dev,
                "name": desc.get(dev) or v["ident"] or dev,
                "status": status.get(dev) or "–",
                "rx": v["rx"],
                "tx": v["tx"],
            })
        out.sort(key=lambda r: r["device"])
        return out

    def detail(self, conn) -> dict:
        snap = _snapshot(conn)
        tables = []

        gws = (snap["gw"] or {}).get("items") or []
        if gws:
            tables.append({
                "title": "Gateways",
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "address", "label": "Address"},
                    {"key": "status", "label": "Status"},
                    {"key": "delay", "label": "Delay"},
                    {"key": "loss", "label": "Loss"},
                ],
                "rows": [{
                    "name": g.get("name") or "–",
                    "address": g.get("address") or "–",
                    "status": g.get("status_translated") or g.get("status") or "–",
                    "delay": g.get("delay") or "–",
                    "loss": g.get("loss") or "–",
                } for g in gws],
            })

        ifaces = self.interfaces(conn)
        if ifaces:
            tables.append({
                "title": f"Interfaces ({len(ifaces)})",
                "interfaces": True,  # UI: clickable rows (history) + edit/remove
                "idKey": "device",
                "columns": [
                    {"key": "name", "label": "Interface"},
                    {"key": "device", "label": "Device"},
                    {"key": "status", "label": "Status"},
                    {"key": "rx", "label": "In"},
                    {"key": "tx", "label": "Out"},
                ],
                "rows": [{
                    "name": f["name"], "device": f["device"],
                    "status": f["status"],
                    "rx": _hbytes(f["rx"]), "tx": _hbytes(f["tx"]),
                } for f in ifaces],
            })

        charts, hide = [], []
        pie = _mem_pie(snap["res"])
        if pie:
            charts.append(pie)
            hide.append("mem_used")

        return {"tables": tables, "charts": charts, "hideEntities": hide}


register(OPNsense())
