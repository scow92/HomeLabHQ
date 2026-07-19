"""Network Access Control (allow-list gating), split out of devices.py.

Setup/approval/enforcement of a device's NAC allow-list, delegated to the
driver, plus client tracking (first/last seen, ignore state) and the
managed-alias / DNS-sync bookkeeping the Settings screen edits.
"""
import os
import time
import traceback

import store
import devices
import client_roster
from drivers import registry

try:
    import push
except Exception:  # push deps optional; NAC/roster tracking still runs without it
    push = None


def nac_interfaces(dev_id):
    """Interfaces the NAC rule can attach to, for the setup picker."""
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        return drv.nac_interfaces(conn)


def nac_aliases(dev_id):
    """Existing firewall aliases, for the 'use an existing alias' picker."""
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        return drv.nac_aliases(conn)


def _save_nac(dev_id, cfg):
    def _mut(doc):
        d = doc["devices"].get(dev_id)
        if not d:
            return None
        d["nac"] = cfg
        return dict(d)
    rec = store.update(_mut)
    if not rec:
        raise ValueError("device not found")
    return devices._public(rec)


def nac_setup_existing(dev_id, alias_uuid):
    """Link the device's access control to a pre-existing alias (e.g. the one
    Network Manager already maintains). Membership-only: no rules are created and
    the user's own firewall rule keeps enforcing it. Returns the public record."""
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        res = drv.nac_ensure_existing(conn, alias_uuid)
    return _save_nac(dev_id, {
        "alias": res["alias"], "aliasUuid": res["aliasUuid"],
        "aliasType": res.get("aliasType"), "mode": "existing",
        "interface": None, "passUuid": None, "blockUuid": None,
        "enabled": False, "managedExternally": True,
    })


def nac_setup(dev_id, alias, interface, seed_macs=None):
    """Create the allow-list alias + top-level rules on the firewall and record
    the resulting config on the device. Enforcement starts OFF (the deny rule is
    created disabled). Returns the public device record."""
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        res = drv.nac_ensure(conn, alias, interface, seed_macs or [])
    return _save_nac(dev_id, {
        "alias": alias.strip(), "interface": interface,
        "aliasUuid": res.get("aliasUuid"),
        "passUuid": res.get("passUuid"),
        "blockUuid": res.get("blockUuid"),
        "aliasType": "mac", "mode": "managed",
        "enabled": False,  # enforcement is an explicit, later opt-in
        "managedExternally": False,
    })


def nac_approve(dev_id, mac, approved):
    """Approve (add) or revoke (remove) one client MAC in the allow-list."""
    dev = devices.get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    alias = (dev.get("nac") or {}).get("alias")
    if not alias:
        raise ValueError("access control is not set up on this device")
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        return drv.nac_set_member(conn, alias, mac, bool(approved))


def nac_approve_many(dev_id, macs, approved):
    """Approve (add) or revoke (remove) a batch of client MACs in the
    allow-list over one firewall connection — the Access tab's bulk approve.
    Invalid MACs fail the whole batch up front rather than half-applying."""
    dev = devices.get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    alias = (dev.get("nac") or {}).get("alias")
    if not alias:
        raise ValueError("access control is not set up on this device")
    macs = [(m or "").strip().upper() for m in macs]
    if not macs:
        raise ValueError("no MAC addresses given")
    for m in macs:
        if not devices._MAC_RE.match(m):
            raise ValueError(f"invalid MAC address: {m}")
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        for m in macs:
            drv.nac_set_member(conn, alias, m, bool(approved))
    return {"updated": len(macs), "approved": bool(approved)}


def nac_set_enforcement(dev_id, enabled):
    """Flip the master enforcement switch (the deny-all rule) and persist it.
    Returns the public device record."""
    dev = devices.get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    block_uuid = (dev.get("nac") or {}).get("blockUuid")
    if not block_uuid:
        raise ValueError("access control is not set up on this device")
    with devices.device_conn(dev_id, require="nac") as (dev, drv, conn):
        res = drv.nac_enforcement(conn, block_uuid, bool(enabled))

    def _mut(doc):
        d = doc["devices"].get(dev_id)
        if not d or not d.get("nac"):
            return None
        d["nac"]["enabled"] = bool(res.get("enabled"))
        return dict(d)

    rec = store.update(_mut)
    if not rec:
        raise ValueError("device not found")
    return devices._public(rec)


