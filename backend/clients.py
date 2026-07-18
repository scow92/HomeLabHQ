"""Network-wide client aggregation, split out of devices.py.

Merges the client lists reported by every AP/switch a user owns into one
de-duplicated view keyed by MAC, then (if access control is configured)
tags each client approved/blocked via nac.py.
"""
import csv
import io
import json
import os
import time
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
            if d.get("ownerId") == owner_id and _is_client_source(d)]

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
    clients = list(merged.values())

    # Network Access Control: if a NAC-configured device exists, read its
    # allow-list first so both live and offline clients can be tagged
    # approved / blocked (the per-client Approve/Revoke control). Fails soft
    # (unreachable firewall just leaves clients untagged).
    nac_info = {"configured": False, "enforced": False, "deviceId": None,
                "deviceName": None, "alias": None, "mode": None,
                "managedExternally": False}
    doc = store.load()
    nac_dev = nac._nac_device(owner_id, False, doc)
    if not nac_dev:
        # Not set up yet — surface the first NAC-capable device (e.g. the
        # OPNsense firewall) so the view can offer a one-click setup.
        for d in doc["devices"].values():
            if d.get("ownerId") != owner_id:
                continue
            drv = registry.get(d.get("driverId"))
            if drv is not None and getattr(drv, "nac_supported", False):
                nac_info["deviceId"] = d["id"]
                nac_info["deviceName"] = d.get("name") or d["host"]
                break
    members, alias_idx, members_known = set(), {}, False
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
            members_known = True
        except Exception as e:
            nac_info["error"] = str(e)

    # Persistent roster: record/annotate the live clients (first/last seen,
    # name/notes, ignore state, connect events), drop ignored ones, then append
    # every tracked-but-absent client so the view shows online vs offline and
    # when each device was last connected. This is a complete scan of the
    # current owner's client sources, so absent clients may flip offline.
    hidden, offline = nac._track_clients(
        owner_id, clients, members if members_known else None, full_scan=True)
    clients = [c for c in clients if c["mac"].upper() not in hidden]
    clients.extend(offline)
    clients.sort(key=lambda c: (c["hostname"] or c["ip"] or c["mac"]).lower())

    if members_known:
        for c in clients:
            c["nac"] = "approved" if c["mac"].upper() in members else "blocked"
            c["aliases"] = [
                {"uuid": u, "name": a["name"]}
                for u, a in alias_idx.items()
                if ((c["mac"] if a["type"] == "mac" else (c.get("ip") or ""))
                    .upper() in a["members"])]
        # The badge counts devices awaiting a decision — offline history
        # entries shouldn't nag, so only currently-connected ones count.
        nac_info["needsApproval"] = sum(
            1 for c in clients if c["nac"] != "approved" and c.get("online"))
    return {"clients": clients, "sources": sources, "nac": nac_info}


# ---- roster export -----------------------------------------------------------
def _csv_cell(v):
    """Neutralize spreadsheet formula injection: device-supplied strings
    (hostnames, vendors) could start with =/+/-/@ and execute when the CSV
    is opened in Excel/Sheets."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def export_clients(owner_id, is_admin=False, fmt="json"):
    """The client roster as a downloadable snapshot (refactor.md 5.8).
    CSV is a flat spreadsheet-friendly table; JSON additionally carries each
    client's stored connect/disconnect history. Returns (bytes, mime, ext)."""
    if fmt not in ("csv", "json"):
        raise ValueError("format must be csv or json")
    rows = list_clients(owner_id, is_admin=is_admin)["clients"]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["name", "hostname", "ip", "mac", "vendor", "kind",
                    "online", "signal_dbm", "access", "first_seen",
                    "last_seen", "notes"])
        def iso(ts):
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(ts)) if ts else ""
        for c in rows:
            w.writerow([_csv_cell(c.get("name")), _csv_cell(c.get("hostname")),
                        c.get("ip") or "", c["mac"],
                        _csv_cell(c.get("vendor")), c.get("kind") or "",
                        "yes" if c.get("online", True) else "no",
                        c.get("signal") if c.get("signal") is not None else "",
                        c.get("nac") or "", iso(c.get("firstSeen")),
                        iso(c.get("lastSeen")), _csv_cell(c.get("notes"))])
        return buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "csv"
    track = nac._roster(store.load(), owner_id)
    for c in rows:
        c["events"] = (track.get(c["mac"].upper()) or {}).get("events", [])
    payload = {"exportedAt": int(time.time()), "clients": rows}
    return (json.dumps(payload, indent=2).encode("utf-8"),
            "application/json", "json")


# ---- background roster scan --------------------------------------------------
# Connection history should accrue even when nobody has the Access tab open, so
# the poller loop calls track_roster() every cycle; it rate-limits itself and
# scans each owner's devices independently, preserving roster isolation.
ROSTER_SCAN_INTERVAL = max(
    60, int(os.environ.get("HLHQ_CLIENT_SCAN_INTERVAL", "300")))
_last_scan = 0.0


def track_roster():
    """Rate-limited background client scan for the persistent roster. A cheap
    no-op when no client sources exist or the interval hasn't elapsed."""
    global _last_scan
    if time.time() - _last_scan < ROSTER_SCAN_INTERVAL:
        return
    owners = {d.get("ownerId") for d in store.load()["devices"].values()
              if d.get("ownerId") and _is_client_source(d)}
    if not owners:
        return
    _last_scan = time.time()
    for owner_id in owners:
        list_clients(owner_id=owner_id, is_admin=False, timeout=6)
