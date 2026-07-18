"""Network-wide client aggregation, split out of devices.py.

Merges the client lists reported by every AP/switch a user owns into one
de-duplicated view keyed by MAC, then (if access control is configured)
tags each client approved/blocked via nac.py.
"""
from concurrent.futures import ThreadPoolExecutor

import store
import transports
import devices
import nac
from drivers import registry
from drivers.base import Driver


def _device_clients(dev, timeout=8):
    """Open one connection and read a device's client list (or [] on failure).
    Returns (device_public, clients, error)."""
    drv = registry.get(dev["driverId"])
    if not drv:
        return dev, [], "driver gone"
    try:
        creds = devices._credentials_for(dev)
        with transports.open_connection(dev["transport"], dev["host"],
                                        dev.get("port"), creds, timeout) as conn:
            return dev, (drv.clients(conn) or []), None
    except Exception as e:
        return dev, [], str(e)


def _is_client_source(dev):
    drv = registry.get(dev.get("driverId"))
    # Only devices whose driver actually implements clients() (overrides the
    # empty base) contribute — APs and switches, not firewalls/NAS.
    return drv is not None and type(drv).clients is not Driver.clients


def list_clients(owner_id, is_admin=False, timeout=8):
    """Aggregate the clients seen across every network device the user owns into
    one de-duplicated list, keyed by MAC. A device that appears on both a switch
    port and an AP is merged into a single entry that lists each place it was
    seen. Devices are polled concurrently so the view stays responsive.

    Returns {clients: [...], sources: [{device, count, error?}]}.
    """
    devs = [d for d in store.load()["devices"].values()
            if (is_admin or d.get("ownerId") == owner_id) and _is_client_source(d)]

    merged, sources = {}, []
    if devs:
        with ThreadPoolExecutor(max_workers=min(8, len(devs))) as ex:
            results = list(ex.map(lambda d: _device_clients(d, timeout), devs))
    else:
        results = []
    for dev, clients, error in results:
        name = dev.get("name") or dev["host"]
        sources.append({"device": name, "count": len(clients),
                        **({"error": error} if error else {})})
        for c in clients:
            mac = (c.get("mac") or "").upper()
            if not mac:
                continue
            m = merged.setdefault(mac, {
                "mac": mac, "ip": "", "hostname": "", "vendor": "",
                "kind": "wired", "signal": None, "seen": [], "_authname": False})
            if not m["ip"] and c.get("ip"):
                m["ip"] = c["ip"]
            if not m["vendor"] and c.get("vendor"):
                m["vendor"] = c["vendor"]
            # Hostname precedence: an authoritative (DHCP/lease) name always
            # wins and overrides anything else; otherwise take the first name.
            host = (c.get("hostname") or "").strip()
            if host and (c.get("hostname_authoritative") or not m["hostname"]):
                if c.get("hostname_authoritative") or not m["_authname"]:
                    m["hostname"] = host
                    m["_authname"] = bool(c.get("hostname_authoritative"))
            if c.get("kind") == "wifi":
                m["kind"] = "wifi"
                if c.get("signal") is not None:
                    m["signal"] = c["signal"]
            m["seen"].append({"via": name, "where": c.get("where") or "",
                              "kind": c.get("kind") or "wired",
                              "signal": c.get("signal")})

    # Fill remaining hostnames from reverse-DNS (covers wired devices whose IP we
    # only learned from the firewall's ARP table). Authoritative names are kept.
    need = [m["ip"] for m in merged.values()
            if m["ip"] and not m["hostname"]]
    if need:
        import netutil
        resolved = netutil.resolve_hostnames(need)
        for m in merged.values():
            if not m["hostname"] and m["ip"]:
                m["hostname"] = resolved.get(m["ip"], "")

    for m in merged.values():
        m.pop("_authname", None)
    clients = sorted(merged.values(),
                     key=lambda c: (c["hostname"] or c["ip"] or c["mac"]).lower())

    # Network Access Control: if a NAC-configured device exists, read its
    # allow-list and tag each client approved / blocked so the view can render
    # the per-client Approve/Revoke control. Fails soft (unreachable firewall
    # just leaves clients untagged).
    nac_info = {"configured": False, "enforced": False, "deviceId": None,
                "deviceName": None, "alias": None, "mode": None,
                "managedExternally": False}
    doc = store.load()
    nac_dev = nac._nac_device(owner_id, is_admin, doc)
    if not nac_dev:
        # Not set up yet — surface the first NAC-capable device (e.g. the
        # OPNsense firewall) so the view can offer a one-click setup.
        for d in doc["devices"].values():
            if not (is_admin or d.get("ownerId") == owner_id):
                continue
            drv = registry.get(d.get("driverId"))
            if drv is not None and getattr(drv, "nac_supported", False):
                nac_info["deviceId"] = d["id"]
                nac_info["deviceName"] = d.get("name") or d["host"]
                break
    if nac_dev:
        cfg = nac_dev.get("nac") or {}
        summ = devices._nac_summary(nac_dev)
        managed = cfg.get("managedAliases", [])
        nac_info.update({"configured": True, "enforced": summ["enforced"],
                         "deviceId": nac_dev["id"],
                         "deviceName": nac_dev.get("name") or nac_dev["host"],
                         "alias": cfg.get("alias"), "mode": summ["mode"],
                         "managedExternally": summ["managedExternally"],
                         "managedAliases": managed,
                         "dnsSync": cfg.get("dnsSync") or {"enabled": False, "domain": ""}})
        try:
            drv = registry.get(nac_dev["driverId"])
            with devices.open_conn(nac_dev, timeout=timeout) as conn:
                members = {m.upper() for m in drv.nac_members(conn, cfg["alias"])}
                # Bulk alias membership so each client shows which aliases it's in
                # (cards + pre-ticked edit boxes), read once per alias not once
                # per client.
                alias_idx = drv.alias_member_index(conn, managed) if managed else {}
            for c in clients:
                c["nac"] = "approved" if c["mac"].upper() in members else "blocked"
                c["aliases"] = [
                    {"uuid": u, "name": a["name"]}
                    for u, a in alias_idx.items()
                    if ((c["mac"] if a["type"] == "mac" else (c.get("ip") or ""))
                        .upper() in a["members"])]
            # Track first/last-seen + ignore state; drops ignored devices until
            # they're seen again, and flags genuinely-new arrivals.
            hidden = nac._track_clients(clients, members)
            clients = [c for c in clients if c["mac"].upper() not in hidden]
            nac_info["needsApproval"] = sum(1 for c in clients if c["nac"] != "approved")
        except Exception as e:
            nac_info["error"] = str(e)
    return {"clients": clients, "sources": sources, "nac": nac_info}
