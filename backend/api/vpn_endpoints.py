"""Owner-authorized OPNsense NordVPN endpoint-manager routes."""
import services

from backend.http.responses import json_response
from backend.http.router import Route


def choices(request):
    return json_response(services.vpn_endpoint_choices(request.require_actor(), request.params["device_id"]))


def status(request):
    refresh = request.query_value("refresh") in ("1", "true")
    return json_response(services.vpn_endpoint_status(request.require_actor(), request.params["device_id"], refresh))


def configure(request):
    profile = services.vpn_endpoint_configure(request.require_actor(), request.params["device_id"], request.body)
    return json_response({"profile": profile})


def compatibility(request):
    body = request.body
    services.vpn_endpoint_compatibility(request.require_actor(), request.params["device_id"],
                                        body.get("candidateId"), body.get("state"), body.get("note", ""))
    return json_response({"ok": True})


def switch(request):
    body = request.body
    return json_response(services.vpn_endpoint_switch(request.require_actor(), request.params["device_id"],
                                                       body.get("candidateId"), bool(body.get("confirmed"))))


def routes():
    return (
        Route("GET", "/api/devices/{device_id}/vpn-endpoints/choices", choices, name="vpn-endpoint-choices"),
        Route("GET", "/api/devices/{device_id}/vpn-endpoints", status, name="vpn-endpoint-status"),
        Route("PUT", "/api/devices/{device_id}/vpn-endpoints", configure, name="vpn-endpoint-configure"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/compatibility", compatibility,
              name="vpn-endpoint-compatibility"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/switch", switch, name="vpn-endpoint-switch"),
    )
