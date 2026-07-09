"""OPNsense firewall via its REST API (api transport, Basic key:secret).

Enter the OPNsense API **key as the API key** and **secret as the API secret**
(OPNsense uses HTTP Basic where key=user, secret=password). Identified from
/api/core/firmware/status.

Field names follow current OPNsense; parsing is defensive so minor version
differences degrade to None rather than erroring.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_FW = "/api/core/firmware/status"


def _get(conn, path):
    try:
        r = conn.get(path)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


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
        cache = {}

        def fw():
            if "fw" not in cache:
                cache["fw"] = _get(conn, _FW) or {}
            return cache["fw"]

        def _prod(key):
            d = fw()
            return (d.get("product") or {}).get(key) or d.get(key)

        return [
            Entity("product", "Product", SENSOR,
                   read=lambda: _prod("product_name") or "OPNsense"),
            Entity("version", "Version", SENSOR,
                   read=lambda: _prod("product_version")),
            Entity("needs_reboot", "Needs reboot", SENSOR,
                   read=lambda: str(fw().get("needs_reboot", "0")) == "1"),
            Entity("updates", "Pending updates", SENSOR,
                   read=lambda: len(fw().get("upgrade_packages") or [])
                   + len(fw().get("new_packages") or [])),
        ]


register(OPNsense())
