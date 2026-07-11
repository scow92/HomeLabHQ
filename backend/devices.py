"""Device persistence + live reads.

A device record stores everything needed to reconnect and read it later: host,
transport, port, the chosen driver, the entity keys the user opted into, and a
reference to its encrypted credential blob. Credentials never live in the
device record itself — they're Fernet-encrypted in the `credentials` map and
decrypted only at connect time.
"""
import re
import secrets
import time

import crypto
import store
import transports
from drivers import registry

_UNSET = object()  # sentinel: "field not provided" vs "set to null/empty"


def _public(dev: dict) -> dict:
    """Device record safe to return to the client (no credential material)."""
    return {
        "id": dev["id"],
        "ownerId": dev["ownerId"],
        "name": dev.get("name") or dev["host"],
        "host": dev["host"],
        "port": dev.get("port"),
        "transport": dev["transport"],
        "driverId": dev.get("driverId"),
        "entities": dev.get("entities", []),
        "dashboardId": dev.get("dashboardId"),  # None => Unassigned
        "order": dev.get("order", 0),           # user-defined sort within a view
        "hiddenInterfaces": dev.get("hiddenInterfaces", []),
        "apBinding": dev.get("apBinding", False),      # roam-binding enabled (SSH-verified)
        "boundClients": dev.get("boundClients", []),  # client MACs pinned to this AP
        "alerts": dev.get("alerts", []),
        "created": dev.get("created"),
        "state": dev.get("state"),  # latest poll: {online, values, errors, ts}
    }


def create_device(owner_id, host, transport, port, credentials, driver_id,
                  name=None, entities=None, dashboard_id=None, ap_binding=False):
    if not host or not transport:
        raise ValueError("host and transport are required")
    drv = registry.get(driver_id)
    if not drv:
        raise ValueError(f"unknown driver: {driver_id}")

    # Roam-binding writes go over SSH; only store it enabled once we've confirmed
    # SSH actually works, so the UI never offers a lock that can't be enforced.
    binding_enabled, binding_warning = False, None
    if ap_binding:
        binding_enabled, binding_warning = _verify_binding(
            drv, transport, host, port, credentials)

    dev_id = secrets.token_hex(8)
    cred_ref = secrets.token_hex(8)
    enc = crypto.encrypt(credentials or {})

    def _mut(doc):
        doc["credentials"][cred_ref] = enc
        # New devices sort to the end of the owner's list.
        order = sum(1 for d in doc["devices"].values()
                    if d.get("ownerId") == owner_id)
        rec = {
            "id": dev_id,
            "ownerId": owner_id,
            "name": name,
            "host": host,
            "port": port,
            "transport": transport,
            "driverId": driver_id,
            "credRef": cred_ref,
            "entities": entities or [],
            "dashboardId": dashboard_id or None,
            "apBinding": binding_enabled,
            "order": order,
            "created": int(time.time()),
        }
        doc["devices"][dev_id] = rec
        return rec

    pub = _public(store.update(_mut))
    if binding_warning:
        pub["bindingWarning"] = binding_warning  # transient; not persisted
    return pub


def _verify_binding(drv, transport, host, port, credentials):
    """Check a driver can enforce roam-binding (SSH usable). Returns
    (enabled, warning): (True, None) on success, (False, msg) if it can't."""
    if not getattr(drv, "supports_binding", False):
        return False, "This device type doesn't support roam-binding."
    try:
        conn = transports.open_connection(transport, host, port,
                                          credentials or {}, timeout=12)
        try:
            drv.binding_ready(conn)
            return True, None
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)


def _clean_alerts(alerts):
    """Normalize alert rules to [{key, op, value, label}]. Ignores malformed
    entries. op is 'above' or 'below'; value must be numeric."""
    out = []
    for a in alerts or []:
        if not isinstance(a, dict) or not a.get("key"):
            continue
        op = a.get("op")
        if op not in ("above", "below"):
            continue
        try:
            value = float(a.get("value"))
        except (TypeError, ValueError):
            continue
        out.append({"key": str(a["key"]), "op": op, "value": value,
                    "label": (a.get("label") or str(a["key"]))})
    return out


