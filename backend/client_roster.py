"""Persistent owner-scoped client roster.

Discovery supplies transient observations; this module is the only place that
turns them into durable identities, presence history, notes, and notifications.
It never opens a device connection.
"""
import os
import time

import devices
import logbuf
import store
from domain import ClientRosterRecord, NacConfiguration

try:
    import push
except Exception:  # optional dependency; the roster remains usable without it
    push = None


CLIENT_EVENTS_MAX = 50
CLIENT_OFFLINE_AFTER = max(60, int(os.environ.get("HLHQ_CLIENT_OFFLINE_AFTER", "600")))
NAC_NEW_WINDOW = 24 * 3600
# A roster is durable identity data, but stale offline entries should not make
# the main JSON document grow forever.  Set to 0 to retain entries indefinitely.
CLIENT_RECORD_RETENTION_DAYS = max(
    0, int(os.environ.get("HLHQ_CLIENT_RECORD_RETENTION_DAYS", "180")))


def roster(doc: dict, owner_id: str, *, create: bool = False) -> dict:
    rosters = doc["clientRosters"]
    return rosters.setdefault(owner_id, {}) if create else rosters.get(owner_id, {})


def _event(record: dict, timestamp: int, event: str, via: str = ""):
    events = record.setdefault("events", [])
    events.append({"ts": timestamp, "ev": event, "via": via})
    if len(events) > CLIENT_EVENTS_MAX:
        del events[:-CLIENT_EVENTS_MAX]


def _via(client: dict) -> str:
    seen = client.get("seen") or []
    if not seen:
        return ""
    sighting = next((item for item in seen if item.get("kind") == "wifi"), seen[0])
    source, where = sighting.get("via") or "", sighting.get("where") or ""
    return f"{source} · {where}" if source and where else source or where


def _serialize(mac: str, record: dict) -> dict:
    return ClientRosterRecord.from_record(record).to_api(mac)


def _notify(owner_id: str, events: list[tuple[str, str, str, str]]):
    if push is None or owner_id not in store.load()["users"]:
        return
    for name, mac, event, via in events:
        title = "Device connected" if event == "up" else "Device disconnected"
        body = (f"{name} came home" + (f" via {via}" if via else "") + "."
                if event == "up" else f"{name} left the network.")
        try:
            push.notify({owner_id}, title, body,
                        data={"type": "presence", "mac": mac, "event": event})
        except Exception as error:
            logbuf.log_event("error", "push_delivery", source="roster", owner_id=owner_id,
                             error=logbuf.redact(str(error)))


def _prune_stale_records(tracked: dict, now: int, present: set[str]):
    """Apply the documented retention period without removing live clients."""
    if not CLIENT_RECORD_RETENTION_DAYS:
        return
    cutoff = now - CLIENT_RECORD_RETENTION_DAYS * 24 * 3600
    for mac, record in list(tracked.items()):
        if (mac not in present and not record.get("online")
                and record.get("lastSeen", record.get("firstSeen", now)) < cutoff):
            tracked.pop(mac, None)


def record_observations(owner_id: str, clients: list[dict], *, approved: set[str] | None = None,
                        aliases_by_mac: dict[str, list[dict]] | None = None,
                        full_scan: bool = True, sources: list[dict] | None = None,
                        nac: dict | None = None) -> dict:
    """Persist one discovery result and return the resulting roster snapshot.

    ``None`` membership means it was not available; it deliberately leaves the
    previously-known NAC status intact.  All writes are centralized here.
    """
    now, presence_events = int(time.time()), []
    normalized = {client["mac"].upper(): client for client in clients if client.get("mac")}
    aliases_by_mac = aliases_by_mac or {}

    def mutate(doc):
        tracked = roster(doc, owner_id, create=True)
        for mac, record in tracked.items():
            if record.get("ignored") and mac not in normalized:
                record["away"] = True
        for mac, client in normalized.items():
            record = tracked.setdefault(mac, {"firstSeen": now, "lastSeen": now})
            via, was_online = _via(client), bool(record.get("online"))
            if record.get("ignored") and record.get("away"):
                record["ignored"] = False
                record.pop("away", None)
            if not was_online:
                _event(record, now, "up", via)
                if record.get("notify"):
                    presence_events.append((record.get("name") or record.get("hostname") or mac,
                                            mac, "up", via))
            record["online"], record["lastSeen"] = True, now
            for key in ("ip", "hostname", "vendor"):
                if client.get(key):
                    record[key] = client[key]
            # A full discovery merges every source and is authoritative for
            # connection type/topology. A partial NAC firewall scan only sees
            # ARP hosts as wired; it must not downgrade a Wi-Fi record or erase
            # the AP sighting that carries its RSSI/location.
            if client.get("kind") and (full_scan or not record.get("kind")):
                record["kind"] = client["kind"]
            if client.get("signal") is not None:
                record["signal"] = client["signal"]
            if full_scan or not record.get("seen"):
                record["seen"] = list(client.get("seen") or [])
            if via:
                record["via"] = via
            if approved is not None:
                record["nacApproved"] = mac in approved
                record["new"] = mac not in approved and now - record["firstSeen"] < NAC_NEW_WINDOW
            if mac in aliases_by_mac:
                record["aliases"] = aliases_by_mac[mac]
        # The OPNsense allow-list is authoritative for approval. Reconcile all
        # tracked MACs, not only devices in this scan: a client can disappear
        # temporarily from ARP/AP tables while remaining explicitly approved.
        if approved is not None:
            for mac, record in tracked.items():
                record["nacApproved"] = mac in approved
                record["new"] = mac not in approved and now - record.get("firstSeen", now) < NAC_NEW_WINDOW
        if full_scan:
            for mac, record in tracked.items():
                if mac in normalized or not record.get("online"):
                    continue
                if now - record.get("lastSeen", 0) >= CLIENT_OFFLINE_AFTER:
                    record["online"] = False
                    _event(record, now, "down", record.get("via", ""))
                    if record.get("notify"):
                        presence_events.append((record.get("name") or record.get("hostname") or mac,
                                                mac, "down", record.get("via", "")))
        _prune_stale_records(tracked, now, set(normalized))
        discovery = doc["meta"].setdefault("clientDiscovery", {})
        snapshot = discovery.setdefault(owner_id, {})
        if sources is not None:
            snapshot["sources"] = list(sources)
        snapshot["updatedAt"] = now
        if nac is not None:
            snapshot["nac"] = nac

    store.update(mutate)
    _notify(owner_id, presence_events)
    return read_snapshot(owner_id)


