"""Network Access Control (allow-list gating), split out of devices.py.

Setup, approval, enforcement, managed aliases, and DNS-sync bookkeeping for a
device's NAC allow-list. Persistent client roster state lives in
``client_roster.py``.
"""
import store
import devices
from drivers import registry


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