def update_device(dev_id, name=_UNSET, dashboard_id=_UNSET, entities=_UNSET,
                  hidden_interfaces=_UNSET, driver_id=_UNSET, alerts=_UNSET):
    """Patch mutable device fields (name / dashboard membership / enabled
    entities / driver). Only fields explicitly passed are touched. Returns the
    public record, or None if the device is gone.

    Changing driver_id re-detects nothing — it just re-points the device at a
    different curated driver (e.g. a switch mis-added as generic.http → the
    keeplink driver). The driver must exist and speak the device's transport;
    the opted-in entity set is cleared so the new driver's sensors default on.
    """
    def _mut(doc):
        dev = doc["devices"].get(dev_id)
        if not dev:
            return None
        if driver_id is not _UNSET:
            drv = registry.get(driver_id)
            if not drv:
                raise ValueError(f"unknown driver: {driver_id}")
            if dev["transport"] not in drv.transports:
                raise ValueError(
                    f"driver {driver_id} does not speak {dev['transport']}")
            if driver_id != dev.get("driverId"):
                dev["driverId"] = driver_id
                dev["entities"] = []   # new driver → re-default its sensors
        if name is not _UNSET:
            dev["name"] = (name or "").strip() or None
        if dashboard_id is not _UNSET:
            dev["dashboardId"] = dashboard_id or None
        if entities is not _UNSET:
            # Normalize to a list of {key} dicts, keeping only the key field.
            dev["entities"] = [{"key": e["key"]} for e in (entities or [])
                               if isinstance(e, dict) and e.get("key")]
        if hidden_interfaces is not _UNSET:
            dev["hiddenInterfaces"] = [str(x) for x in (hidden_interfaces or [])]
        if alerts is not _UNSET:
            dev["alerts"] = _clean_alerts(alerts)
        return dict(dev)

    dev = store.update(_mut)
    return _public(dev) if dev else None


def list_devices(owner_id, is_admin=False):
    devs = store.load()["devices"].values()
    out = [_public(d) for d in devs if is_admin or d.get("ownerId") == owner_id]
    out.sort(key=lambda d: (d.get("order", 0), d.get("created") or 0))
    return out


def reorder(owner_id, ids, is_admin=False):
    """Assign order = position for the given device ids (those the user owns).

    Called with the new left-to-right sequence of a dashboard's cards; other
    devices keep their order. Returns the number reordered."""
    n = 0

    def _mut(doc):
        nonlocal n
        for i, dev_id in enumerate(ids or []):
            dev = doc["devices"].get(dev_id)
            if dev and (is_admin or dev.get("ownerId") == owner_id):
                dev["order"] = i
                n += 1

    store.update(_mut)
    return n


def get_device(dev_id):
    return store.load()["devices"].get(dev_id)


def delete_device(dev_id):
    def _mut(doc):
        dev = doc["devices"].pop(dev_id, None)
        if dev and dev.get("credRef"):
            doc["credentials"].pop(dev["credRef"], None)
    store.update(_mut)


def _credentials_for(dev):
    ref = dev.get("credRef")
    if not ref:
        return {}
    blob = store.load()["credentials"].get(ref)
    return crypto.decrypt(blob) if blob else {}


def _drv_for(dev):
    drv = registry.get(dev["driverId"])
    if not drv:
        raise ValueError(f"driver gone: {dev['driverId']}")
    return drv


def open_conn(dev, timeout=30):
    """Open a connection to a stored device record (raw dict from get_device)."""
    return transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), _credentials_for(dev),
                                      timeout)


