"""Actor-scoped application services used by the HTTP layer.

The lower-level modules retain device protocol and persistence mechanics;
this module is the public boundary for request-driven operations.  Every
operation takes an ``Actor`` before it can see or mutate an owned resource.
"""
import auth
import ansible_integration
import authorization as authorize
import client_roster
import client_service
import compute
import compute_maintenance
import dashboards
import devices
import device_updates
import firewall
import history
import nac_service
import vpn_endpoint_service
from context import Actor
from errors import Conflict, NotFound, ValidationError


def require_admin(actor: Actor):
    return authorize.admin(actor)


def authorized_device(actor: Actor, device_id):
    return authorize.device(actor, device_id)


def list_devices(actor: Actor):
    records = devices.list_devices(actor.user_id, is_admin=actor.is_admin)
    counts = {}
    for item in compute.list_instances(actor.user_id, is_admin=actor.is_admin):
        parent_id = item.get("parentDeviceId")
        counts[parent_id] = counts.get(parent_id, 0) + 1
    for record in records:
        record["computeWorkloadCount"] = counts.get(record["id"], 0)
    return records


def list_compute(actor: Actor):
    controller = ansible_integration.get_controller()
    return {
        "instances": compute.list_instances(actor.user_id, is_admin=actor.is_admin),
        "summary": compute.summary(actor.user_id, is_admin=actor.is_admin),
        "ansibleEnabled": bool(controller and controller.get("enabled")),
    }


def compute_detail(actor: Actor, instance_id):
    resource = authorize.compute(actor, instance_id)
    result = compute.public_instance(resource)
    result["suggestedMappings"] = (ansible_integration.mapping_suggestions(resource)
                                   if actor.is_admin else [])
    return result


def refresh_compute(actor: Actor):
    authorize.admin(actor)
    result = compute.discover_all(actor.user_id, is_admin=True)
    controller = ansible_integration.get_controller()
    if not controller or not controller.get("enabled"):
        result["ansibleInventory"] = {"ok": False, "skipped": "controller disabled"}
        result["dockerJobs"] = []
        return result
    try:
        inventory = ansible_integration.refresh_inventory(controller["id"])
        result["ansibleInventory"] = {
            "ok": True, "hosts": len(inventory.get("hosts") or []),
            "groups": len(inventory.get("groups") or []),
        }
    except Exception as error:
        result["ansibleInventory"] = {
            "ok": False, "error": ansible_integration.sanitized_error(error, controller)}
    result["dockerJobs"] = []
    if ansible_integration.operation_is_approved(controller, "docker_discovery"):
        for instance in compute.list_instances(actor.user_id, is_admin=True):
            if not (instance.get("ansible") or {}).get("dockerDiscoveryEligible"):
                continue
            try:
                job = compute_maintenance.start_job(
                    instance["id"], "docker_discovery", actor.user_id)
                result["dockerJobs"].append({"computeInstanceId": instance["id"],
                                             "jobId": job["id"], "queued": True})
            except Conflict:
                result["dockerJobs"].append({"computeInstanceId": instance["id"],
                                             "queued": False, "reason": "job active"})
            except Exception as error:
                result["dockerJobs"].append({
                    "computeInstanceId": instance["id"], "queued": False,
                    "error": ansible_integration.sanitized_error(error, controller)})
    return result


def get_ansible_controller(actor: Actor):
    authorize.admin(actor)
    return ansible_integration.get_controller(public=True)


def save_ansible_controller(actor: Actor, config):
    authorize.admin(actor)
    return ansible_integration.save_controller(config)


def test_ansible_controller(actor: Actor):
    authorize.admin(actor)
    return ansible_integration.test_connection()


def refresh_ansible_inventory(actor: Actor):
    authorize.admin(actor)
    return ansible_integration.refresh_inventory()


def discover_ansible_playbooks(actor: Actor):
    authorize.admin(actor)
    return ansible_integration.discover_playbooks()


def approve_ansible_playbook(actor: Actor, config):
    authorize.admin(actor)
    return ansible_integration.approve_playbook(ansible_integration.CONTROLLER_ID, config)


def set_compute_mapping(actor: Actor, instance_id, enabled, controller_id, inventory_host,
                        maintenance=None):
    authorize.admin(actor)
    authorize.compute(actor, instance_id)
    record = ansible_integration.set_mapping(
        instance_id, enabled, controller_id, inventory_host, maintenance)
    return compute.public_instance(record)


def compute_jobs(actor: Actor, instance_id):
    authorize.compute(actor, instance_id)
    return compute_maintenance.list_jobs(instance_id)


def compute_job(actor: Actor, job_id):
    job = compute_maintenance.get_job(job_id)
    if not job:
        raise NotFound()
    authorize.compute(actor, job.get("computeInstanceId"))
    return job


def compute_check_updates(actor: Actor, instance_id):
    authorize.compute(actor, instance_id)
    return compute_maintenance.start_job(instance_id, "os_check", actor.user_id)


def compute_update(actor: Actor, instance_id, *, allow_reboot=False,
                   reboot_confirmed=False):
    authorize.admin(actor)
    authorize.compute(actor, instance_id)
    if allow_reboot and not reboot_confirmed:
        raise ValidationError("reboot permission requires explicit confirmation")
    return compute_maintenance.start_job(
        instance_id, "os_update", actor.user_id, allow_reboot=allow_reboot)


