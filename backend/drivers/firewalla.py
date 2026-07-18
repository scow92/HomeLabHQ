"""Firewalla via the MSP API (api transport, Authorization: Token <token>).

Firewalla's programmable API is the MSP API at https://<your>.firewalla.net.
In the wizard pick the `api` transport, auth style **header**, key header
**Authorization**, and paste **`Token <your-personal-access-token>`** as the API
key. Host is your MSP domain. Identified from /v2/boxes.
"""
from .base import Driver, Entity, SENSOR
from .registry import register

_BOXES = "/v2/boxes"


def _boxes(conn):
    try:
        r = conn.get(_BOXES)
        if r.status != 200:
            return None
        j = r.json()
        if isinstance(j, list):
            return j
        if isinstance(j, dict):
            return j.get("results") or j.get("data") or []
        return None
    except Exception:
        return None


class Firewalla(Driver):
    id = "firewalla.msp"
    display_name = "Firewalla"
    transports = ["api"]

    def probe(self, conn) -> float:
        boxes = _boxes(conn)
        if isinstance(boxes, list):
            # MSP boxes carry a group id (gid) — a good Firewalla fingerprint.
            if boxes and any("gid" in b for b in boxes if isinstance(b, dict)):
                return 0.9
            return 0.75
        return 0.0

    def entities(self, conn):
        cache = {}

        def boxes():
            if "b" not in cache:
                cache["b"] = _boxes(conn) or []
            return cache["b"]

        def first(key):
            bs = boxes()
            return bs[0].get(key) if bs else None

        return [
            Entity("boxes", "Boxes", SENSOR, read=lambda: len(boxes())),
            Entity("online", "Boxes online", SENSOR,
                   read=lambda: sum(1 for b in boxes() if b.get("online"))),
            Entity("model", "Model", SENSOR, read=lambda: first("model")),
            Entity("version", "Version", SENSOR, read=lambda: first("version")),
        ]


register(Firewalla())
