"""Dashboards: named, ordered groups of devices.

A dashboard is just a label a user creates ("Network", "Proxmox", ...) that
devices are assigned to via `device.dashboardId`. Membership is single-homed —
a device belongs to one dashboard at a time (moving it re-points that field),
and devices with no dashboard show under an "Unassigned" view. Dashboards are
per-owner; admins see all, matching the device ownership model.
"""
import secrets
import time

import store

_UNSET = object()  # distinguish "leave unchanged" from "set to null/empty"


def _public(d: dict) -> dict:
    return {
        "id": d["id"],
        "ownerId": d["ownerId"],
        "name": d["name"],
        "order": d.get("order", 0),
        "created": d.get("created"),
    }


def list_dashboards(owner_id, is_admin=False):
    ds = store.load()["dashboards"].values()
    out = [_public(d) for d in ds if is_admin or d.get("ownerId") == owner_id]
    out.sort(key=lambda d: (d["order"], d["created"] or 0))
    return out


def get(dash_id):
    return store.load()["dashboards"].get(dash_id)


def create(owner_id, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("dashboard name is required")
    dash_id = secrets.token_hex(6)

    def _mut(doc):
        order = sum(1 for d in doc["dashboards"].values()
                    if d.get("ownerId") == owner_id)
        rec = {"id": dash_id, "ownerId": owner_id, "name": name,
               "order": order, "created": int(time.time())}
        doc["dashboards"][dash_id] = rec
        return rec

    return _public(store.update(_mut))


def update(dash_id, name=_UNSET, order=_UNSET):
    def _mut(doc):
        d = doc["dashboards"].get(dash_id)
        if not d:
            return None
        if name is not _UNSET:
            nm = (name or "").strip()
            if nm:
                d["name"] = nm
        if order is not _UNSET and order is not None:
            d["order"] = int(order)
        return dict(d)

    d = store.update(_mut)
    return _public(d) if d else None


def delete(dash_id):
    """Delete a dashboard; any devices assigned to it become unassigned."""
    def _mut(doc):
        doc["dashboards"].pop(dash_id, None)
        for dev in doc["devices"].values():
            if dev.get("dashboardId") == dash_id:
                dev["dashboardId"] = None
    store.update(_mut)