def _nac_device(owner_id, is_admin, doc=None):
    """The user's first NAC-configured device, or None. (Typically the one
    OPNsense firewall that gates the network.)"""
    doc = doc or store.load()
    for d in doc["devices"].values():
        if (is_admin or d.get("ownerId") == owner_id) and (d.get("nac") or {}).get("alias"):
            return d
    return None


NAC_NEW_WINDOW = 24 * 3600  # a client counts as "new" for 24h after first sight

# ---- persistent client roster ------------------------------------------------
# Every client ever seen is kept under clientRosters[owner_id]. Each owner sees
# and manages only the clients observed through that owner's devices. This is a
# deliberately per-owner product model; administrators do not get an implicit
# shared roster simply by virtue of their role.
CLIENT_EVENTS_MAX = 50   # connect/disconnect events kept per client
# A client absent from a scan only flips offline once it hasn't been seen for
# this long — APs briefly drop entries from their association tables, and the
# NAC firewall's ARP scan (every poll cycle) keeps lastSeen fresh in between.
CLIENT_OFFLINE_AFTER = max(
    60, int(os.environ.get("HLHQ_CLIENT_OFFLINE_AFTER", "600")))


def _roster(doc, owner_id, create=False):
    rosters = doc["clientRosters"]
    if create:
        return rosters.setdefault(owner_id, {})
    return rosters.get(owner_id, {})


def _push_event(rec, ts, ev, via=""):
    """Append one connect ('up') / disconnect ('down') event, bounded."""
    evs = rec.setdefault("events", [])
    evs.append({"ts": ts, "ev": ev, "via": via})
    if len(evs) > CLIENT_EVENTS_MAX:
        del evs[:-CLIENT_EVENTS_MAX]


def _mark_seen(rec, now, via=""):
    """Bump a roster record for a client present in a scan: refresh lastSeen,
    clear an armed ignore, and record a connect event on an offline→online
    flip (including the very first sighting)."""
    if rec.get("ignored") and rec.get("away"):
        rec["ignored"] = False   # seen again after going away
        rec.pop("away", None)
    if not rec.get("online", False):
        _push_event(rec, now, "up", via)
    rec["online"] = True
    rec["lastSeen"] = now


def _client_via(c):
    """Where a live client was seen, for the roster/events: prefer the Wi-Fi
    source (the AP it's associated with), else the first source; include the
    port/SSID detail when the driver reported one."""
    seen = c.get("seen") or []
    if not seen:
        return ""
    wifi = [s for s in seen if s.get("kind") == "wifi"]
    pick = wifi[0] if wifi else seen[0]
    via = pick.get("via") or ""
    where = pick.get("where") or ""
    return f"{via} · {where}" if via and where else (via or where)


def _offline_client(mac, rec):
    """A client-list record for a tracked device that isn't in the live scan,
    built from the identity remembered at its last sighting."""
    return {
        "mac": mac,
        "ip": rec.get("ip", ""),
        "hostname": rec.get("hostname", ""),
        "vendor": rec.get("vendor", ""),
        "kind": rec.get("kind", "wired"),
        "signal": None,
        "seen": [],
        "via": rec.get("via", ""),
        "online": bool(rec.get("online")),
        "firstSeen": rec.get("firstSeen"),
        "lastSeen": rec.get("lastSeen"),
        "name": rec.get("name", ""),
        "notes": rec.get("notes", ""),
        "notify": bool(rec.get("notify")),
        "new": False,
    }


