"""Client roster read, export, and lifecycle routes."""
import time

import services

from backend.http.responses import FileResponse, json_response
from backend.http.router import Route


def list_clients(request):
    return json_response(services.list_clients(request.require_actor()))


def client_history(request):
    return json_response(services.client_history(request.require_actor(), request.query_value("mac")))


def client_events(request):
    return json_response(services.client_events(request.require_actor(), request.query_value("since", "0")))


def export_clients(request):
    data, content_type, extension = services.export_clients(
        request.require_actor(), request.query_value("format", "json"))
    filename = time.strftime("homelabhq-clients-%Y%m%d-%H%M") + "." + extension
    return FileResponse(data=data, content_type=content_type, filename=filename)


def forget_clients(request):
    body, actor = request.body, request.require_actor()
    macs = body.get("macs")
    if isinstance(macs, list):
        result = services.forget_clients(actor, macs)
    else:
        result = services.forget_client(actor, body.get("mac"))
    return json_response(result)


def routes():
    return (
        Route("GET", "/api/clients", list_clients, name="clients-list"),
        Route("GET", "/api/clients/history", client_history, name="clients-history"),
        Route("GET", "/api/clients/events", client_events, name="clients-events"),
        Route("GET", "/api/clients/export", export_clients, name="clients-export"),
        Route("POST", "/api/clients/forget", forget_clients, name="clients-forget"),
    )
