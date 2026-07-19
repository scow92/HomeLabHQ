"""Compatibility adapters for the Phase 4 client-service split.

New request code uses :mod:`client_service`; this module retains the historic
owner-id API for integrations while ensuring it follows the same refresh path.
"""
import os
import time

import client_discovery
import client_roster
import client_service
from context import POLLER_CONTEXT


ROSTER_SCAN_INTERVAL = max(60, int(os.environ.get("HLHQ_CLIENT_SCAN_INTERVAL", "300")))
_last_scan = 0.0


def _is_client_source(device):
    return client_discovery.is_client_source(device)


def list_clients(owner_id, is_admin=False, timeout=8):
    """Deprecated compatibility call: explicitly performs a live refresh."""
    return client_service.refresh(POLLER_CONTEXT, owner_id, timeout=timeout)


def export_clients(owner_id, is_admin=False, fmt="json"):
    from context import Actor, Role
    role = Role.ADMIN if is_admin else Role.MEMBER
    return client_service.export_clients(Actor(owner_id, role), fmt)


def track_roster():
    """Background discovery with an explicit trusted poller context."""
    global _last_scan
    if time.time() - _last_scan < ROSTER_SCAN_INTERVAL:
        return
    import store
    owners = {device.get("ownerId") for device in store.load()["devices"].values()
              if device.get("ownerId") and _is_client_source(device)}
    if not owners:
        return
    _last_scan = time.time()
    for owner_id in owners:
        client_service.refresh(POLLER_CONTEXT, owner_id, timeout=6)


# Roster operations moved here for callers that previously imported clients.
read_snapshot = client_roster.read_snapshot
