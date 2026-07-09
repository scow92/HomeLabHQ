"""Device persistence + live reads.

A device record stores everything needed to reconnect and read it later: host,
transport, port, the chosen driver, the entity keys the user opted into, and a
reference to its encrypted credential blob. Credentials never live in the
device record itself — they're Fernet-encrypted in the `credentials` map and
decrypted only at connect time.
"""
import secrets
import time

import crypto
import store
import transports
from drivers import registry


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
        "created": dev.get("created"),
        "state": dev.get("state"),  # latest poll: {online, values, errors, ts}
    }


def create_device(owner_id, host, transport, port, credentials, driver_id,
                  name=None, entities=None):
    if not host or not transport:
        raise ValueError("host and transport are required")
    if not registry.get(driver_id):
        raise ValueError(f"unknown driver: {driver_id}")

    dev_id = secrets.token_hex(8)
    cred_ref = secrets.token_hex(8)
    enc = crypto.encrypt(credentials or {})

    def _mut(doc):
        doc["credentials"][cred_ref] = enc
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
            "created": int(time.time()),
        }
        doc["devices"][dev_id] = rec
        return rec

    return _public(store.update(_mut))


def list_devices(owner_id, is_admin=False):
    devs = store.load()["devices"].values()
    return [_public(d) for d in devs if is_admin or d.get("ownerId") == owner_id]


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


def read_state(dev_id, timeout=8):
    """Connect to a stored device and read its selected sensor entities.

    Returns {values: {key: value}, errors: {key: msg}}. Only entities the user
    opted into (dev['entities']) are read; controls are skipped.
    """
    dev = get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    drv = registry.get(dev["driverId"])
    if not drv:
        raise ValueError(f"driver gone: {dev['driverId']}")

    wanted = {e["key"] for e in dev.get("entities", [])} or None
    creds = _credentials_for(dev)
    conn = transports.open_connection(dev["transport"], dev["host"],
                                      dev.get("port"), creds, timeout)
    values, errors = {}, {}
    try:
        for ent in drv.entities(conn):
            if wanted is not None and ent.key not in wanted:
                continue
            if ent.kind != "sensor" or not ent.read:
                continue
            try:
                values[ent.key] = ent.read()
            except Exception as e:
                errors[ent.key] = str(e)
    finally:
        conn.close()
    return {"values": values, "errors": errors}
