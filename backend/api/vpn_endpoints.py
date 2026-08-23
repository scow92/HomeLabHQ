"""Owner-authorized OPNsense NordVPN endpoint-manager routes."""
import services

from backend.api.contracts import Route, json_response


def choices(request):
    return json_response(services.vpn_endpoint_choices(request.require_actor(), request.params["device_id"]))


def status(request):
    refresh = request.query_value("refresh") in ("1", "true")
    return json_response(services.vpn_endpoint_status(request.require_actor(), request.params["device_id"], refresh))


def configure(request):
    profile = services.vpn_endpoint_configure(request.require_actor(), request.params["device_id"], request.body)
    return json_response({"profile": profile})


def create(request):
    profile = services.vpn_endpoint_create(
        request.require_actor(), request.params["device_id"], request.body)
    return json_response({"profile": profile}, 201)


def profile_status(request):
    refresh = request.query_value("refresh") in ("1", "true")
    return json_response(services.vpn_endpoint_profile_status(
        request.require_actor(), request.params["device_id"], request.params["profile_id"], refresh))


def configure_profile(request):
    profile = services.vpn_endpoint_configure(
        request.require_actor(), request.params["device_id"], request.body,
        request.params["profile_id"])
    return json_response({"profile": profile})


def remove_profile(request):
    services.vpn_endpoint_remove(
        request.require_actor(), request.params["device_id"], request.params["profile_id"],
        request.body.get("confirmed") is True)
    return json_response({"ok": True})


def compatibility(request):
    body = request.body
    services.vpn_endpoint_compatibility(request.require_actor(), request.params["device_id"],
                                        body.get("candidateId"), body.get("targetId"),
                                        body.get("state"), body.get("note", ""))
    return json_response({"ok": True})


def switch(request):
    body = request.body
    return json_response(services.vpn_endpoint_switch(request.require_actor(), request.params["device_id"],
                                                       body.get("candidateId"), bool(body.get("confirmed"))))


def profile_compatibility(request):
    body = request.body
    services.vpn_endpoint_compatibility(
        request.require_actor(), request.params["device_id"], body.get("candidateId"),
        body.get("targetId"), body.get("state"), body.get("note", ""),
        request.params["profile_id"])
    return json_response({"ok": True})


def profile_switch(request):
    body = request.body
    return json_response(services.vpn_endpoint_switch(
        request.require_actor(), request.params["device_id"], body.get("candidateId"),
        bool(body.get("confirmed")), request.params["profile_id"]))


def routes():
    return (
        Route("GET", "/api/devices/{device_id}/vpn-endpoints/choices", choices, name="vpn-endpoint-choices"),
        Route("GET", "/api/devices/{device_id}/vpn-endpoints", status, name="vpn-endpoint-status"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints", create, name="vpn-endpoint-create"),
        Route("PATCH", "/api/devices/{device_id}/vpn-endpoints", configure, name="vpn-endpoint-configure"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/compatibility", compatibility,
              name="vpn-endpoint-compatibility"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/switch", switch, name="vpn-endpoint-switch"),
        Route("GET", "/api/devices/{device_id}/vpn-endpoints/{profile_id}", profile_status,
              name="vpn-endpoint-profile-status"),
        Route("PATCH", "/api/devices/{device_id}/vpn-endpoints/{profile_id}", configure_profile,
              name="vpn-endpoint-profile-configure"),
        Route("DELETE", "/api/devices/{device_id}/vpn-endpoints/{profile_id}", remove_profile,
              name="vpn-endpoint-profile-remove"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/{profile_id}/compatibility",
              profile_compatibility, name="vpn-endpoint-profile-compatibility"),
        Route("POST", "/api/devices/{device_id}/vpn-endpoints/{profile_id}/switch",
              profile_switch, name="vpn-endpoint-profile-switch"),
    )
