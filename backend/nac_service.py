"""Network-access-control operations, isolated from client roster history."""
import nac as _legacy
import client_roster
import devices
import store
from drivers import registry


# The existing driver-facing operations remain stable while callers migrate to
# this boundary.  None of these functions own general client roster state.
nac_interfaces = _legacy.nac_interfaces
nac_aliases = _legacy.nac_aliases
nac_setup_existing = _legacy.nac_setup_existing
nac_setup = _legacy.nac_setup
nac_approve = _legacy.nac_approve
nac_approve_many = _legacy.nac_approve_many
nac_set_enforcement = _legacy.nac_set_enforcement
get_config = _legacy.get_nac_config
set_config = _legacy.set_nac_config
create_managed_alias = _legacy.create_managed_alias
client_membership = _legacy.client_membership


def configured_device(owner_id, is_admin=False, document=None):
    """Return the visible configured NAC device, if any."""
    return _legacy._nac_device(owner_id, is_admin, document)


def edit_membership(owner_id, is_admin, mac, **kwargs):
    """Apply firewall alias/DNS changes only; local roster metadata is separate."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    device = configured_device(owner_id, is_admin)
    if not device:
        raise ValueError("set up access control before syncing aliases or DNS")
    config = device.get("nac") or {}
    allowed = {alias["uuid"] for alias in config.get("managedAliases", [])}
    driver = registry.get(device["driverId"])
    result = {"mac": mac, "aliasChanges": {}, "dns": None}
    with devices.open_conn(device, timeout=20) as connection:
        for uuid, add in (kwargs.get("alias_changes") or {}).items():
            if uuid in allowed:
                driver.alias_set_member(connection, uuid, kwargs.get("ip") or "", mac, bool(add))
                result["aliasChanges"][uuid] = bool(add)
        sync_dns = kwargs.get("sync_dns")
        if sync_dns is True:
            domain = (config.get("dnsSync") or {}).get("domain", "")
            result["dns"] = driver.dnsmasq_set_host(connection, kwargs.get("hostname") or "",
                                                      kwargs.get("ip") or "", mac, domain)
        elif sync_dns is False:
            result["dns"] = driver.dnsmasq_del_host(connection, mac)
    return result


def scan_new_clients():
    """Background NAC discovery, with roster persistence delegated explicitly."""
    document = store.load()
    device = next((candidate for candidate in document["devices"].values()
                   if (candidate.get("nac") or {}).get("alias")), None)
    if not device:
        return None, []
    driver = registry.get(device.get("driverId"))
    if not driver:
        return None, []
    try:
        with devices.open_conn(device, timeout=15) as connection:
            approved = {mac.upper() for mac in driver.nac_members(connection, device["nac"]["alias"])}
            clients = driver.clients(connection) or []
    except Exception:
        return None, []
    return device, client_roster.record_nac_observations(device["ownerId"], clients, approved)


def _summary(device: dict) -> dict:
    cfg = device.get("nac") or {}
    return {
        "configured": True, "enforced": bool(cfg.get("enabled")),
        "deviceId": device["id"], "deviceName": device.get("name") or device["host"],
        "alias": cfg.get("alias"), "mode": cfg.get("mode"),
        "managedExternally": bool(cfg.get("managedExternally")),
        "managedAliases": cfg.get("managedAliases", []),
        "dnsSync": cfg.get("dnsSync") or {"enabled": False, "domain": ""},
    }


def discovery_membership(owner_id: str, *, timeout: int = 8) -> tuple[dict, set[str] | None, dict]:
    """Read NAC membership for a discovery cycle only.

    A client-list GET must not call this function: it opens the firewall and is
    intentionally reserved for explicit/background refreshes.
    """
    document = store.load()
    device = _legacy._nac_device(owner_id, False, document)
    if not device:
        # Surface a candidate capable device without connecting to it.
        info = {"configured": False, "enforced": False, "deviceId": None,
                "deviceName": None, "alias": None, "mode": None,
                "managedExternally": False}
        for candidate in document["devices"].values():
            driver = registry.get(candidate.get("driverId"))
            if candidate.get("ownerId") == owner_id and driver and getattr(driver, "nac_supported", False):
                info["deviceId"] = candidate["id"]
                info["deviceName"] = candidate.get("name") or candidate["host"]
                break
        return info, None, {}
    info = _summary(device)
    try:
        driver = registry.get(device["driverId"])
        with devices.open_conn(device, timeout=timeout) as connection:
            members = {mac.upper() for mac in driver.nac_members(connection, device["nac"]["alias"])}
            aliases = driver.alias_member_index(connection, info["managedAliases"]) \
                if info["managedAliases"] else {}
        return info, members, aliases
    except Exception as error:
        info["error"] = str(error)
        return info, None, {}
