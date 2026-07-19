"""Device, driver, firewall, and device-scoped NAC routes."""
import detect
import services
import transports
from drivers import registry

from errors import UpstreamUnavailable, ValidationError
from backend.http.router import Route
from backend.http.responses import json_response


def list_drivers(request):
    transport = request.query_value("transport")
    drivers = registry.for_transport(transport) if transport else registry.all_drivers()
    result = sorted(({"id": driver.id, "displayName": driver.display_name,
                      "transports": driver.transports} for driver in drivers),
                    key=lambda driver: driver["displayName"])
    return json_response({"drivers": result,
                          "transports": sorted({t for d in result for t in d["transports"]})})


def list_devices(request):
    return json_response({"devices": services.list_devices(request.require_actor())})


def detect_device(request):
    body = request.body
    try:
        result = detect.detect(body.get("transport"), body.get("host"), body.get("port"),
                               body.get("credentials"))
    except transports.ConnectionError as error:
        raise UpstreamUnavailable(str(error)) from error
    except Exception as error:
        raise ValidationError(str(error)) from error
    return json_response(result)


def enumerate_entities(request):
    body = request.body
    try:
        entities = detect.enumerate_entities(body.get("transport"), body.get("host"),
                                             body.get("port"), body.get("credentials"),
                                             body.get("driverId"))
    except transports.ConnectionError as error:
        raise UpstreamUnavailable(str(error)) from error
    except Exception as error:
        raise ValidationError(str(error)) from error
    driver = registry.get(body.get("driverId"))
    return json_response({"entities": entities,
                          "supportsBinding": bool(getattr(driver, "supports_binding", False)),
                          "nacSupported": bool(getattr(driver, "nac_supported", False))})


def create_device(request):
    body = request.body
    record = services.create_device(
        request.require_actor(), host=body.get("host"), transport=body.get("transport"),
        port=body.get("port"), credentials=body.get("credentials"),
        driver_id=body.get("driverId"), name=body.get("name"), entities=body.get("entities"),
        dashboard_id=body.get("dashboardId"), ap_binding=bool(body.get("apBinding")),
    )
    response = {"device": record}
    warning = record.pop("bindingWarning", None)
    if warning:
        response["bindingWarning"] = warning
    return json_response(response)


def reorder_devices(request):
    count = services.reorder_devices(request.require_actor(), request.body.get("ids") or [])
    return json_response({"reordered": count})


def delete_device(request):
    device_id = request.query_value("id")
    if not device_id:
        raise ValidationError("id required")
    services.delete_device(request.require_actor(), device_id)
    return json_response({"ok": True})


def update_device(request):
    body = request.body
    fields = {}
    for source, target in (("name", "name"), ("dashboardId", "dashboard_id"),
                           ("entities", "entities"),
                           ("hiddenInterfaces", "hidden_interfaces"),
                           ("driverId", "driver_id"), ("alerts", "alerts")):
        if source in body:
            fields[target] = body.get(source)
    record = services.update_device(request.require_actor(), request.params["device_id"], **fields)
    return json_response({"device": record})


def history(request):
    device_id = request.params["device_id"]
    key, range_name = request.query_value("key"), request.query_value("range")
    series = services.device_history(request.require_actor(), device_id, key, range_name)
    return json_response({"key": key, "range": range_name, "series": series})


def state(request):
    return json_response(services.device_state(request.require_actor(), request.params["device_id"]))


def series(request):
    metric, identifier = request.query_value("metric"), request.query_value("id")
    data = services.device_series(request.require_actor(), request.params["device_id"], metric, identifier)
    return json_response({"metric": metric, "id": identifier, "series": data})


def detail(request):
    return json_response(services.device_detail(request.require_actor(), request.params["device_id"]))


def action(request):
    body = request.body
    result = services.device_action(request.require_actor(), request.params["device_id"],
                                    body.get("action"), body.get("args") or {})
    return json_response(result)


def firewall_all(request):
    return json_response({"rules": services.firewall_all(
        request.require_actor(), request.params["device_id"])})


def firewall_toggle(request):
    body = request.body
    return json_response(services.firewall_toggle(request.require_actor(), request.params["device_id"],
                                                  body.get("uuid"), bool(body.get("enabled"))))


def firewall_rules(request):
    rules = services.firewall_set_managed(request.require_actor(), request.params["device_id"],
                                          request.body.get("rules") or [])
    return json_response({"rules": rules})