def _track_clients(owner_id, clients, approved, full_scan=False):
    """Update the persistent client roster and annotate each live client.

    Persists per-MAC records under clientRosters[owner_id]: firstSeen/lastSeen, the
    user's name/notes, ignore state, an online flag, a bounded
    connect/disconnect event log, and the identity details (ip, hostname,
    vendor, kind, via) needed to render the client after it disconnects.
    Records are kept until explicitly forgotten (forget_client) — that's the
    point of the roster — with growth bounded per record by CLIENT_EVENTS_MAX.

    `approved` is the NAC allow-list MAC set, or None when membership is
    unknown (NAC unconfigured or unreachable) — the 'new' flag needs it.
    `full_scan` marks a complete scan of this owner's devices. Only those may
    flip absent clients offline, since a partial device view cannot prove a
    client has left.

    An ignored client stays hidden until it disappears and is seen again
    (mirrors Network Manager's 'skip until seen again').

    A client with its opt-in `notify` flag set fires a push notification
    ("phone came home" / "left") on each online/offline transition — reusing
    the same connect/disconnect edges the roster events already record, so
    this is a pure side effect, not a second tracking pass. The persisted
    `online` flag flips exactly once per real transition regardless of how
    many callers (interactive views, the background scan) observe it, so
    this never double-fires.

    Returns (hidden, offline): the ignored MACs to drop from the live list,
    and the tracked-but-absent client records to append to it.
    """
    now = int(time.time())
    present = {c["mac"].upper() for c in clients}
    hidden = set()
    offline = []
    presence_events = []  # (name, mac, "up"|"down", via) — notified after commit

    def _mut(doc):
        track = _roster(doc, owner_id, create=True)
        # Any ignored device not seen this round has gone away — arm its return.
        for mac, rec in track.items():
            if rec.get("ignored") and mac not in present:
                rec["away"] = True
        for c in clients:
            mac = c["mac"].upper()
            rec = track.get(mac)
            if rec is None:
                rec = {"firstSeen": now, "lastSeen": now}
                track[mac] = rec
            via = _client_via(c)
            was_online = rec.get("online", False)
            _mark_seen(rec, now, via)
            # Seeing a client at all is a definitive "it's online" signal even
            # from a partial (single-device) scan — unlike the offline flip
            # below, which needs a full network view to be sure it's gone.
            if rec.get("notify") and not was_online:
                presence_events.append(
                    (rec.get("name") or rec.get("hostname") or mac, mac, "up", via))
            # Remember identity so the client can still be shown once offline.
            for k, v in (("ip", c.get("ip")), ("hostname", c.get("hostname")),
                         ("vendor", c.get("vendor")), ("kind", c.get("kind")),
                         ("via", via)):
                if v:
                    rec[k] = v
            c["online"] = True
            c["lastSeen"] = now
            c["firstSeen"] = rec["firstSeen"]
            c["name"] = rec.get("name", "")     # user's friendly name (local)
            c["notes"] = rec.get("notes", "")   # free-text notes (local)
            c["notify"] = bool(rec.get("notify"))  # opt-in presence alerts (local)
            c["new"] = (approved is not None and mac not in approved
                        and now - rec["firstSeen"] < NAC_NEW_WINDOW)
            if rec.get("ignored"):
                c["ignored"] = True
                hidden.add(mac)
        for mac, rec in track.items():
            if mac in present:
                continue
            if (full_scan and rec.get("online")
                    and now - rec.get("lastSeen", 0) >= CLIENT_OFFLINE_AFTER):
                rec["online"] = False
                _push_event(rec, now, "down", rec.get("via", ""))
                if rec.get("notify"):
                    presence_events.append(
                        (rec.get("name") or rec.get("hostname") or mac, mac, "down",
                         rec.get("via", "")))
            if not rec.get("ignored"):
                offline.append(_offline_client(mac, rec))

    store.update(_mut)
    if presence_events:
        _notify_presence(owner_id, presence_events)
    return hidden, offline


def _notify_presence(owner_id, events):
    """Push a per-owner roster transition only to that roster's owner."""
    if push is None:
        return
    doc = store.load()
    if owner_id not in doc["users"]:
        return
    for name, mac, ev, via in events:
        if ev == "up":
            title, body = "Device connected", f"{name} came home" + (f" via {via}" if via else "") + "."
        else:
            title, body = "Device disconnected", f"{name} left the network."
        try:
            push.notify({owner_id}, title, body, data={"type": "presence", "mac": mac, "event": ev})
        except Exception:
            traceback.print_exc()


