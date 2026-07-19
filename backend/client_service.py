"""Application orchestration for authorized client reads and refreshes."""
import csv
import io
import json
import os
import time

import client_discovery
import client_merge
import client_roster
import nac_service
from context import Actor, TrustedSystem


ROSTER_SCAN_INTERVAL = max(60, int(os.environ.get("HLHQ_CLIENT_SCAN_INTERVAL", "300")))
_last_background_refresh = 0.0


def list_clients(actor: Actor) -> dict:
    """Return the last persisted client snapshot without I/O or mutation."""
    return client_roster.read_snapshot(actor.user_id)


def refresh(actor: Actor | TrustedSystem, owner_id: str | None = None, *, timeout: int = 8) -> dict:
    """Perform an explicit, owner-scoped live discovery and record its result."""
    if isinstance(actor, Actor):
        owner_id = actor.user_id
    elif not isinstance(actor, TrustedSystem) or not owner_id:
        raise ValueError("a trusted context and owner id are required for background refresh")
    observations, sources = client_discovery.discover(owner_id, timeout=timeout)
    merged = client_discovery.resolve_missing_hostnames(client_merge.merge_observations(observations))
    nac, approved, aliases = nac_service.discovery_membership(owner_id, timeout=timeout)
    aliases_by_mac = {}
    if approved is not None:
        for client in merged:
            mac, ip = client["mac"], client.get("ip") or ""
            aliases_by_mac[mac] = [
                {"uuid": uuid, "name": alias.get("name", "")}
                for uuid, alias in aliases.items()
                if (mac if alias.get("type") == "mac" else ip).upper() in alias.get("members", set())
            ]
    return client_roster.record_observations(owner_id, merged, approved=approved,
                                             aliases_by_mac=aliases_by_mac,
                                             full_scan=True, sources=sources, nac=nac)


def refresh_rosters(actor: TrustedSystem, *, timeout: int = 6):
    """Refresh each owner with a client-capable device on the poller schedule.

    This is a trusted background operation, deliberately separate from the
    actor-scoped read path above.  Keeping it here means client discovery has
    one orchestration boundary rather than a legacy owner-ID adapter.
    """
    if not isinstance(actor, TrustedSystem):
        raise ValueError("a trusted context is required for background refresh")
    global _last_background_refresh
    if time.time() - _last_background_refresh < ROSTER_SCAN_INTERVAL:
        return
    import store
    owners = {device.get("ownerId") for device in store.load()["devices"].values()
              if device.get("ownerId") and client_discovery.is_client_source(device)}
    if not owners:
        return
    _last_background_refresh = time.time()
    for owner_id in owners:
        refresh(actor, owner_id, timeout=timeout)


def export_clients(actor: Actor, fmt: str = "json") -> tuple[bytes, str, str]:
    if fmt not in ("csv", "json"):
        raise ValueError("format must be csv or json")
    rows = list_clients(actor)["clients"]
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["name", "hostname", "ip", "mac", "vendor", "kind", "online",
                         "signal_dbm", "access", "first_seen", "last_seen", "notes"])
        for client in rows:
            writer.writerow([_csv_cell(client.get("name")), _csv_cell(client.get("hostname")),
                             client.get("ip") or "", client["mac"], _csv_cell(client.get("vendor")),
                             client.get("kind") or "", "yes" if client.get("online") else "no",
                             client.get("signal") if client.get("signal") is not None else "",
                             client.get("nac") or "", _timestamp(client.get("firstSeen")),
                             _timestamp(client.get("lastSeen")), _csv_cell(client.get("notes"))])
        return output.getvalue().encode(), "text/csv; charset=utf-8", "csv"
    payload = {"exportedAt": int(time.time()), "clients": rows}
    for client in payload["clients"]:
        client["events"] = client_roster.client_history(actor.user_id, client["mac"])["events"]
    return json.dumps(payload, indent=2).encode(), "application/json", "json"


def _timestamp(value):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else ""


def _csv_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
