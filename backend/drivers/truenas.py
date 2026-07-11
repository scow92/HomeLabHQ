"""TrueNAS (CORE/SCALE) via the REST API v2.0 (api transport, Bearer API key).

Surfaces the same data model as our Home Assistant "TrueNAS (native)"
integration — pools, datasets/volumes, disks (+ temperatures), services,
active alerts, and live CPU/RAM — but over the REST API v2.0 that this app's
`api` transport speaks, rather than the WebSocket JSON-RPC + reporting.realtime
subscription HA uses. The realtime-subscription-only bits (per-interface
throughput, per-disk IO rates, ZFS ARC hit ratio) aren't cheaply available over
REST and are intentionally left out.

Create an API key in TrueNAS, then in the wizard pick the `api` transport, auth
style **bearer**, and paste the key as the API key. Identified from
/api/v2.0/system/info.
"""
import re
import time

from .base import Driver, Entity, SENSOR
from .registry import register

_INFO = "/api/v2.0/system/info"
# ZFS top-level vdev categories as returned by /pool's "topology" field.
_TOPOLOGY_CATEGORIES = ("data", "cache", "log", "spare", "special", "dedup")


def _get(conn, path):
    try:
        r = conn.get(path)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


def _post(conn, path, body):
    try:
        r = conn.request("POST", path, json=body)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


def _zfs_number(value):
    """Pull a numeric byte count out of a TrueNAS ZFS-property value, which the
    API shapes as {"parsed": 123, "rawvalue": "123", "value": "1.02 TiB", ...}.
    "parsed" is the real number; rawvalue/value are fallbacks against a future
    TrueNAS reshaping it."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("parsed", "rawvalue", "value"):
            candidate = value.get(key)
            if candidate is None:
                continue
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
    return None


def _hbytes(num):
    """Format a byte count the way a NAS UI would ('512 GB', '4.0 TB')."""
    if num is None:
        return None
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _walk_vdev(vdev, category, out):
    """Recursively walk a vdev tree (handles mirror/raidz nesting) collecting
    each leaf DISK's category and ZFS status."""
    if vdev.get("type") == "DISK" and vdev.get("disk"):
        out[vdev["disk"]] = {"category": category, "zfs_status": vdev.get("status")}
    for child in vdev.get("children") or []:
        _walk_vdev(child, category, out)


def _pool_topology(pool):
    """Return ({disk_name: {category, zfs_status}}, raid_level) for a pool."""
    disks = {}
    topo = pool.get("topology") or {}
    for category in _TOPOLOGY_CATEGORIES:
        for vdev in topo.get(category) or []:
            _walk_vdev(vdev, category, disks)
    raid_level = None
    data_vdevs = topo.get("data") or []
    if data_vdevs:
        top_type = data_vdevs[0].get("type")
        raid_level = "STRIPE" if top_type == "DISK" else top_type
    return disks, raid_level


def _pool_used_pct(pool):
    size = pool.get("size") or 0
    return round((pool.get("allocated") or 0) / size * 100, 1) if size else None


def _reporting_latest(conn, names):
    """Latest value of each named reporting graph's first data series, fetched
    over a short 30s window so the payload stays a few dozen points rather than
    a full page. Returns {graph_name: float}; graphs with no usable data (or a
    failed call) are simply absent. Legends put time in column 0 and the value
    we want in column 1 (cpu -> aggregate busy%, memory -> available bytes,
    arcsize -> ARC bytes)."""
    now = int(time.time())
    data = _post(conn, "/api/v2.0/reporting/get_data",
                 {"graphs": [{"name": n} for n in names],
                  "query": {"start": now - 30, "end": now}})
    out = {}
    if isinstance(data, list):
        for g in data:
            if not isinstance(g, dict):
                continue
            for row in reversed(g.get("data") or []):
                if len(row) > 1 and row[1] is not None:
                    out[g.get("name")] = float(row[1])
                    break
    return out


def _cpu_ram(conn, physmem):
    """Latest CPU busy% and RAM used% from the reporting endpoint. Returns
    (cpu_pct, ram_pct); either may be None if unavailable."""
    latest = _reporting_latest(conn, ["cpu", "memory"])
    cpu = round(latest["cpu"], 1) if "cpu" in latest else None
    ram = None
    if "memory" in latest and physmem:
        ram = round((1 - latest["memory"] / physmem) * 100, 1)
    return cpu, ram


def _mem_breakdown(conn, physmem):
    """Split physical memory into allocated / ZFS ARC / free bytes for the memory
    donut. 'available' (free) and ARC size come from the reporting endpoint;
    ARC lives inside used memory, so allocated is whatever's left of physmem
    after free and ARC. Returns {} when physmem is unknown."""
    if not physmem:
        return {}
    latest = _reporting_latest(conn, ["memory", "arcsize"])
    total = float(physmem)
    free = max(0.0, latest.get("memory") or 0.0)
    arc = max(0.0, latest.get("arcsize") or 0.0)
    allocated = max(0.0, total - free - arc)
    return {"total": total, "free": free, "arc": arc, "allocated": allocated}


