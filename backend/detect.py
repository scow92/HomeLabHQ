"""Detection pipeline: probe a connection against every compatible driver and
rank them by confidence. This is the 'smart enough to know what it's talking to'
part — curated drivers each score how sure they are, best match wins, and the
user can override from the ranked list.
"""
from drivers import registry
import transports


def detect(transport, host, port=None, credentials=None, timeout=8):
    """Open a connection and rank compatible drivers by probe confidence.

    Returns a dict: {candidates: [{driverId, displayName, confidence}...],
    banner: str}. Raises transports.ConnectionError if the device can't be
    reached / authenticated.
    """
    conn = transports.open_connection(transport, host, port, credentials, timeout)
    try:
        banner = conn.info()
        candidates = []
        for drv in registry.for_transport(transport):
            try:
                score = float(drv.probe(conn))
            except Exception:
                score = 0.0
            if score > 0:
                candidates.append({
                    "driverId": drv.id,
                    "displayName": drv.display_name,
                    "confidence": round(max(0.0, min(1.0, score)), 3),
                })
        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        return {"candidates": candidates, "banner": banner}
    finally:
        conn.close()


def enumerate_entities(transport, host, port, credentials, driver_id, timeout=8):
    """Connect and list the entities a chosen driver exposes on this device.

    Returns [entity.describe() ...]. Raises if driver unknown or unreachable.
    """
    drv = registry.get(driver_id)
    if not drv:
        raise ValueError(f"unknown driver: {driver_id}")
    conn = transports.open_connection(transport, host, port, credentials, timeout)
    try:
        return [e.describe() for e in drv.entities(conn)]
    finally:
        conn.close()