_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def set_client_binding(dev_id, mac, bound):
    """Lock (bound=True) or unlock a client MAC to an AP device. A MAC has at
    most one preferred AP, so binding it here first clears it from every other
    device. Returns the public record of `dev_id`, or None if it's gone.

    The binding is enforced by the poller (see poller.enforce_bindings), which
    kicks a bound client off any AP that isn't its preferred one."""
    mac = (mac or "").strip().upper()
    if not _MAC_RE.match(mac):
        raise ValueError("invalid MAC address")

    def _mut(doc):
        if dev_id not in doc["devices"]:
            return None
        for d in doc["devices"].values():
            kept = [m for m in (d.get("boundClients") or []) if m.upper() != mac]
            if d["id"] == dev_id and bound:
                kept.append(mac)
            if kept:
                d["boundClients"] = kept
            else:
                d.pop("boundClients", None)
        return dict(doc["devices"][dev_id])

    dev = store.update(_mut)
    return _public(dev) if dev else None


def _read_entities(drv, conn, wanted):
    """Read the opted-in sensor entities off an open connection.

    `wanted` is a set of entity keys, or None for all. Returns (values, errors).
    """
    values, errors = {}, {}
    for ent in drv.entities(conn):
        if wanted is not None and ent.key not in wanted:
            continue
        if ent.kind != "sensor" or not ent.read:
            continue
        try:
            values[ent.key] = ent.read()
        except Exception as e:
            errors[ent.key] = str(e)
    return values, errors


def read_state(dev_id, timeout=8):
    """Connect to a stored device and read its selected sensor entities.

    Returns {values: {key: value}, errors: {key: msg}}. Only entities the user
    opted into (dev['entities']) are read; controls are skipped.
    """
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    wanted = {e["key"] for e in dev.get("entities", [])} or None
    creds = _credentials_for(dev)
    conn = transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), creds, timeout)
    try:
        values, errors = _read_entities(drv, conn, wanted)
    finally:
        conn.close()
    return {"values": values, "errors": errors}


def poll_read(dev_id, timeout=8):
    """One-connection read for the poller: opted-in sensor values plus (for
    network gear) per-interface counters. Returns {values, errors, interfaces}.
    """
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    wanted = {e["key"] for e in dev.get("entities", [])} or None
    creds = _credentials_for(dev)
    conn = transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), creds, timeout)
    try:
        values, errors = _read_entities(drv, conn, wanted)
        try:
            ifaces = drv.interfaces(conn) or []
        except Exception:
            ifaces = []
    finally:
        conn.close()
    return {"values": values, "errors": errors, "interfaces": ifaces}


def _device_clients(dev, timeout=8):
    """Open one connection and read a device's client list (or [] on failure).
    Returns (device_public, clients, error)."""
    drv = registry.get(dev["driverId"])
    if not drv:
        return dev, [], "driver gone"
    try:
        creds = _credentials_for(dev)
        conn = transports.open_connection(dev["transport"], dev["host"],
                                          dev.get("port"), creds, timeout)
        try:
            return dev, (drv.clients(conn) or []), None
        finally:
            conn.close()
    except Exception as e:
        return dev, [], str(e)


def list_clients(owner_id, is_admin=False, timeout=8):
    """Aggregate the clients seen across every network device the user owns into
    one de-duplicated list, keyed by MAC. A device that appears on both a switch
    port and an AP is merged into a single entry that lists each place it was
    seen. Devices are polled concurrently so the view stays responsive.

    Returns {clients: [...], sources: [{device, count, error?}]}.
    """
    from concurrent.futures import ThreadPoolExecutor
    from drivers.base import Driver

    def _is_client_source(dev):
        drv = registry.get(dev.get("driverId"))
        # Only devices whose driver actually implements clients() (overrides the
        # empty base) contribute — APs and switches, not firewalls/NAS.
        return drv is not None and type(drv).clients is not Driver.clients

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
    return {"clients": clients, "sources": sources}


def run_action(dev_id, name, args, timeout=30):
    """Execute a named driver action on a stored device (e.g. force-roam a
    client off an AP). Opens a connection, dispatches to the driver, returns the
    driver's result dict. Raises ValueError for unknown device/driver/action."""
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    creds = _credentials_for(dev)
    conn = transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), creds, timeout)
    try:
        return drv.run_action(conn, name, args or {})
    finally:
        conn.close()


