"""Firewall-rule management, delegated to the driver.

Split out of devices.py: the managed-rule list a device opts into (add /
rename / remove / reorder) and their live enabled state, for drivers whose
`firewall_rule_states` capability is set (currently OPNsense/pfSense).
"""
import re

import store
from devices import device_conn

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def firewall_states(dev_id):
    """Live enabled-state of a device's managed firewall rules."""
    with device_conn(dev_id, require="firewall") as (dev, drv, conn):
        return drv.firewall_rule_states(conn, dev.get("firewallRules") or [])


def firewall_all(dev_id):
    """Every firewall rule on the device, for the add-rule picker."""
    with device_conn(dev_id, require="firewall") as (dev, drv, conn):
        return drv.firewall_all_rules(conn)


def firewall_toggle(dev_id, uuid, enabled):
    """Enable/disable one managed rule on the firewall (never deletes)."""
    with device_conn(dev_id, require="firewall") as (dev, drv, conn):
        return drv.firewall_toggle(conn, uuid, bool(enabled))


def firewall_set_managed(dev_id, rules):
    """Replace a device's managed firewall-rule list (add / rename / remove /
    reorder). `rules` is [{uuid, name, renamed?}]; entries are sanitized and
    de-duplicated by uuid. `renamed` marks a label the user set here (so the UI
    prefers it over the live rule name). Returns the fresh live states."""
    clean, seen = [], set()
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        uuid = str(r.get("uuid") or "").strip()
        if not uuid or not _UUID_RE.match(uuid) or uuid in seen:
            continue
        seen.add(uuid)
        name = str(r.get("name") or "").strip() or uuid
        entry = {"uuid": uuid, "name": name[:120]}
        if r.get("renamed"):
            entry["renamed"] = True
        clean.append(entry)

    def _mut(doc):
        dev = doc["devices"].get(dev_id)
        if not dev:
            return None
        dev["firewallRules"] = clean
        return dict(dev)

    dev = store.update(_mut)
    if not dev:
        raise ValueError("device not found")
    return firewall_states(dev_id)
