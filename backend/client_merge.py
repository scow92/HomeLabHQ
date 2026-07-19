"""Pure client-observation normalization and merge rules.

This module intentionally has no store, driver, transport, clock, or network
dependencies.  Discovery adapters create :class:`ClientObservation` values;
the roster and HTTP layers only consume the merged dictionaries returned here.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ClientObservation:
    """One client sighting reported by a network device."""

    mac: str
    source_id: str
    source_name: str
    ip: str = ""
    hostname: str = ""
    hostname_authoritative: bool = False
    vendor: str = ""
    kind: str = "wired"
    signal: int | float | None = None
    where: str = ""


def merge_observations(observations: list[ClientObservation]) -> list[dict]:
    """Merge observations by MAC without performing I/O or mutation.

    DHCP/lease names marked authoritative take precedence.  Wi-Fi sightings
    take precedence for the primary kind and signal, while every sighting is
    retained in ``seen`` for the UI and roster history.
    """
    merged: dict[str, dict] = {}
    for observation in observations:
        mac = (observation.mac or "").upper()
        if not mac:
            continue
        client = merged.setdefault(mac, {
            "mac": mac, "ip": "", "hostname": "", "vendor": "",
            "kind": "wired", "signal": None, "seen": [],
            "_authoritative_hostname": False,
        })
        if not client["ip"] and observation.ip:
            client["ip"] = observation.ip
        if not client["vendor"] and observation.vendor:
            client["vendor"] = observation.vendor
        hostname = observation.hostname.strip()
        if hostname and (observation.hostname_authoritative or not client["hostname"]):
            if observation.hostname_authoritative or not client["_authoritative_hostname"]:
                client["hostname"] = hostname
                client["_authoritative_hostname"] = observation.hostname_authoritative
        if observation.kind == "wifi":
            client["kind"] = "wifi"
            if observation.signal is not None:
                client["signal"] = observation.signal
        client["seen"].append({
            "via": observation.source_name,
            "where": observation.where,
            "kind": observation.kind or "wired",
            "signal": observation.signal,
        })
    for client in merged.values():
        client.pop("_authoritative_hostname", None)
    return list(merged.values())
