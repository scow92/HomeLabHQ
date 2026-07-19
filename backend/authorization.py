"""Central resource-visibility policy for application services."""
import client_roster
import nac_service
import devices
import dashboards
import store
from context import Actor
from errors import Forbidden, NotFound, ValidationError


def admin(actor: Actor) -> Actor:
    if not actor.is_admin:
        raise Forbidden("admin only")
    return actor


def device(actor: Actor, device_id: str) -> dict:
    resource = devices.get_device(device_id)
    # Deliberately do not reveal another tenant's resource existence.
    if not resource or (not actor.is_admin and resource.get("ownerId") != actor.user_id):
        raise NotFound()
    return resource


def dashboard(actor: Actor, dashboard_id: str | None, *, allow_unassigned=False) -> dict | None:
    if not dashboard_id and allow_unassigned:
        return None
    resource = dashboards.get(dashboard_id) if dashboard_id else None
    if not resource or (not actor.is_admin and resource.get("ownerId") != actor.user_id):
        raise NotFound()
    return resource


def client(actor: Actor, mac: str) -> dict:
    """Return an actor-owned roster entry without exposing another owner's data."""
    mac = (mac or "").strip().upper()
    if not devices._MAC_RE.match(mac):
        raise ValidationError("invalid MAC address")
    resource = client_roster.roster(store.load(), actor.user_id).get(mac)
    if not resource:
        raise NotFound()
    return resource


def nac(actor: Actor) -> dict:
    """Return the NAC device visible to this actor, if one is configured."""
    resource = nac_service.configured_device(actor.user_id, actor.is_admin)
    if not resource:
        raise NotFound("access control is not configured")
    return resource
