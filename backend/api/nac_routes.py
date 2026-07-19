"""Access-control configuration and client editing routes."""
import services

from backend.http.responses import json_response
from backend.http.router import Route


def get_config(request):
    return json_response(services.get_nac_config(request.require_actor()))


def set_config(request):
    body = request.body
    return json_response(services.set_nac_config(request.require_actor(),
                                                  body.get("managedAliases") or [],
                                                  body.get("dnsSync") or {}))


def ignore(request):
    return json_response(services.nac_ignore(request.require_actor(), request.body.get("mac")))


def membership(request):
    body = request.body
    return json_response(services.client_membership(request.require_actor(), body.get("mac"),
                                                    body.get("ip") or ""))


def edit_client(request):
    body = request.body
    sync_dns, notify = body.get("syncDns"), body.get("notify")
    result = services.edit_client(
        request.require_actor(), body.get("mac"), ip=body.get("ip") or "",
        name=body.get("name") or "", notes=body.get("notes") or "",
        hostname=body.get("hostname") or "",
        sync_dns=None if sync_dns is None else bool(sync_dns),
        alias_changes=body.get("aliasChanges") or {},
        notify=None if notify is None else bool(notify),
    )
    return json_response(result)


def create_alias(request):
    body = request.body
    return json_response(services.create_managed_alias(request.require_actor(), body.get("name"),
                                                       body.get("type") or "host"))


def routes():
    return (
        Route("GET", "/api/nac/config", get_config, name="nac-config-get"),
        Route("POST", "/api/nac/config", set_config, name="nac-config-set"),
        Route("POST", "/api/nac/ignore", ignore, name="nac-ignore"),
        Route("POST", "/api/nac/client/membership", membership, name="nac-membership"),
        Route("POST", "/api/nac/client", edit_client, name="nac-client-edit"),
        Route("POST", "/api/nac/alias", create_alias, name="nac-alias-create"),
    )