def nac_interfaces(request):
    return json_response({"interfaces": services.nac_interfaces(
        request.require_actor(), request.params["device_id"])})


def nac_aliases(request):
    return json_response({"aliases": services.nac_aliases(
        request.require_actor(), request.params["device_id"])})


def nac_setup(request):
    body, actor, device_id = request.body, request.require_actor(), request.params["device_id"]
    if body.get("mode") == "existing":
        record = services.nac_setup_existing(actor, device_id, body.get("existingUuid"))
        return json_response({"device": record, "seeded": 0})
    seed = []
    if body.get("seedExisting"):
        try:
            clients = services.list_clients(actor)
            seed = [client["mac"] for client in clients.get("clients", []) if client.get("mac")]
        except Exception:
            seed = []
    record = services.nac_setup(actor, device_id, body.get("alias"), body.get("interface"), seed)
    return json_response({"device": record, "seeded": len(seed)})


def nac_approve(request):
    body, actor, device_id = request.body, request.require_actor(), request.params["device_id"]
    macs = body.get("macs")
    if isinstance(macs, list):
        result = services.nac_approve_many(actor, device_id, macs, bool(body.get("approved", True)))
    else:
        result = services.nac_approve(actor, device_id, body.get("mac"), bool(body.get("approved")))
    return json_response(result)


def nac_enforcement(request):
    result = services.nac_set_enforcement(request.require_actor(), request.params["device_id"],
                                          bool(request.body.get("enabled")))
    return json_response({"device": result})


def binding(request):
    record, warning = services.set_ap_binding(request.require_actor(), request.params["device_id"],
                                              bool(request.body.get("enabled")))
    if record is None:
        raise ValidationError("unknown device")
    response = {"device": record}
    if warning:
        response["bindingWarning"] = warning
    return json_response(response)


def bind_client(request):
    body = request.body
    record = services.set_client_binding(request.require_actor(), request.params["device_id"],
                                         body.get("mac"), bool(body.get("bound")))
    if record is None:
        raise ValidationError("unknown device")
    return json_response({"device": record})


def routes():
    return (
        Route("GET", "/api/drivers", list_drivers, name="drivers-list"),
        Route("GET", "/api/devices", list_devices, name="devices-list"),
        Route("POST", "/api/devices/detect", detect_device, name="devices-detect"),
        Route("POST", "/api/devices/entities", enumerate_entities, name="devices-entities"),
        Route("POST", "/api/devices", create_device, name="devices-create"),
        Route("POST", "/api/devices/reorder", reorder_devices, name="devices-reorder"),
        Route("DELETE", "/api/devices", delete_device, name="devices-delete"),
        Route("PATCH", "/api/devices/{device_id}", update_device, name="devices-update"),
        Route("GET", "/api/devices/{device_id}/history", history, name="devices-history"),
        Route("GET", "/api/devices/{device_id}/state", state, name="devices-state"),
        Route("GET", "/api/devices/{device_id}/series", series, name="devices-series"),
        Route("GET", "/api/devices/{device_id}/detail", detail, name="devices-detail"),
        Route("POST", "/api/devices/{device_id}/action", action, name="devices-action"),
        Route("GET", "/api/devices/{device_id}/firewall/all", firewall_all, name="firewall-all"),
        Route("POST", "/api/devices/{device_id}/firewall/toggle", firewall_toggle, name="firewall-toggle"),
        Route("POST", "/api/devices/{device_id}/firewall/rules", firewall_rules, name="firewall-rules"),
        Route("GET", "/api/devices/{device_id}/nac/interfaces", nac_interfaces, name="nac-interfaces"),
        Route("GET", "/api/devices/{device_id}/nac/aliases", nac_aliases, name="nac-aliases"),
        Route("POST", "/api/devices/{device_id}/nac/setup", nac_setup, name="nac-setup"),
        Route("POST", "/api/devices/{device_id}/nac/approve", nac_approve, name="nac-approve"),
        Route("POST", "/api/devices/{device_id}/nac/enforcement", nac_enforcement, name="nac-enforcement"),
        Route("POST", "/api/devices/{device_id}/binding", binding, name="devices-binding"),
        Route("POST", "/api/devices/{device_id}/bind-client", bind_client, name="devices-bind-client"),
    )
