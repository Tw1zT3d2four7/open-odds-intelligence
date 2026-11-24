from fastapi.routing import APIRoute

from app import main
from app.route_policy import (
    ROUTE_ACCESS_POLICY,
    classify_route,
    stale_policy_entries,
    unclassified_routes,
)


def _application_routes():
    return [route for route in main.app.routes if isinstance(route, APIRoute)]


def test_every_application_route_has_an_explicit_target_access_class():
    routes = _application_routes()
    assert unclassified_routes(routes) == []
    assert stale_policy_entries(routes) == []


def test_policy_inventory_covers_all_21_existing_http_routes():
    routes = _application_routes()
    actual = {
        (method, route.path)
        for route in routes
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert len(actual) == 21
    assert set(ROUTE_ACCESS_POLICY) == actual


def test_shared_provider_refresh_routes_require_administrator_access():
    assert classify_route("POST", "/api/refresh") == "administrator_only"
    assert classify_route("POST", "/api/limits/refresh") == "administrator_only"
    assert classify_route("POST", "/api/simulation/games/refresh") == "administrator_only"


def test_read_and_simulation_routes_require_subscriber_entitlement():
    for method, path in (
        ("GET", "/"),
        ("GET", "/api/odds"),
        ("GET", "/api/opportunities"),
        ("GET", "/api/arbitrage"),
        ("POST", "/api/simulation/run"),
    ):
        assert classify_route(method, path) == "subscriber_entitled"


def test_health_is_the_only_public_target_route():
    public = [
        key for key, access_class in ROUTE_ACCESS_POLICY.items()
        if access_class == "public_minimal"
    ]
    assert public == [("GET", "/api/health")]


def test_unknown_route_is_unclassified_not_implicitly_public():
    assert classify_route("GET", "/api/new-endpoint") is None
    assert classify_route("POST", "/api/odds") is None