def client_history(owner_id, mac):
    """The stored connect/disconnect events for one client (oldest first),
    for the Access tab's per-client history panel."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    rec = _roster(store.load(), owner_id).get(mac) or {}
    return {"mac": mac, "online": bool(rec.get("online")),
            "firstSeen": rec.get("firstSeen"), "lastSeen": rec.get("lastSeen"),
            "events": rec.get("events", [])}


def events_since(owner_id, ts):
    """Count stored roster connect/disconnect events newer than `ts` — the
    Access tab's "new events since last visit" badge. Cheap: one read of the
    store doc, no device I/O."""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid since timestamp")
    track = _roster(store.load(), owner_id)
    count = sum(1 for rec in track.values()
                for e in rec.get("events") or [] if e.get("ts", 0) > ts)
    return {"since": ts, "count": count}


def forget_client(owner_id, mac):
    """Drop a client's roster record (name, notes, connection history). It
    reappears as a brand-new device if it ever connects again."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")

    def _mut(doc):
        _roster(doc, owner_id).pop(mac, None)

    store.update(_mut)
    return {"mac": mac, "forgotten": True}


def forget_clients(owner_id, macs):
    """Bulk forget_client: drop several roster records in one store write —
    the Access tab's forget-all-offline. Invalid MACs fail the batch."""
    macs = [(m or "").strip().upper() for m in macs]
    if not macs:
        raise ValueError("no MAC addresses given")
    for m in macs:
        if not devices._MAC_RE.match(m):
            raise ValueError(f"invalid MAC address: {m}")

    def _mut(doc):
        track = _roster(doc, owner_id)
        return sum(1 for m in macs if track.pop(m, None) is not None)

    return {"forgotten": store.update(_mut) or 0}


def nac_ignore(owner_id, mac):
    """Dismiss a client from the approval list until it's seen again."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")

    def _mut(doc):
        now = int(time.time())
        track = _roster(doc, owner_id, create=True)
        rec = track.setdefault(mac, {"firstSeen": now, "lastSeen": now})
        rec["ignored"] = True
        rec["away"] = False

    store.update(_mut)
    return {"mac": mac, "ignored": True}


def set_client_meta(owner_id, mac, name, notes, notify=None):
    """Persist a client's friendly name + notes (and, opt-in, presence push
    notifications) under clientRosters[owner_id] (local only — no firewall
    interaction). `notify=None` leaves the flag untouched. Returns the
    stored values."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    name = (name or "").strip()
    notes = (notes or "").strip()

    def _mut(doc):
        now = int(time.time())
        track = _roster(doc, owner_id, create=True)
        rec = track.setdefault(mac, {"firstSeen": now, "lastSeen": now})
        rec["name"] = name
        rec["notes"] = notes
        if notify is not None:
            rec["notify"] = bool(notify)

    store.update(_mut)
    doc = store.load()
    rec = _roster(doc, owner_id).get(mac) or {}
    return {"mac": mac, "name": name, "notes": notes, "notify": bool(rec.get("notify"))}


def get_nac_config(owner_id, is_admin=False):
    """Managed-alias + DNS-sync settings for the Settings screen. These live on
    the NAC-configured firewall device's nac config."""
    doc = store.load()
    nac_dev = _nac_device(owner_id, is_admin, doc)
    if not nac_dev:
        return {"configured": False, "managedAliases": [],
                "dnsSync": {"enabled": False, "domain": ""}}
    cfg = nac_dev.get("nac") or {}
    return {"configured": True, "deviceId": nac_dev["id"],
            "managedAliases": cfg.get("managedAliases", []),
            "dnsSync": cfg.get("dnsSync") or {"enabled": False, "domain": ""}}


def set_nac_config(owner_id, is_admin, managed_aliases, dns_sync):
    """Save which firewall aliases show as per-client tick boxes and whether
    hostname DNS sync is on. Stored on the NAC device's nac config."""
    doc = store.load()
    nac_dev = _nac_device(owner_id, is_admin, doc)
    if not nac_dev:
        raise ValueError("set up access control before configuring this")
    ma = []
    for a in managed_aliases or []:
        uuid = (a.get("uuid") or "").strip()
        if uuid:
            ma.append({"uuid": uuid, "name": a.get("name") or "",
                       "type": a.get("type") or ""})
    ds = {"enabled": bool((dns_sync or {}).get("enabled")),
          "domain": ((dns_sync or {}).get("domain") or "").strip()}

    def _mut(d):
        dev = d["devices"].get(nac_dev["id"])
        if not dev or not dev.get("nac"):
            return None
        dev["nac"]["managedAliases"] = ma
        dev["nac"]["dnsSync"] = ds
        return True

    if not store.update(_mut):
        raise ValueError("access control not configured")
    return {"managedAliases": ma, "dnsSync": ds}