def compute_docker_discover(actor: Actor, instance_id):
    authorize.compute(actor, instance_id)
    return compute_maintenance.start_job(instance_id, "docker_discovery", actor.user_id)


def compute_docker_check(actor: Actor, instance_id):
    authorize.compute(actor, instance_id)
    return compute_maintenance.start_job(instance_id, "docker_check", actor.user_id)


def compute_docker_strategy(actor: Actor, instance_id, project_id, strategy):
    authorize.admin(actor)
    authorize.compute(actor, instance_id)
    return compute_maintenance.set_project_strategy(instance_id, project_id, strategy)


def compute_docker_update(actor: Actor, instance_id, project_id):
    authorize.admin(actor)
    authorize.compute(actor, instance_id)
    return compute_maintenance.start_job(
        instance_id, "docker_project_update", actor.user_id, project_id=project_id)


def create_device(actor: Actor, **kwargs):
    _assignment_dashboard(actor, kwargs.get("dashboard_id"), actor.user_id)
    return devices.create_device(owner_id=actor.user_id, **kwargs)


def _assignment_dashboard(actor: Actor, dashboard_id, device_owner_id):
    try:
        assigned = authorize.dashboard(actor, dashboard_id, allow_unassigned=True)
    except NotFound as error:
        raise ValidationError("unknown dashboard") from error
    if assigned and assigned.get("ownerId") != device_owner_id:
        raise ValidationError("dashboard must have the same owner as the device")
    return assigned


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


def device_updates_check(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return device_updates.check(device_id)


def device_updates_status(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return {"operation": device_updates.status(device_id)}


def device_updates_install(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return {"operation": device_updates.start(device_id)}


def device_updates_configure_ssh(actor: Actor, device_id, **credentials):
    authorize.device(actor, device_id)
    return device_updates.configure_ssh(device_id, **credentials)


def update_device(actor: Actor, device_id, **kwargs):
    device = authorize.device(actor, device_id)
    if "dashboard_id" in kwargs:
        _assignment_dashboard(actor, kwargs["dashboard_id"], device.get("ownerId"))
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


def vpn_endpoint_choices(actor: Actor, device_id):
    authorize.device(actor, device_id)
    return vpn_endpoint_service.choices(device_id)


def vpn_endpoint_status(actor: Actor, device_id, refresh=False):
    device = authorize.device(actor, device_id)
    return vpn_endpoint_service.statuses(device["ownerId"], device_id, refresh=refresh)


def vpn_endpoint_profile_status(actor: Actor, device_id, profile_id, refresh=False):
    device = authorize.device(actor, device_id)
    return vpn_endpoint_service.status(
        device["ownerId"], device_id, profile_id, refresh=refresh)


def vpn_endpoint_configure(actor: Actor, device_id, profile, profile_id=None):
    device = authorize.device(actor, device_id)
    return vpn_endpoint_service.configure(
        device["ownerId"], device_id, profile, profile_id=profile_id)


def vpn_endpoint_create(actor: Actor, device_id, profile):
    device = authorize.device(actor, device_id)
    return vpn_endpoint_service.configure(device["ownerId"], device_id, profile, create=True)


def vpn_endpoint_remove(actor: Actor, device_id, profile_id, confirmed):
    device = authorize.device(actor, device_id)
    vpn_endpoint_service.remove_profile(device["ownerId"], device_id, profile_id, confirmed)


def vpn_endpoint_compatibility(actor: Actor, device_id, candidate_id, target_id, state, note,
                               profile_id=None):
    device = authorize.device(actor, device_id)
    vpn_endpoint_service.set_validation(
        device["ownerId"], device_id, candidate_id, target_id, state, note,
        profile_id=profile_id)


def vpn_endpoint_switch(actor: Actor, device_id, candidate_id, confirmed, profile_id=None):
    device = authorize.device(actor, device_id)
    return vpn_endpoint_service.switch(
        device["ownerId"], device_id, candidate_id, confirmed, profile_id=profile_id)


def get_nac_config(actor: Actor):
    return nac_service.get_config(actor.user_id)


def set_nac_config(actor: Actor, managed_aliases, dns_sync):
    authorize.nac(actor)
    return nac_service.set_config(actor.user_id, managed_aliases, dns_sync)


def create_managed_alias(actor: Actor, name, alias_type):
    authorize.nac(actor)
    return nac_service.create_managed_alias(actor.user_id, name, alias_type)


def client_membership(actor: Actor, mac, ip):
    return nac_service.client_membership(actor.user_id, mac, ip)


def edit_client(actor: Actor, mac, **kwargs):
    # Roster metadata is always local; firewall alias and DNS operations are
    # delegated to the NAC boundary only when requested.
    name, notes, notify = kwargs.get("name", ""), kwargs.get("notes", ""), kwargs.get("notify")
    meta = client_roster.set_metadata(actor.user_id, mac, name, notes, notify=notify)
    firewall_changes = {key: value for key, value in kwargs.items()
                        if key in {"ip", "hostname", "sync_dns", "alias_changes"}}
    if not firewall_changes.get("alias_changes") and firewall_changes.get("sync_dns") is None:
        return {**meta, "aliasChanges": {}, "dns": None}
    result = nac_service.edit_membership(actor.user_id, mac,
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
