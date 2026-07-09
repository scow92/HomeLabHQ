"""Ubiquiti UniFi Network via the local Integration API (api transport).

UniFi Network 9+ exposes a local API-key API under /proxy/network/integration.
In the wizard pick the `api` transport, auth style **header**, key header
**X-API-KEY**, and paste the key. Identified from the sites listing.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_SITES = "/proxy/network/integration/v1/sites"


def _get(conn, path):
    try:
        r = conn.get(path)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


class UniFiNetwork(Driver):
    id = "unifi.network"
    display_name = "UniFi Network controller (API)"
    transports = ["api"]

    def probe(self, conn) -> float:
        d = _get(conn, _SITES)
        if isinstance(d, dict) and "data" in d:
            return 0.85
        return 0.0

    def entities(self, conn):
        cache = {}

        def sites():
            if "s" not in cache:
                cache["s"] = _get(conn, _SITES) or {}
            return cache["s"]

        def _first_site_id():
            data = sites().get("data") or []
            return data[0].get("id") if data else None

        def device_count():
            sid = _first_site_id()
            if not sid:
                return None
            d = _get(conn, f"{_SITES}/{sid}/devices")
            if isinstance(d, dict):
                return d.get("totalCount", len(d.get("data") or []))
            return None

        return [
            Entity("sites", "Sites", SENSOR,
                   read=lambda: sites().get("totalCount",
                                            len(sites().get("data") or []))),
            Entity("devices", "Devices (first site)", SENSOR, read=device_count),
        ]


register(UniFiNetwork())