def create_managed_alias(owner_id, is_admin, name, atype="host"):
    """Create a new firewall alias and add it to the managed set so devices can
    be assigned to it from the edit-client tick boxes. Idempotent by name."""
    nac_dev = _nac_device(owner_id, is_admin)
    if not nac_dev:
        raise ValueError("set up access control before adding aliases")
    drv = registry.get(nac_dev["driverId"])
    with devices.open_conn(nac_dev, timeout=15) as conn:
        res = drv.alias_create(conn, name, atype)

    def _mut(d):
        dev = d["devices"].get(nac_dev["id"])
        if not dev or not dev.get("nac"):
            return None
        ma = dev["nac"].setdefault("managedAliases", [])
        if not any(a.get("uuid") == res["uuid"] for a in ma):
            ma.append({"uuid": res["uuid"], "name": res["name"],
                       "type": res["type"]})
        return {"managedAliases": ma}

    out = store.update(_mut)
    if not out:
        raise ValueError("access control not configured")
    return {"alias": res, "managedAliases": out["managedAliases"]}


def client_membership(owner_id, is_admin, mac, ip=""):
    """Prefill for the edit-client modal: for the configured managed aliases,
    whether this client is a member, plus current DNS-sync state. `ip` is passed
    from the client card so we don't have to re-poll the network."""
    mac = (mac or "").strip().upper()
    doc = store.load()
    nac_dev = _nac_device(owner_id, is_admin, doc)
    if not nac_dev:
        return {"configured": False, "aliases": [],
                "dnsSync": {"enabled": False, "domain": ""}, "dnsSynced": False}
    cfg = nac_dev.get("nac") or {}
    managed = cfg.get("managedAliases", [])
    ds = cfg.get("dnsSync") or {"enabled": False, "domain": ""}
    drv = registry.get(nac_dev["driverId"])
    with devices.open_conn(nac_dev, timeout=10) as conn:
        uuids = [a["uuid"] for a in managed]
        member = drv.alias_membership(conn, uuids, ip, mac) if uuids else {}
        dns_synced = drv.dnsmasq_synced(conn, mac) if ds.get("enabled") else False
    aliases = [{"uuid": a["uuid"], "name": a.get("name", ""),
                "type": a.get("type", ""), "member": member.get(a["uuid"], False)}
               for a in managed]
    return {"configured": True, "aliases": aliases, "dnsSync": ds,
            "dnsSynced": dns_synced}