def binding_map(doc=None):
    """Global map {client MAC (upper) -> preferred AP device id} across every
    device's boundClients."""
    doc = doc or store.load()
    pref = {}
    for d in doc["devices"].values():
        for mac in d.get("boundClients") or []:
            pref[(mac or "").upper()] = d["id"]
    return pref


def _annotate_client_bindings(detail, dev, drv):
    """Tag each row of a layout:"clients" table with its AP-lock state so the UI
    can render the bind circle: "here" (locked to this AP), "elsewhere" (locked
    to another AP) or "" (unlocked). Marks the table `bindable` only when the
    driver can enforce a binding AND the user enabled (and we SSH-verified)
    roam-binding for this AP."""
    bindable = (bool(getattr(drv, "supports_binding", False))
                and bool(dev.get("apBinding")))
    pref = binding_map() if bindable else {}
    for t in (detail.get("tables") or []):
        if t.get("layout") != "clients":
            continue
        t["bindable"] = bindable
        for row in t.get("rows") or []:
            pid = pref.get((row.get("mac") or "").upper())
            row["lock"] = ("here" if pid == dev["id"]
                           else "elsewhere" if pid else "")


def read_detail(dev_id, timeout=8):
    """Rich per-device read for the detail view.

    Opens one connection and gathers: the driver's structured detail()
    (supplementary info + tables), the full entity catalogue (every entity the
    driver exposes, each tagged with whether the user has it enabled and — for
    enabled sensors — its freshly read value), and the stored numeric history
    for charting. Each piece fails soft.

    The entity catalogue is what powers the customizable detail view: the UI
    renders enabled entities as metrics and offers the rest to add. `enabled`
    follows the device's opted-in set (`dev['entities']`); an empty set means
    "all sensors" (the wizard default), matching read_state()/the poller.

    Returns {device, detail, entities, history}. Raises
    transports.ConnectionError only if the device can't be reached at all.
    """
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    opted = {e["key"] for e in dev.get("entities", [])}
    creds = _credentials_for(dev)
    conn = transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), creds, timeout)
    try:
        try:
            detail = drv.detail(conn) or {}
        except transports.ConnectionError:
            raise
        except Exception as e:
            detail = {"error": str(e)}
        _annotate_client_bindings(detail, dev, drv)
        entities = []
        for ent in drv.entities(conn):
            # Empty opt-in set => every sensor is on (wizard default).
            enabled = ent.key in opted if opted else ent.kind == "sensor"
            rec = ent.describe()
            rec["enabled"] = bool(enabled)
            if enabled and ent.kind == "sensor" and ent.read:
                try:
                    rec["value"] = ent.read()
                except Exception as e:
                    rec["error"] = str(e)
            entities.append(rec)
        try:
            device_actions = drv.actions() or []
        except Exception:
            device_actions = []
    finally:
        conn.close()
    return {
        "device": _public(dev),
        "detail": detail,
        "entities": entities,
        "actions": device_actions,
        "supportsBinding": bool(getattr(drv, "supports_binding", False)),
        "history": dev.get("history", {}),
        "ifHistory": dev.get("ifHistory", {}),
    }


def set_ap_binding(dev_id, enabled):
    """Enable/disable roam-binding on an already-added AP. Enabling re-verifies
    SSH first (same check the wizard does) so we never turn on a binding that
    can't be enforced; disabling also clears any client locks on this AP so no
    stale bindings linger. Returns (public_record, warning). warning is set only
    when enabling failed verification (record left disabled)."""
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    warning = None
    if enabled:
        ok, warning = _verify_binding(drv, dev["transport"], dev["host"],
                                      dev.get("port"), _credentials_for(dev))
        enabled = ok

    def _mut(doc):
        d = doc["devices"].get(dev_id)
        if not d:
            return None
        d["apBinding"] = bool(enabled)
        if not enabled:
            d.pop("boundClients", None)  # no enforcement -> drop stale locks
        return dict(d)

    rec = store.update(_mut)
    return (_public(rec) if rec else None), warning
