"""pfSense via the REST API package (v2) — api transport, API key in a header.

Targets the popular pfSense REST API v2 package. In the wizard pick the `api`
transport, auth style **header**, key header **X-API-Key**, and paste the key as
the API key. Identified from /api/v2/status/system.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_SYS = "/api/v2/status/system"


def _data(conn, path):
    try:
        r = conn.get(path)
        if r.status != 200:
            return None
        j = r.json() or {}
        # REST API v2 wraps payloads in {"data": {...}}
        return j.get("data", j)
    except Exception:
        return None


class PfSense(Driver):
    id = "pfsense.firewall"
    display_name = "pfSense"
    transports = ["api"]

    def probe(self, conn) -> float:
        d = _data(conn, _SYS)
        if not isinstance(d, dict):
            return 0.0
        if "netgate_id" in d or "platform" in d or "kernel_pti" in d:
            return 0.85
        return 0.3

    def entities(self, conn):
        cache = {}

        def sys():
            if "s" not in cache:
                cache["s"] = _data(conn, _SYS) or {}
            return cache["s"]

        return [
            Entity("platform", "Platform", SENSOR, read=lambda: sys().get("platform")),
            Entity("version", "Version", SENSOR,
                   read=lambda: sys().get("version") or sys().get("kernel")),
            Entity("uptime", "Uptime", SENSOR, read=lambda: sys().get("uptime")),
            Entity("cpu_load", "CPU load", SENSOR, unit="%",
                   read=lambda: sys().get("cpu_load") or sys().get("cpu_usage")),
            Entity("mem_used", "Memory used", SENSOR, unit="%",
                   read=lambda: sys().get("mem_usage") or sys().get("memory_usage")),
        ]


register(PfSense())
