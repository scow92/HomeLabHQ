"""Morning update-check settings, execution, and persisted result routes."""
import services

from errors import NotFound
from backend.api.contracts import AuthPolicy, Route, json_response


def get_settings(request):
    return json_response(services.morning_update_settings(request.require_actor()))


def save_settings(request):
    body = request.body
    return json_response(services.save_morning_update_settings(
        request.require_actor(), config=body.get("config") if "config" in body else None,
        notifications=(body.get("notifications")
                       if "notifications" in body else None)))


def start_run(request):
    run = services.start_morning_update_run(request.require_actor())
    return json_response({"run": run}, status=202)


def latest_run(request):
    run = services.morning_update_run(request.require_actor(), None)
    return json_response({"run": run})


def get_run(request):
    run = services.morning_update_run(
        request.require_actor(), request.params["run_id"])
    if run is None:
        raise NotFound()
    return json_response({"run": run})


def routes():
    authenticated = AuthPolicy.AUTHENTICATED
    admin = AuthPolicy.ADMIN
    return (
        Route("GET", "/api/settings/morning-updates", get_settings, authenticated,
              "morning-update-settings"),
        Route("POST", "/api/settings/morning-updates", save_settings, authenticated,
              "morning-update-settings-save"),
        Route("POST", "/api/morning-updates/run", start_run, admin,
              "morning-update-run"),
        Route("GET", "/api/morning-updates/runs/latest", latest_run, authenticated,
              "morning-update-latest"),
        Route("GET", "/api/morning-updates/runs/{run_id}", get_run, authenticated,
              "morning-update-result"),
    )
