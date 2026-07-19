"""Live discovery of clients from eligible owner-scoped devices."""
from concurrent.futures import ThreadPoolExecutor

import devices
import netutil
import store
import transports
from client_merge import ClientObservation
from domain import safe_error
from drivers import registry
from drivers.base import Driver


def is_client_source(device: dict) -> bool:
    """Whether a device's driver supplies a real client-list operation."""
    driver = registry.get(device.get("driverId"))
    return driver is not None and type(driver).clients is not Driver.clients


def _read_device(device: dict, timeout: int):
    driver = registry.get(device["driverId"])
    if not driver:
        return device, [], "driver gone"
    try:
        credentials = devices._credentials_for(device)
        with transports.open_connection(device["transport"], device["host"],
                                        device.get("port"), credentials, timeout) as connection:
            return device, driver.clients(connection) or [], None
    except Exception as error:
        return device, [], safe_error(error)


def discover(owner_id: str, *, timeout: int = 8) -> tuple[list[ClientObservation], list[dict]]:
    """Query client-capable devices owned by ``owner_id``.

    The return value deliberately contains observations rather than roster
    records.  Calling this function has no persistent side effects.
    """
    devices_for_owner = [device for device in store.load()["devices"].values()
                         if device.get("ownerId") == owner_id and is_client_source(device)]
    if devices_for_owner:
        with ThreadPoolExecutor(max_workers=min(8, len(devices_for_owner))) as executor:
            results = list(executor.map(lambda device: _read_device(device, timeout),
                                        devices_for_owner))
    else:
        results = []
    observations, sources = [], []
    for device, reported_clients, error in results:
        name = device.get("name") or device["host"]
        sources.append({"device": name, "count": len(reported_clients),
                        **({"error": error} if error else {})})
        for client in reported_clients:
            if not isinstance(client, dict):
                continue
            try:
                observations.append(ClientObservation(
                    mac=str(client.get("mac") or ""), source_id=device["id"], source_name=name,
                    ip=str(client.get("ip") or ""), hostname=str(client.get("hostname") or ""),
                    hostname_authoritative=bool(client.get("hostname_authoritative")),
                    vendor=str(client.get("vendor") or ""), kind=str(client.get("kind") or "wired"),
                    signal=client.get("signal"), where=str(client.get("where") or ""),
                ))
            except (TypeError, ValueError):
                # A malformed vendor row is one observation, not a failed scan.
                continue
    return observations, sources


def resolve_missing_hostnames(clients: list[dict]) -> list[dict]:
    """Enrich merged results with reverse DNS, retaining authoritative names."""
    needed = [client["ip"] for client in clients if client.get("ip") and not client.get("hostname")]
    if not needed:
        return clients
    resolved = netutil.resolve_hostnames(needed)
    for client in clients:
        if not client.get("hostname") and client.get("ip"):
            client["hostname"] = resolved.get(client["ip"], "")
    return clients
