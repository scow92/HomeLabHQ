"""Administrator-only Ansible controller configuration routes."""
import services

from backend.http.responses import json_response
from backend.http.router import AuthPolicy, Route


def get_settings(request):
    return json_response({"controller": services.get_ansible_controller(
        request.require_actor())})


def save_settings(request):
    return json_response({"controller": services.save_ansible_controller(
        request.require_actor(), request.body)})


def test(request):
    return json_response({"status": services.test_ansible_controller(
        request.require_actor())})


def inventory(request):
    return json_response({"inventory": services.refresh_ansible_inventory(
        request.require_actor())})


def playbooks(request):
    return json_response({"playbooks": services.discover_ansible_playbooks(
        request.require_actor())})


def approve(request):
    return json_response({"playbooks": services.approve_ansible_playbook(
        request.require_actor(), request.body)})


def mapping(request):
    body = request.body
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if not enabled and (body.get("controllerId") or body.get("inventoryHost") or
                        body.get("maintenance") is not None):
        raise ValueError("disabled mappings must not include an Ansible target")
    return json_response({"instance": services.set_compute_mapping(
        request.require_actor(), request.params["compute_id"], enabled,
        body.get("controllerId"), body.get("inventoryHost"), body.get("maintenance"))})


def routes():
    admin = AuthPolicy.ADMIN
    return (
        Route("GET", "/api/settings/ansible", get_settings, admin, "ansible-settings"),
        Route("POST", "/api/settings/ansible", save_settings, admin, "ansible-save"),
        Route("POST", "/api/settings/ansible/test", test, admin, "ansible-test"),
        Route("POST", "/api/settings/ansible/inventory", inventory, admin,
              "ansible-inventory"),
        Route("POST", "/api/settings/ansible/playbooks", playbooks, admin,
              "ansible-playbooks"),
        Route("POST", "/api/settings/ansible/playbooks/approve", approve, admin,
              "ansible-playbooks-approve"),
        Route("POST", "/api/compute/{compute_id}/ansible", mapping, admin,
              "compute-ansible-mapping"),
    )
