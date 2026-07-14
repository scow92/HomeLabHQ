"""Proxmox VE via its API using an API token (api transport, header auth).

Proxmox authenticates with a single header:
    Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET
The add-device wizard's Proxmox preset collects the token id + secret and
assembles that header for you (auth style **header**, key header
**Authorization**). Identified from /api2/json/version.

Surfaces the same shape as our TrueNAS driver — flat health sensors plus a
detail() drill-down with usage donuts (CPU + memory) and tables (nodes, VMs,
containers, storage, disks with SMART temperatures). Everything is read from
one /cluster/resources call (which a standalone node answers too), with a
per-node status/disks fan-out only in the on-demand detail view.
"""
from .base import Driver, Entity, SENSOR
from .registry import register


def _data(conn, path):
    """Return the `data` field of a Proxmox API response, or None on any error."""
    try:
        r = conn.get(path)
        if r.status != 200:
            return None
        return (r.json() or {}).get("data")
    except Exception:
        return None


def _hbytes(num):
    """Format a byte count the way the Proxmox UI would ('512 GB', '4.0 TB')."""
    if num is None:
        return None
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _huptime(sec):
    """Compact uptime like '5d 3h' / '2h 14m' from a second count."""
    if not sec:
        return None
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _f(x):
    """Coerce an API value to float, or None. Proxmox hands some numerics back
    as strings (loadavg, disk wearout), so callers can't assume int/float."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(x, digits=1):
    """A Proxmox cpu/usage fraction (0..1) as a rounded percentage."""
    return round(float(x) * 100, digits) if isinstance(x, (int, float)) else None


def _pie_bytes(title, slices, total, center_label="used"):
    """A byte-usage donut (mirrors the TrueNAS memory/pool pies): `slices` is a
    list of (label, value_bytes, tone); the center shows used% (everything not
    toned 'free')."""
    rows = [{"label": lbl, "value": val, "text": _hbytes(val), "tone": tone}
            for lbl, val, tone in slices if val is not None]
    used = sum(r["value"] for r in rows if r["tone"] != "free")
    pct = round(used / total * 100) if total else 0
    return {"kind": "pie", "title": title, "slices": rows,
            "center": f"{pct}%", "centerLabel": center_label,
            "totalText": (_hbytes(total) + " total") if total else None}


def _pie_pct(title, used_pct, used_label="Used", idle_label="Idle"):
    """A percentage donut for CPU: a Used slice + an Idle remainder, both shown
    as NN%. `used_pct` is already 0..100."""
    if used_pct is None:
        return None
    used_pct = max(0.0, min(100.0, float(used_pct)))
    idle = round(100.0 - used_pct, 1)
    rows = [{"label": used_label, "value": round(used_pct, 1),
             "text": f"{round(used_pct, 1)}%", "tone": "used"},
            {"label": idle_label, "value": idle, "text": f"{idle}%", "tone": "free"}]
    return {"kind": "pie", "title": title, "slices": rows,
            "center": f"{round(used_pct)}%", "centerLabel": "busy"}


def _split_resources(resources):
    """Bucket a /cluster/resources list by type."""
    nodes, qemu, lxc, storage = [], [], [], []
    for r in resources or []:
        t = r.get("type")
        if t == "node":
            nodes.append(r)
        elif t == "qemu":
            qemu.append(r)
        elif t == "lxc":
            lxc.append(r)
        elif t == "storage":
            storage.append(r)
    return nodes, qemu, lxc, storage


def _cluster_cpu_pct(nodes):
    """Core-weighted cluster CPU busy% across online nodes (a 32-core node at
    50% counts more than a 4-core node at 50%)."""
    used = sum((n.get("cpu") or 0) * (n.get("maxcpu") or 0)
               for n in nodes if n.get("status") == "online")
    cores = sum((n.get("maxcpu") or 0) for n in nodes if n.get("status") == "online")
    return round(used / cores * 100, 1) if cores else None


def _cluster_mem(nodes):
    """(used_bytes, total_bytes) summed over online nodes."""
    used = sum((n.get("mem") or 0) for n in nodes if n.get("status") == "online")
    total = sum((n.get("maxmem") or 0) for n in nodes if n.get("status") == "online")
    return used, total


def _disk_temp(conn, node, devpath):
    """Current SMART temperature (°C) for a disk, or None. ATA disks expose it
    as attribute 194; NVMe comes back as smartctl text (the SN520 path), so we
    parse a 'Temperature: NN Celsius' line as a fallback."""
    d = _data(conn, f"/api2/json/nodes/{node}/disks/smart?disk={devpath}")
    if not isinstance(d, dict):
        return None
    attrs = d.get("attributes")
    if isinstance(attrs, list):
        for a in attrs:
            name = (a.get("name") or "").lower()
            if str(a.get("id")) == "194" or name.startswith("temperature"):
                raw = a.get("raw", a.get("value"))
                try:
                    return int(str(raw).strip().split()[0])
                except (ValueError, IndexError):
                    continue
    if d.get("type") == "text" and d.get("text"):
        for line in d["text"].splitlines():
            if "Temperature" in line and ":" in line:
                try:
                    return int(line.split(":", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    continue
    return None


class ProxmoxVE(Driver):
    id = "proxmox.ve"
    display_name = "Proxmox VE (API token)"
    transports = ["api"]

    def probe(self, conn) -> float:
        d = _data(conn, "/api2/json/version")
        if isinstance(d, dict) and d.get("version"):
            return 0.9
        return 0.0

    def entities(self, conn):
        # One /version + one /cluster/resources fetch per poll, shared below.
        cache = {}

        def version():
            if "v" not in cache:
                cache["v"] = _data(conn, "/api2/json/version") or {}
            return cache["v"]

        def resources():
            if "r" not in cache:
                cache["r"] = _data(conn, "/api2/json/cluster/resources") or []
            return cache["r"]

        def nodes():
            return _split_resources(resources())[0]

        def guests():
            _, qemu, lxc, _ = _split_resources(resources())
            return qemu, lxc

        def mem_used_pct():
            used, total = _cluster_mem(nodes())
            return round(used / total * 100, 1) if total else None

        return [
            Entity("version", "PVE version", SENSOR,
                   read=lambda: version().get("version")),
            Entity("nodes_total", "Nodes", SENSOR, read=lambda: len(nodes())),
            Entity("nodes_online", "Nodes online", SENSOR,
                   read=lambda: sum(1 for n in nodes()
                                    if n.get("status") == "online")),
            Entity("cpu_pct", "CPU usage", SENSOR, unit="%",
                   read=lambda: _cluster_cpu_pct(nodes())),
            Entity("mem_total", "Memory total", SENSOR, unit="bytes",
                   read=lambda: _cluster_mem(nodes())[1] or None),
            Entity("mem_used_pct", "Memory used", SENSOR, unit="%",
                   read=mem_used_pct),
            Entity("vms_running", "VMs running", SENSOR,
                   read=lambda: sum(1 for q in guests()[0]
                                    if q.get("status") == "running")),
            Entity("lxc_running", "Containers running", SENSOR,
                   read=lambda: sum(1 for c in guests()[1]
                                    if c.get("status") == "running")),
            Entity("cluster_uptime", "Max node uptime", SENSOR, unit="s",
                   read=lambda: max((n.get("uptime") or 0 for n in nodes()),
                                    default=None) or None),
        ]

    def detail(self, conn) -> dict:
        resources = _data(conn, "/api2/json/cluster/resources") or []
        nodes, qemu, lxc, storage = _split_resources(resources)
        online = [n for n in nodes if n.get("status") == "online"]

        # Per-node status: CPU model, swap and load average aren't in
        # /cluster/resources, so pull each online node's status for the table.
        node_status = {}
        for n in online:
            name = n.get("node")
            if name:
                node_status[name] = _data(
                    conn, f"/api2/json/nodes/{name}/status") or {}

        # --- Nodes table ---
        node_rows = []
        for n in sorted(nodes, key=lambda x: x.get("node") or ""):
            name = n.get("node")
            st = node_status.get(name, {})
            la = st.get("loadavg") or []
            nq = sum(1 for q in qemu if q.get("node") == name
                     and q.get("status") == "running")
            nc = sum(1 for c in lxc if c.get("node") == name
                     and c.get("status") == "running")
            maxmem = n.get("maxmem")
            node_rows.append({
                "node": name,
                "status": n.get("status"),
                "model": (st.get("cpuinfo") or {}).get("model"),
                "cpu": _pct(n.get("cpu")),
                "mem": _pct((n.get("mem") or 0) / maxmem) if maxmem else None,
                "memtext": f"{_hbytes(n.get('mem'))} / {_hbytes(maxmem)}",
                "load": (round(_f(la[0]), 2) if la and _f(la[0]) is not None
                         else None),
                "uptime": _huptime(n.get("uptime")),
                "vms": nq,
                "cts": nc,
            })

        # --- Virtual machines + Containers tables ---
        def guest_rows(items):
            rows = []
            for g in sorted(items, key=lambda x: (x.get("node") or "",
                                                  x.get("name") or "")):
                maxmem = g.get("maxmem")
                rows.append({
                    "name": g.get("name"),
                    "vmid": g.get("vmid"),
                    "node": g.get("node"),
                    "status": g.get("status"),
                    "cpu": _pct(g.get("cpu")) if g.get("status") == "running" else None,
                    "mem": _pct((g.get("mem") or 0) / maxmem) if maxmem else None,
                    "memtext": f"{_hbytes(g.get('mem'))} / {_hbytes(maxmem)}",
                    "disk": _hbytes(g.get("maxdisk")),
                    "uptime": _huptime(g.get("uptime")),
                })
            return rows

        vm_rows = guest_rows(qemu)
        ct_rows = guest_rows(lxc)

        # --- Storage table (dedupe shared storages seen on every node) ---
        seen = set()
        stor_rows = []
        for s in sorted(storage, key=lambda x: x.get("storage") or ""):
            name = s.get("storage")
            key = name if s.get("shared") else (name, s.get("node"))
            if key in seen:
                continue
            seen.add(key)
            maxd = s.get("maxdisk")
            stor_rows.append({
                "storage": name,
                "node": "cluster" if s.get("shared") else s.get("node"),
                "type": s.get("plugintype"),
                "used": _pct((s.get("disk") or 0) / maxd) if maxd else None,
                "size": _hbytes(maxd),
                "free": _hbytes((maxd - s.get("disk", 0)) if maxd else None),
                "status": s.get("status"),
            })

        # --- Disks table (+ SMART temperature), one disks/list per online node ---
        disk_rows = []
        for n in online:
            name = n.get("node")
            for d in _data(conn, f"/api2/json/nodes/{name}/disks/list") or []:
                devpath = d.get("devpath")
                wearout = _f(d.get("wearout"))
                disk_rows.append({
                    "node": name,
                    "dev": devpath,
                    "model": d.get("model"),
                    "serial": d.get("serial"),
                    "type": (d.get("type") or "").upper() or None,
                    "size": _hbytes(d.get("size")),
                    "temp": _disk_temp(conn, name, devpath) if devpath else None,
                    "wearout": round(100 - wearout) if wearout is not None else None,
                    "health": d.get("health"),
                })

        tables = [
            {"title": f"Nodes ({len(node_rows)})",
             "columns": [
                 {"key": "node", "label": "Node"},
                 {"key": "status", "label": "Status"},
                 {"key": "model", "label": "CPU"},
                 {"key": "cpu", "label": "CPU", "unit": "%"},
                 {"key": "mem", "label": "Mem", "unit": "%"},
                 {"key": "memtext", "label": "Memory"},
                 {"key": "load", "label": "Load 1m"},
                 {"key": "uptime", "label": "Uptime"},
                 {"key": "vms", "label": "VMs"},
                 {"key": "cts", "label": "CTs"}],
             "rows": node_rows},
            {"title": f"Virtual machines ({len(vm_rows)})",
             "columns": [
                 {"key": "name", "label": "Name"},
                 {"key": "vmid", "label": "VMID"},
                 {"key": "node", "label": "Node"},
                 {"key": "status", "label": "Status"},
                 {"key": "cpu", "label": "CPU", "unit": "%"},
                 {"key": "mem", "label": "Mem", "unit": "%"},
                 {"key": "memtext", "label": "Memory"},
                 {"key": "disk", "label": "Disk"},
                 {"key": "uptime", "label": "Uptime"}],
             "rows": vm_rows},
            {"title": f"Containers ({len(ct_rows)})",
             "columns": [
                 {"key": "name", "label": "Name"},
                 {"key": "vmid", "label": "VMID"},
                 {"key": "node", "label": "Node"},
                 {"key": "status", "label": "Status"},
                 {"key": "cpu", "label": "CPU", "unit": "%"},
                 {"key": "mem", "label": "Mem", "unit": "%"},
                 {"key": "memtext", "label": "Memory"},
                 {"key": "disk", "label": "Disk"},
                 {"key": "uptime", "label": "Uptime"}],
             "rows": ct_rows},
            {"title": f"Storage ({len(stor_rows)})",
             "columns": [
                 {"key": "storage", "label": "Storage"},
                 {"key": "node", "label": "Node"},
                 {"key": "type", "label": "Type"},
                 {"key": "used", "label": "Used", "unit": "%"},
                 {"key": "size", "label": "Size"},
                 {"key": "free", "label": "Free"},
                 {"key": "status", "label": "Status"}],
             "rows": stor_rows},
        ]
        if disk_rows:
            tables.append({
                "title": f"Disks ({len(disk_rows)})",
                "columns": [
                    {"key": "node", "label": "Node"},
                    {"key": "dev", "label": "Device"},
                    {"key": "model", "label": "Model"},
                    {"key": "serial", "label": "Serial"},
                    {"key": "type", "label": "Type"},
                    {"key": "size", "label": "Size"},
                    {"key": "temp", "label": "Temp", "unit": "°C"},
                    {"key": "wearout", "label": "Wearout", "unit": "%"},
                    {"key": "health", "label": "Health"}],
                "rows": disk_rows})

        # --- usage donuts: cluster CPU + cluster memory, then per-node memory.
        # These replace the flat cpu%/mem% metric cards (hidden below).
        charts = []
        cpu_pie = _pie_pct("Cluster CPU", _cluster_cpu_pct(nodes))
        if cpu_pie:
            charts.append(cpu_pie)
        used, total = _cluster_mem(nodes)
        if total:
            charts.append(_pie_bytes("Cluster memory", [
                ("Used", used, "used"),
                ("Free", max(0, total - used), "free"),
            ], total))
        for n in sorted(online, key=lambda x: x.get("node") or ""):
            name = n.get("node")
            maxmem = n.get("maxmem")
            if not name or not maxmem:
                continue
            mem = n.get("mem") or 0
            charts.append(_pie_bytes(f"{name} memory", [
                ("Used", mem, "used"),
                ("Free", max(0, maxmem - mem), "free"),
            ], maxmem))

        return {"info": {}, "tables": tables, "charts": charts,
                "hideEntities": ["cpu_pct", "mem_used_pct", "mem_total"]}


register(ProxmoxVE())