def read_snapshot(owner_id: str) -> dict:
    """Read the latest persisted roster.  This function is strictly read-only."""
    document = store.load()
    clients = [_serialize(mac, record) for mac, record in roster(document, owner_id).items()
               if not record.get("ignored")]
    clients.sort(key=lambda client: (client["hostname"] or client["ip"] or client["mac"]).lower())
    metadata = (document["meta"].get("clientDiscovery", {}).get(owner_id) or {})
    nac = NacConfiguration.from_mapping(metadata.get("nac")).to_dict()
    if nac.get("configured"):
        nac["needsApproval"] = sum(1 for client in clients
                                    if client.get("online") and client.get("nac") == "blocked")
    return {"clients": clients, "sources": metadata.get("sources", []), "nac": nac,
            "updatedAt": metadata.get("updatedAt")}


def client_history(owner_id: str, mac: str) -> dict:
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    record = roster(store.load(), owner_id).get(mac) or {}
    return {"mac": mac, "online": bool(record.get("online")),
            "firstSeen": record.get("firstSeen"), "lastSeen": record.get("lastSeen"),
            "events": record.get("events", [])}


def events_since(owner_id: str, timestamp) -> dict:
    try:
        timestamp = int(timestamp or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid since timestamp") from error
    count = sum(1 for record in roster(store.load(), owner_id).values()
                for event in record.get("events", []) if event.get("ts", 0) > timestamp)
    return {"since": timestamp, "count": count}


def forget(owner_id: str, macs: list[str]) -> int:
    normalized = [(mac or "").strip().upper() for mac in macs]
    if not normalized:
        raise ValueError("no MAC addresses given")
    if any(not devices._MAC_RE.match(mac) for mac in normalized):
        raise ValueError("invalid MAC address")
    return store.update(lambda document: sum(
        1 for mac in normalized if roster(document, owner_id).pop(mac, None) is not None)) or 0


def ignore(owner_id: str, mac: str) -> dict:
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    def mutate(document):
        now = int(time.time())
        record = roster(document, owner_id, create=True).setdefault(
            mac, {"firstSeen": now, "lastSeen": now})
        record["ignored"], record["away"] = True, False
    store.update(mutate)
    return {"mac": mac, "ignored": True}


def set_metadata(owner_id: str, mac: str, name: str, notes: str, *, notify=None) -> dict:
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    name, notes = (name or "").strip(), (notes or "").strip()
    def mutate(document):
        now = int(time.time())
        record = roster(document, owner_id, create=True).setdefault(
            mac, {"firstSeen": now, "lastSeen": now})
        record["name"], record["notes"] = name, notes
        if notify is not None:
            record["notify"] = bool(notify)
    store.update(mutate)
    return {"mac": mac, "name": name, "notes": notes,
            "notify": bool(roster(store.load(), owner_id).get(mac, {}).get("notify"))}


def record_nac_observations(owner_id: str, clients: list[dict], approved: set[str]) -> list[dict]:
    """Record a partial firewall observation and return newly unapproved clients.

    This keeps NAC's discovery concern separate from general roster history and
    reserves the ``notified`` bookkeeping for the roster persistence boundary.
    """
    before = roster(store.load(), owner_id)
    first_run = not before
    record_observations(owner_id, clients, approved=approved, full_scan=False)
    candidates = {client["mac"].upper(): client for client in clients if client.get("mac")}
    events = []
    def mutate(document):
        tracked = roster(document, owner_id, create=True)
        for mac, client in candidates.items():
            record = tracked.get(mac) or {}
            if mac in approved:
                record.pop("notified", None)
            elif not first_run and not record.get("ignored") and not record.get("notified"):
                record["notified"] = True
                events.append({"mac": mac, "name": client.get("hostname") or client.get("ip") or mac,
                               "ip": client.get("ip") or "", "vendor": client.get("vendor") or "",
                               "where": client.get("where") or ""})
    store.update(mutate)
    return events
