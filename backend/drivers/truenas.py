"""TrueNAS (CORE/SCALE) via the REST API v2.0 (api transport, Bearer API key).

Create an API key in TrueNAS, then in the wizard pick the `api` transport, auth
style **bearer**, and paste the key as the API key. Identified from
/api/v2.0/system/info.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_INFO = "/api/v2.0/system/info"


def _get(conn, path):
    try:
        r = conn.get(path)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


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
        cache = {}

        def info():
            if "i" not in cache:
                cache["i"] = _get(conn, _INFO) or {}
            return cache["i"]

        def load1():
            la = info().get("loadavg") or []
            return round(la[0], 2) if la else None

        return [
            Entity("version", "Version", SENSOR, read=lambda: info().get("version")),
            Entity("hostname", "Hostname", SENSOR, read=lambda: info().get("hostname")),
            Entity("uptime", "Uptime", SENSOR, unit="s",
                   read=lambda: info().get("uptime_seconds")),
            Entity("load1", "Load average (1m)", SENSOR, read=load1),
            Entity("mem_total", "Physical memory", SENSOR, unit="bytes",
                   read=lambda: info().get("physmem")),
        ]


register(TrueNAS())
