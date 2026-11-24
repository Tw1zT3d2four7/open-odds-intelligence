
from app import main
from app.route_policy import (
    ROUTE_ACCESS_POLICY,
    classify_route,
    stale_policy_entries,
    unclassified_routes,
)


def _application_routes():
    # Include framework-generated docs/schema routes, not only APIRoute objects.
    return [route for route in main.app.routes if getattr(route, "methods", None)]


def test_every_application_route_has_an_explicit_target_access_class():
    routes = _application_routes()
    assert unclassified_routes(routes) == []
    assert stale_policy_entries(routes) == []


def test_policy_inventory_covers_all_application_and_framework_http_routes():
    routes = _application_routes()
    actual = {
        (method, route.path)
        for route in routes
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert len(actual) == 25
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


def test_framework_schema_and_interactive_docs_are_not_implicitly_public():
    for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
        assert classify_route("GET", path) == "administrator_only"


def test_unknown_route_is_unclassified_not_implicitly_public():
    assert classify_route("GET", "/api/new-endpoint") is None
    assert classify_route("POST", "/api/odds") is None
