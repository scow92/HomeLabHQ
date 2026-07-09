"""MikroTik RouterOS via the REST API (RouterOS v7+).

RouterOS exposes a REST API at /rest with HTTP Basic auth, so it rides the
`api` transport: enter the RouterOS username as the "API key" and the password
as the "API secret" (Basic auth = key:secret). Identified with high confidence
when /rest/system/resource reports the MikroTik platform.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_RESOURCE = "/rest/system/resource"


class MikroTikRouterOS(Driver):
    id = "mikrotik.routeros"
    display_name = "MikroTik RouterOS (REST)"
    transports = ["api"]

    def probe(self, conn) -> float:
        try:
            r = conn.get(_RESOURCE)
        except Exception:
            return 0.0
        if r.status != 200:
            return 0.0
        d = r.json() or {}
        if "board-name" in d or "platform" in d:
            return 0.9 if str(d.get("platform", "")).lower() == "mikrotik" else 0.6
        return 0.0

    def entities(self, conn):
        cache = {}

        def res():
            if "r" not in cache:
                try:
                    cache["r"] = conn.get(_RESOURCE).json() or {}
                except Exception:
                    cache["r"] = {}
            return cache["r"]

        def version():
            return res().get("version")

        def board():
            return res().get("board-name")

        def uptime():
            return res().get("uptime")

        def cpu_load():
            v = res().get("cpu-load")
            try:
                return int(v)
            except Exception:
                return None

        def mem_used_pct():
            total = _num(res().get("total-memory"))
            free = _num(res().get("free-memory"))
            if total:
                return round((total - free) / total * 100, 1)
            return None

        return [
            Entity("version", "RouterOS version", SENSOR, read=version),
            Entity("board", "Board", SENSOR, read=board),
            Entity("uptime", "Uptime", SENSOR, read=uptime),
            Entity("cpu_load", "CPU load", SENSOR, unit="%", read=cpu_load),
            Entity("mem_used", "Memory used", SENSOR, unit="%", read=mem_used_pct),
        ]


def _num(v):
    try:
        return int(v)
    except Exception:
        return None


register(MikroTikRouterOS())
