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
from contextlib import contextmanager

import crypto
import store
import history
import transports
from drivers import registry
from domain import AlertRule, DevicePollResult, DriverDetail, safe_error

_UNSET = object()  # sentinel: "field not provided" vs "set to null/empty"


def _nac_summary(dev: dict) -> dict:
    """Public view of a device's Network Access Control setup. `configured` is
    true once linked to an allow-list alias; `mode` is 'existing' (membership
    only — the user's own rule enforces it) or 'managed' (we created the alias +
    rules). `enforced` is the deny-all rule's state, meaningful only when
    managed; `managedExternally` marks the existing-alias case where enforcement
    is out of our hands."""
    nac = dev.get("nac") or {}
    mode = nac.get("mode") or ("managed" if nac.get("blockUuid") else None)
    return {
        "configured": bool(nac.get("alias")),
        "alias": nac.get("alias"),
        "interface": nac.get("interface"),
        "mode": mode,
        "managedExternally": bool(nac.get("managedExternally")) or mode == "existing",
        "enforced": bool(nac.get("enabled")),
    }


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
        "nac": _nac_summary(dev),                     # access-control setup, if any
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

    # Device metadata and its encrypted credential must become durable in the
    # same document commit; a partial device is not usable.
    pub = _public(store.batch_update(_mut))
    if binding_warning:
        pub["bindingWarning"] = binding_warning  # transient; not persisted
    return pub


def _verify_binding(drv, transport, host, port, credentials):
    """Check a driver can enforce roam-binding (SSH usable). Returns
    (enabled, warning): (True, None) on success, (False, msg) if it can't."""
    if not getattr(drv, "supports_binding", False):
        return False, "This device type doesn't support roam-binding."
    try:
        with transports.open_connection(transport, host, port,
                                        credentials or {}, timeout=12) as conn:
            drv.binding_ready(conn)
        return True, None
    except Exception as e:
        return False, str(e)


def _clean_alerts(alerts):
    """Normalize alert rules to [{key, op, value, label}]. Ignores malformed
    entries. op is 'above' or 'below'; value must be numeric."""
    out = []
    for a in alerts or []:
        try:
            if not isinstance(a, dict):
                raise ValueError("alert must be an object")
            out.append(AlertRule.from_mapping(a).to_dict())
        except (TypeError, ValueError):
            continue
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

    store.batch_update(_mut)
    return n


def get_device(dev_id):
    return store.load()["devices"].get(dev_id)


def delete_device(dev_id):
    def _mut(doc):
        dev = doc["devices"].pop(dev_id, None)
        if dev and dev.get("credRef"):
            doc["credentials"].pop(dev["credRef"], None)
    store.batch_update(_mut)
    history.delete(dev_id)


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


# Capability checks for device_conn(require=...): a lambda that must be truthy
# on the driver, plus the ValueError message to raise when it isn't.
_CAP_REQUIREMENTS = {
    "nac": (lambda drv: getattr(drv, "nac_supported", False),
            "device does not support access control"),
    "firewall": (lambda drv: getattr(drv, "firewall_rule_states", None),
                 "device does not manage firewall rules"),
}


@contextmanager
def device_conn(dev_id, timeout=15, require=None):
    """Look up a stored device, open a connection to it, and yield
    (dev, drv, conn). The connection is closed automatically when the `with`
    block exits — including on exception — via Connection's
    __enter__/__exit__, so callers never hand-roll try/finally: conn.close().

    `require` is "nac" or "firewall" to also assert the driver supports that
    capability before connecting; raises ValueError (without touching the
    network) if it doesn't. Raises ValueError if the device itself is gone.
    """
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = _drv_for(dev)
    if require is not None:
        check, msg = _CAP_REQUIREMENTS[require]
        if not check(drv):
            raise ValueError(msg)
    with transports.open_connection(dev["transport"], dev["host"],
                                    dev.get("port"), _credentials_for(dev),
                                    timeout) as conn:
        yield dev, drv, conn


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
            errors[ent.key] = safe_error(e)
    return values, errors


def read_state(dev_id, timeout=8):
    """Connect to a stored device and read its selected sensor entities.

    Returns {values: {key: value}, errors: {key: msg}}. Only entities the user
    opted into (dev['entities']) are read; controls are skipped.
    """
    with device_conn(dev_id, timeout=timeout) as (dev, drv, conn):
        wanted = {e["key"] for e in dev.get("entities", [])} or None
        values, errors = _read_entities(drv, conn, wanted)
    return {"values": values, "errors": errors}


def read_series(dev_id, metric, ident, timeout=15):
    """Fetch a driver-provided time-series for a detail-table cell chart (e.g. a
    disk's temperature history). Returns [[epoch, value], ...], or [] when the
    driver doesn't support the requested metric.
    """
    with device_conn(dev_id, timeout=timeout) as (dev, drv, conn):
        return drv.series(conn, metric, ident) or []


def poll_read(dev_id, timeout=8) -> DevicePollResult:
    """One-connection read for the poller: opted-in sensor values plus (for
    network gear) per-interface counters. Returns {values, errors, interfaces}.
    """
    with device_conn(dev_id, timeout=timeout) as (dev, drv, conn):
        wanted = {e["key"] for e in dev.get("entities", [])} or None
        values, errors = _read_entities(drv, conn, wanted)
        try:
            ifaces = drv.interfaces(conn) or []
        except Exception:
            ifaces = []
    return DevicePollResult(values=values, errors=errors, interfaces=ifaces)


def run_action(dev_id, name, args, timeout=30):
    """Execute a named driver action on a stored device (e.g. force-roam a
    client off an AP). Opens a connection, dispatches to the driver, returns the
    driver's result dict. Raises ValueError for unknown device/driver/action."""
    with device_conn(dev_id, timeout=timeout) as (dev, drv, conn):
        return drv.run_action(conn, name, args or {})


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
    with device_conn(dev_id, timeout=timeout) as (dev, drv, conn):
        opted = {e["key"] for e in dev.get("entities", [])}
        try:
            detail = drv.detail(conn) or {}
        except transports.ConnectionError:
            raise
        except Exception as e:
            detail = {"error": safe_error(e)}
        # Driver-specific data remains flexible, but the shared tables/info
        # contract is checked before the application layer consumes it.
        detail = DriverDetail.from_mapping(detail).to_dict()
        _annotate_client_bindings(detail, dev, drv)
        # Drivers that manage firewall rules (OPNsense) ship the live enabled
        # state of the device's opted-in rule list alongside detail(), so the
        # section repaints on the modal's 20s refresh too.
        if getattr(drv, "firewall_rule_states", None):
            managed = dev.get("firewallRules") or []
            try:
                detail["firewall"] = {
                    "supported": True,
                    "rules": drv.firewall_rule_states(conn, managed),
                }
            except transports.ConnectionError:
                raise
            except Exception as e:
                detail["firewall"] = {"supported": True, "rules": [],
                                      "error": safe_error(e)}
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
                    rec["error"] = safe_error(e)
            entities.append(rec)
        try:
            device_actions = drv.actions() or []
        except Exception:
            device_actions = []
    h = history.load(dev["id"])
    return {
        "device": _public(dev),
        "detail": detail,
        "entities": entities,
        "actions": device_actions,
        "supportsBinding": bool(getattr(drv, "supports_binding", False)),
        "history": h["history"],
        "ifHistory": h["ifHistory"],
        "online": h.get("online", []),
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