def _pie(title, slices, total, center_label="used"):
    """Build a usage-donut spec the UI renders as a pie. `slices` is a list of
    (label, value_bytes, tone); the center shows the used percentage (everything
    that isn't the 'free' tone)."""
    rows = [{"label": lbl, "value": val, "text": _hbytes(val), "tone": tone}
            for lbl, val, tone in slices if val is not None]
    used = sum(r["value"] for r in rows if r["tone"] != "free")
    pct = round(used / total * 100) if total else 0
    return {"kind": "pie", "title": title, "slices": rows,
            "center": f"{pct}%", "centerLabel": center_label,
            "totalText": (_hbytes(total) + " total") if total else None}


class TrueNAS(Driver):
    id = "truenas.system"
    display_name = "TrueNAS (REST API)"
    transports = ["api"]

    def probe(self, conn) -> float:
        d = _get(conn, _INFO)
        if isinstance(d, dict) and (d.get("version") or d.get("hostname")):
            return 0.9
        return 0.0

    def entities(self, conn):
        # One fetch per poll, shared across every entity read below.
        cache = {}

        def info():
            if "i" not in cache:
                cache["i"] = _get(conn, _INFO) or {}
            return cache["i"]

        def pools():
            if "p" not in cache:
                cache["p"] = _get(conn, "/api/v2.0/pool") or []
            return cache["p"]

        def alerts_count():
            if "a" not in cache:
                lst = _get(conn, "/api/v2.0/alert/list") or []
                cache["a"] = sum(1 for a in lst
                                 if isinstance(a, dict) and not a.get("dismissed"))
            return cache["a"]

        def realtime():
            if "rt" not in cache:
                cache["rt"] = _cpu_ram(conn, info().get("physmem"))
            return cache["rt"]

        def load1():
            la = info().get("loadavg") or []
            return round(la[0], 2) if la else None

        ents = [
            Entity("version", "Version", SENSOR, read=lambda: info().get("version")),
            Entity("hostname", "Hostname", SENSOR, read=lambda: info().get("hostname")),
            Entity("model", "Model", SENSOR,
                   read=lambda: info().get("system_product") or info().get("model")),
            Entity("uptime", "Uptime", SENSOR, unit="s",
                   read=lambda: info().get("uptime_seconds")),
            Entity("load1", "Load average (1m)", SENSOR, read=load1),
            Entity("cpu_usage", "CPU usage", SENSOR, unit="%",
                   read=lambda: realtime()[0]),
            Entity("mem_total", "Physical memory", SENSOR, unit="bytes",
                   read=lambda: info().get("physmem")),
            Entity("ram_used", "RAM used", SENSOR, unit="%",
                   read=lambda: realtime()[1]),
            Entity("alerts", "Active alerts", SENSOR, read=alerts_count),
        ]

        # One usage% sensor per pool (like the HA per-pool "Used" sensors).
        for pool in pools():
            name = pool.get("name")
            if not name:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "pool"

            def used(pool_name=name):
                for p in pools():
                    if p.get("name") == pool_name:
                        return _pool_used_pct(p)
                return None

            ents.append(Entity(f"pool_{slug}_used", f"Pool {name} used",
                               SENSOR, unit="%", read=used))
        return ents

    def detail(self, conn) -> dict:
        info = _get(conn, _INFO) or {}
        pools = _get(conn, "/api/v2.0/pool") or []
        datasets = _get(conn, "/api/v2.0/pool/dataset") or []
        disks = _get(conn, "/api/v2.0/disk") or []
        services = _get(conn, "/api/v2.0/service") or []
        alerts = _get(conn, "/api/v2.0/alert/list") or []
        temps = _post(conn, "/api/v2.0/disk/temperatures", {}) or {}

        # --- pools (+ topology -> RAID level, per-disk category/status/pool) ---
        disk_topo = {}      # disk name -> {category, zfs_status}
        disk_pool = {}      # disk name -> owning pool (the /disk record often omits it)
        pool_rows = []
        for pool in sorted(pools, key=lambda p: p.get("name") or ""):
            topo_disks, raid = _pool_topology(pool)
            disk_topo.update(topo_disks)
            for disk_name in topo_disks:
                disk_pool[disk_name] = pool.get("name")
            pool_rows.append({
                "name": pool.get("name"),
                "status": pool.get("status"),
                "healthy": "Yes" if pool.get("healthy") else "No",
                "level": raid,
                "used": _pool_used_pct(pool),
                "size": _hbytes(pool.get("size")),
                "free": _hbytes(pool.get("free")),
                "disks": len(topo_disks),
            })

        # --- datasets ("volumes"): skip each pool's root (id == pool name). ---
        vol_rows = []
        for ds in sorted(datasets, key=lambda d: d.get("id") or ""):
            ds_id = ds.get("id")
            if not ds_id:
                continue
            pool_name = ds.get("pool") or ds_id.split("/", 1)[0]
            if ds_id == pool_name:
                continue  # pool root, already represented by the Pools table
            used = _zfs_number(ds.get("used"))
            avail = _zfs_number(ds.get("available"))
            total = used + avail if used is not None and avail is not None else None
            vol_rows.append({
                "name": ds.get("name") or ds_id,
                "pool": pool_name,
                "used": _hbytes(used),
                "available": _hbytes(avail),
                "total": _hbytes(total),
                "mountpoint": ds.get("mountpoint"),
            })

        # --- disks (+ temperature, topology-derived role/health) ---
        disk_rows = []
        for disk in sorted(disks, key=lambda d: d.get("name") or ""):
            name = disk.get("name")
            if not name:
                continue
            topo = disk_topo.get(name, {})
            temp = temps.get(name)
            disk_rows.append({
                "name": name,
                "model": disk.get("model"),
                "serial": disk.get("serial"),
                "size": _hbytes(disk.get("size")),
                "temp": round(temp) if isinstance(temp, (int, float)) else None,
                "pool": disk.get("pool") or disk_pool.get(name) or "—",
                "role": topo.get("category", "data"),
                "status": topo.get("zfs_status"),
            })

        svc_rows = [{"service": s.get("service"), "state": s.get("state"),
                     "boot": "Yes" if s.get("enable") else "No"}
                    for s in services if s.get("service")]

        alert_rows = [{"level": a.get("level"), "klass": a.get("klass"),
                       "message": a.get("formatted") or a.get("text"),
                       "dismissed": "Yes" if a.get("dismissed") else "No"}
                      for a in alerts]

        info_kv = {
            "Model": info.get("system_product"),
            "Version": info.get("version"),
            "Hostname": info.get("hostname"),
            "Serial": info.get("system_serial"),
            "Uptime": info.get("uptime"),
        }

        tables = [
            {"title": f"Pools ({len(pool_rows)})",
             "columns": [
                 {"key": "name", "label": "Pool"},
                 {"key": "status", "label": "Status"},
                 {"key": "healthy", "label": "Healthy"},
                 {"key": "level", "label": "RAID"},
                 {"key": "used", "label": "Used", "unit": "%"},
                 {"key": "size", "label": "Size"},
                 {"key": "free", "label": "Free"},
                 {"key": "disks", "label": "Disks"}],
             "rows": pool_rows},
            {"title": f"Volumes ({len(vol_rows)})",
             "columns": [
                 {"key": "name", "label": "Dataset"},
                 {"key": "pool", "label": "Pool"},
                 {"key": "used", "label": "Used"},
                 {"key": "available", "label": "Free"},
                 {"key": "total", "label": "Total"},
                 {"key": "mountpoint", "label": "Mounted at"}],
             "rows": vol_rows},
            {"title": f"Disks ({len(disk_rows)})",
             "columns": [
                 {"key": "name", "label": "Disk"},
                 {"key": "model", "label": "Model"},
                 {"key": "serial", "label": "Serial"},
                 {"key": "size", "label": "Size"},
                 {"key": "temp", "label": "Temp", "unit": "°C"},
                 {"key": "pool", "label": "Pool"},
                 {"key": "role", "label": "Role"},
                 {"key": "status", "label": "Status"}],
             "rows": disk_rows},
            {"title": f"Services ({len(svc_rows)})",
             "columns": [
                 {"key": "service", "label": "Service"},
                 {"key": "state", "label": "State"},
                 {"key": "boot", "label": "Start on boot"}],
             "rows": svc_rows},
        ]
        if alert_rows:
            tables.append({
                "title": f"Alerts ({len(alert_rows)})",
                "columns": [
                    {"key": "level", "label": "Level"},
                    {"key": "klass", "label": "Class"},
                    {"key": "message", "label": "Message"},
                    {"key": "dismissed", "label": "Dismissed"}],
                "rows": alert_rows})

        # --- usage donuts (pie charts): physical memory + per-pool capacity. ---
        # Each replaces its flat metric card(s) — hidden via hideEntities so the
        # same number isn't shown twice.
        charts = []
        hide = []
        mem = _mem_breakdown(conn, info.get("physmem"))
        if mem:
            charts.append(_pie("Physical memory", [
                ("Allocated", mem["allocated"], "used"),
                ("ZFS ARC", mem["arc"], "cache"),
                ("Free", mem["free"], "free"),
            ], mem["total"]))
            hide += ["mem_total", "ram_used"]
        for pool in sorted(pools, key=lambda p: p.get("name") or ""):
            name = pool.get("name")
            size = pool.get("size") or 0
            if not name or not size:
                continue
            allocated = pool.get("allocated") or 0
            free = pool.get("free")
            free = max(0, size - allocated) if free is None else free
            charts.append(_pie(f"Pool {name}", [
                ("Used", allocated, "used"),
                ("Free", free, "free"),
            ], size))
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "pool"
            hide.append(f"pool_{slug}_used")

        return {"info": info_kv, "tables": tables,
                "charts": charts, "hideEntities": hide}


register(TrueNAS())
