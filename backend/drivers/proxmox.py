"""Proxmox VE via its API using an API token (api transport, header auth).

Proxmox authenticates with a single header:
    Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET
In the wizard pick the `api` transport, auth style **header**, key header
**Authorization**, and paste the whole `PVEAPIToken=…` string as the API key.
Identified from /api2/json/version.
"""
from .base import Driver, Entity, SENSOR
from .registry import register


def _data(conn, path):
    try:
        r = conn.get(path)
        if r.status != 200:
            return None
        return (r.json() or {}).get("data")
    except Exception:
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
        cache = {}

        def version():
            if "v" not in cache:
                cache["v"] = _data(conn, "/api2/json/version") or {}
            return cache["v"]

        def nodes():
            if "n" not in cache:
                cache["n"] = _data(conn, "/api2/json/nodes") or []
            return cache["n"]

        return [
            Entity("version", "PVE version", SENSOR,
                   read=lambda: version().get("version")),
            Entity("nodes_total", "Nodes", SENSOR, read=lambda: len(nodes())),
            Entity("nodes_online", "Nodes online", SENSOR,
                   read=lambda: sum(1 for n in nodes()
                                    if n.get("status") == "online")),
            Entity("cluster_uptime", "Max node uptime", SENSOR, unit="s",
                   read=lambda: max((n.get("uptime") or 0 for n in nodes()),
                                    default=None)),
        ]


register(ProxmoxVE())
