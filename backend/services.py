"""Actor-scoped application services used by the HTTP layer.

The lower-level modules retain device protocol and persistence mechanics;
this module is the public boundary for request-driven operations.  Every
operation takes an ``Actor`` before it can see or mutate an owned resource.
"""
import auth
import authorization as authorize
import client_roster
import client_service
import dashboards
import devices
import firewall
import history
import nac_service
from context import Actor
from errors import NotFound, ValidationError


def require_admin(actor: Actor):
    return authorize.admin(actor)


def authorized_device(actor: Actor, device_id):
    return authorize.device(actor, device_id)


def list_devices(actor: Actor):
    return devices.list_devices(actor.user_id, is_admin=actor.is_admin)


def create_device(actor: Actor, **kwargs):
    _assignment_dashboard(actor, kwargs.get("dashboard_id"))
    return devices.create_device(owner_id=actor.user_id, **kwargs)


def _assignment_dashboard(actor: Actor, dashboard_id):
    try:
        return authorize.dashboard(actor, dashboard_id, allow_unassigned=True)
    except NotFound as error:
        raise ValidationError("unknown dashboard") from error


def reorder_devices(actor: Actor, ids):
    if not isinstance(ids, list):
        raise ValidationError("ids must be a list")
    return devices.reorder(actor.user_id, ids, is_admin=actor.is_admin)


def device_history(actor: Actor, device_id, key, range_name):
    authorize.device(actor, device_id)
    return history.series(device_id, key, range_name) if key else {}


def device_state(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return devices.read_state(device_id)


def device_series(actor: Actor, device_id, metric, identifier):
    authorize.device(actor, device_id)
    return devices.read_series(device_id, metric, identifier)


def device_detail(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return devices.read_detail(device_id)


def device_action(actor: Actor, device_id, action, args):
    authorize.device(actor, device_id)
    return devices.run_action(device_id, action, args)


def update_device(actor: Actor, device_id, **kwargs):
    authorize.device(actor, device_id)
    if "dashboard_id" in kwargs:
        _assignment_dashboard(actor, kwargs["dashboard_id"])
    return devices.update_device(device_id, **kwargs)


def delete_device(actor: Actor, device_id):
    authorize.device(actor, device_id)
    devices.delete_device(device_id)


def set_ap_binding(actor: Actor, device_id, enabled):
    authorize.device(actor, device_id)
    return devices.set_ap_binding(device_id, enabled)


def set_client_binding(actor: Actor, device_id, mac, bound):
    authorize.device(actor, device_id)
    return devices.set_client_binding(device_id, mac, bound)


def firewall_all(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return firewall.firewall_all(device_id)


def firewall_toggle(actor: Actor, device_id, uuid, enabled):
    authorize.device(actor, device_id)
    return firewall.firewall_toggle(device_id, uuid, enabled)


def firewall_set_managed(actor: Actor, device_id, rules):
    authorize.device(actor, device_id)
    return firewall.firewall_set_managed(device_id, rules)


def list_dashboards(actor: Actor):
    return dashboards.list_dashboards(actor.user_id, is_admin=actor.is_admin)


def create_dashboard(actor: Actor, name):
    return dashboards.create(actor.user_id, name)


def update_dashboard(actor: Actor, dashboard_id, **kwargs):
    authorize.dashboard(actor, dashboard_id)
    return dashboards.update(dashboard_id, **kwargs)


def delete_dashboard(actor: Actor, dashboard_id):
    authorize.dashboard(actor, dashboard_id)
    dashboards.delete(dashboard_id)


def list_clients(actor: Actor):
    return client_service.list_clients(actor)


def refresh_clients(actor: Actor):
    return client_service.refresh(actor)


def export_clients(actor: Actor, fmt):
    return client_service.export_clients(actor, fmt)


def client_history(actor: Actor, mac):
    return client_roster.client_history(actor.user_id, mac)


def client_events(actor: Actor, since):
    return client_roster.events_since(actor.user_id, since)


def forget_client(actor: Actor, mac):
    return {"mac": (mac or "").strip().upper(), "forgotten": bool(client_roster.forget(actor.user_id, [mac]))}


def forget_clients(actor: Actor, macs):
    return {"forgotten": client_roster.forget(actor.user_id, macs)}


def nac_ignore(actor: Actor, mac):
    return client_roster.ignore(actor.user_id, mac)


def nac_interfaces(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return nac_service.nac_interfaces(device_id)


def nac_aliases(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return nac_service.nac_aliases(device_id)


def nac_setup_existing(actor: Actor, device_id, alias_uuid):
    authorize.device(actor, device_id)
    return nac_service.nac_setup_existing(device_id, alias_uuid)


def nac_setup(actor: Actor, device_id, alias, interface, seed_macs=None):
    authorize.device(actor, device_id)
    return nac_service.nac_setup(device_id, alias, interface, seed_macs)


def nac_approve(actor: Actor, device_id, mac, approved):
    authorize.device(actor, device_id)
    return nac_service.nac_approve(device_id, mac, approved)


def nac_approve_many(actor: Actor, device_id, macs, approved):
    authorize.device(actor, device_id)
    return nac_service.nac_approve_many(device_id, macs, approved)


def nac_set_enforcement(actor: Actor, device_id, enabled):
    authorize.device(actor, device_id)
    return nac_service.nac_set_enforcement(device_id, enabled)


def get_nac_config(actor: Actor):
    return nac_service.get_config(actor.user_id, is_admin=actor.is_admin)


def set_nac_config(actor: Actor, managed_aliases, dns_sync):
    authorize.nac(actor)
    return nac_service.set_config(actor.user_id, actor.is_admin, managed_aliases, dns_sync)


def create_managed_alias(actor: Actor, name, alias_type):
    authorize.nac(actor)
    return nac_service.create_managed_alias(actor.user_id, actor.is_admin, name, alias_type)


def client_membership(actor: Actor, mac, ip):
    return nac_service.client_membership(actor.user_id, actor.is_admin, mac, ip)


def edit_client(actor: Actor, mac, **kwargs):
    # Roster metadata is always local; firewall alias and DNS operations are
    # delegated to the NAC boundary only when requested.
    name, notes, notify = kwargs.get("name", ""), kwargs.get("notes", ""), kwargs.get("notify")
    meta = client_roster.set_metadata(actor.user_id, mac, name, notes, notify=notify)
    firewall_changes = {key: value for key, value in kwargs.items()
                        if key in {"ip", "hostname", "sync_dns", "alias_changes"}}
    if not firewall_changes.get("alias_changes") and firewall_changes.get("sync_dns") is None:
        return {**meta, "aliasChanges": {}, "dns": None}
    result = nac_service.edit_membership(actor.user_id, actor.is_admin, mac,
                                         name=name, notes=notes, notify=notify,
                                         **firewall_changes)
    result.update(meta)
    return result


def create_user(actor: Actor, username, password, role):
    authorize.admin(actor)
    return auth.create_user(username, password, role)


def list_users(actor: Actor):
    authorize.admin(actor)
    return auth.list_users()


def delete_user(actor: Actor, user_id):
    authorize.admin(actor)
    if user_id == actor.user_id:
        raise ValidationError("cannot delete yourself")
    auth.delete_user(user_id)
