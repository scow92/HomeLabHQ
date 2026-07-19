"""Dashboard CRUD routes."""
import services

from errors import ValidationError
from backend.http.router import AuthPolicy, Route
from backend.http.responses import json_response


def list_dashboards(request):
    return json_response({"dashboards": services.list_dashboards(request.require_actor())})


def create_dashboard(request):
    return json_response({"dashboard": services.create_dashboard(
        request.require_actor(), request.body.get("name"))})


def update_dashboard(request):
    body = request.body
    fields = {}
    if "name" in body:
        fields["name"] = body.get("name")
    if "order" in body:
        fields["order"] = body.get("order")
    result = services.update_dashboard(request.require_actor(), request.params["dashboard_id"], **fields)
    return json_response({"dashboard": result})


def delete_dashboard(request):
    dashboard_id = request.query_value("id")
    if not dashboard_id:
        raise ValidationError("id required")
    services.delete_dashboard(request.require_actor(), dashboard_id)
    return json_response({"ok": True})


def routes():
    return (
        Route("GET", "/api/dashboards", list_dashboards, name="dashboards-list"),
        Route("POST", "/api/dashboards", create_dashboard, name="dashboards-create"),
        Route("PATCH", "/api/dashboards/{dashboard_id}", update_dashboard,
              name="dashboards-update"),
        Route("DELETE", "/api/dashboards", delete_dashboard, name="dashboards-delete"),
    )