def edit_client(owner_id, is_admin, mac, ip="", name="", notes="",
                hostname="", sync_dns=None, alias_changes=None, notify=None):
    """Apply an edit-client save: always persist the local name/notes/notify
    flag, then (if access control is set up) apply firewall-alias membership
    changes and DNS sync. `sync_dns` is None to leave DNS untouched, True to
    publish the hostname, False to remove it. `notify` is None to leave the
    opt-in presence-alert flag untouched. Returns what was applied."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    meta = set_client_meta(owner_id, mac, name, notes, notify=notify)
    res = {"mac": mac, "name": meta["name"], "notes": meta["notes"],
           "notify": meta["notify"], "aliasChanges": {}, "dns": None}
    alias_changes = alias_changes or {}
    if not alias_changes and sync_dns is None:
        return res  # local-only edit, no firewall work

    nac_dev = _nac_device(owner_id, is_admin)
    if not nac_dev:
        raise ValueError("set up access control before syncing aliases or DNS")
    cfg = nac_dev.get("nac") or {}
    allowed = {a["uuid"] for a in cfg.get("managedAliases", [])}
    domain = (cfg.get("dnsSync") or {}).get("domain", "")
    drv = registry.get(nac_dev["driverId"])
    with devices.open_conn(nac_dev, timeout=20) as conn:
        for uuid, add in alias_changes.items():
            if uuid not in allowed:
                continue  # only touch aliases the admin chose to manage
            drv.alias_set_member(conn, uuid, ip, mac, bool(add))
            res["aliasChanges"][uuid] = bool(add)
        if sync_dns is True:
            res["dns"] = drv.dnsmasq_set_host(conn, hostname, ip, mac, domain)
        elif sync_dns is False:
            res["dns"] = drv.dnsmasq_del_host(conn, mac)
    return res


def scan_new_clients():
    """Background scan for newly-appeared, unapproved clients on the NAC firewall
    (mirrors Network Manager's pending-device detection). Reads the firewall's
    live client list (ARP + DHCP leases) and its allow-list, updates tracking,
    and returns (nac_device, [events]) where each event is a device that just
    showed up and isn't approved/ignored — so the poller can push a "new device"
    notification exactly once per device. Returns (None, []) when NAC isn't set
    up or the firewall is unreachable. Silent on the very first scan so a fresh
    instance doesn't fire a notification for every existing device."""
    doc = store.load()
    nac_dev = next((d for d in doc["devices"].values()
                    if (d.get("nac") or {}).get("alias")), None)
    if not nac_dev:
        return None, []
    drv = registry.get(nac_dev.get("driverId"))
    if not drv:
        return None, []
    cfg = nac_dev["nac"]
    try:
        with devices.open_conn(nac_dev, timeout=15) as conn:
            members = {m.upper() for m in drv.nac_members(conn, cfg["alias"])}
            clients = drv.clients(conn) or []
    except Exception:
        return None, []

    now = int(time.time())
    events = []

    def _mut(doc):
        track = _roster(doc, nac_dev["ownerId"], create=True)
        first_run = not track   # a fresh instance: seed silently, don't notify
        present = set()
        for c in clients:
            mac = (c.get("mac") or "").upper()
            if not mac:
                continue
            present.add(mac)
            approved = mac in members
            rec = track.get(mac)
            if rec is None:
                rec = {"firstSeen": now, "lastSeen": now}
                track[mac] = rec
            # Bump the roster (lastSeen, online flag, connect event) so
            # connection history accrues from this background scan too. The
            # firewall scan doesn't know which AP/switch the client is on, so
            # the event carries no location.
            _mark_seen(rec, now)
            if approved:
                rec.pop("notified", None)  # re-notify if it's ever removed later
                continue
            if rec.get("ignored") or rec.get("notified"):
                continue
            rec["notified"] = True
            if not first_run:
                events.append({
                    "mac": mac,
                    "name": (c.get("hostname") or c.get("ip") or mac),
                    "ip": c.get("ip") or "",
                    "vendor": c.get("vendor") or "",
                    "where": c.get("where") or "",
                })
        # Arm the "seen again" return for ignored devices that have left.
        for mac, rec in track.items():
            if rec.get("ignored") and mac not in present:
                rec["away"] = True

    store.update(_mut)
    return nac_dev, events


# Compatibility surface for integrations that imported roster helpers from the
# pre-Phase-4 NAC module.  Production code uses client_roster directly; keeping
# these aliases avoids a flag-day break while ensuring all future roster writes
# pass through the dedicated service.
CLIENT_EVENTS_MAX = client_roster.CLIENT_EVENTS_MAX
CLIENT_OFFLINE_AFTER = client_roster.CLIENT_OFFLINE_AFTER
NAC_NEW_WINDOW = client_roster.NAC_NEW_WINDOW


def _roster(doc, owner_id, create=False):
    return client_roster.roster(doc, owner_id, create=create)


def _track_clients(owner_id, clients, approved, full_scan=False):
    snapshot = client_roster.record_observations(owner_id, clients, approved=approved,
                                                  full_scan=full_scan)
    present = {(client.get("mac") or "").upper() for client in clients}
    tracked = client_roster.roster(store.load(), owner_id)
    hidden = {mac for mac in present if tracked.get(mac, {}).get("ignored")}
    offline = [client for client in snapshot["clients"] if client["mac"] not in present]
    return hidden, offline


client_history = client_roster.client_history
events_since = client_roster.events_since
nac_ignore = client_roster.ignore
set_client_meta = client_roster.set_metadata


def forget_client(owner_id, mac):
    mac = (mac or "").strip().upper()
    client_roster.forget(owner_id, [mac])
    return {"mac": mac, "forgotten": True}


def forget_clients(owner_id, macs):
    return {"forgotten": client_roster.forget(owner_id, macs)}
