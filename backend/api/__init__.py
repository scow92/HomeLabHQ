"""Route modules grouped by application domain."""


def all_routes():
    from . import (admin_routes, auth_routes, client_routes, dashboard_routes,
                   device_routes, nac_routes, push_routes, vpn_endpoints)
    modules = (auth_routes, admin_routes, push_routes, device_routes,
               dashboard_routes, client_routes, nac_routes, vpn_endpoints)
    return [route for module in modules for route in module.routes()]
