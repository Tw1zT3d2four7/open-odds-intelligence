
from app import main
from app.route_policy import (
    IDENTITY_ROUTE_ACCESS_POLICY,
    ROUTE_ACCESS_POLICY,
    application_route_access_policy,
    classify_route,
    stale_policy_entries,
    unclassified_routes,
)


def _application_routes():
    # Include framework-generated docs/schema routes, not only APIRoute objects.
    return [route for route in main.app.routes if getattr(route, "methods", None)]


def test_every_application_route_has_an_explicit_target_access_class():
    routes = _application_routes()
    assert len(routes) == 51
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
    assert len(actual) == 51
    assert set(application_route_access_policy()) == actual


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



def test_head_requests_inherit_the_get_route_access_class():
    assert classify_route("HEAD", "/api/odds") == "subscriber_entitled"
    assert classify_route("HEAD", "/api/health") == "public_minimal"
    assert classify_route("HEAD", "/docs") == "administrator_only"



def test_every_isolated_identity_router_route_has_an_explicit_access_policy():
    from app.identity_routes import router as identity_router

    routes = [
        route for route in identity_router.routes
        if getattr(route, "methods", None)
    ]
    assert len(IDENTITY_ROUTE_ACCESS_POLICY) == 26
    assert len(routes) == 26
    assert unclassified_routes(routes, IDENTITY_ROUTE_ACCESS_POLICY) == []
    assert stale_policy_entries(routes, IDENTITY_ROUTE_ACCESS_POLICY) == []


def test_identity_route_policy_distinguishes_public_mutations_and_session_logout():
    assert classify_route("GET", "/auth/csrf", IDENTITY_ROUTE_ACCESS_POLICY) == "public_minimal"
    assert classify_route("GET", "/admin", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"
    assert classify_route("GET", "/admin/overview", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"
    assert classify_route("GET", "/admin/audit-events", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"
    assert classify_route("GET", "/admin/subscribers", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"
    assert classify_route("POST", "/admin/subscribers/{identity_id}/status", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"
    for path in (
        "/auth/register",
        "/auth/verification/resend",
        "/auth/verify-email",
        "/auth/password-reset/request",
        "/auth/password-reset/consume",
        "/auth/login",
        "/admin/auth/login",
    ):
        assert classify_route("POST", path, IDENTITY_ROUTE_ACCESS_POLICY) == "public_csrf"
    assert classify_route("POST", "/auth/logout", IDENTITY_ROUTE_ACCESS_POLICY) == "subscriber_only"
    assert classify_route("POST", "/admin/auth/logout", IDENTITY_ROUTE_ACCESS_POLICY) == "administrator_only"



def test_identity_router_security_dependencies_match_its_policy_manifest():
    from app.dependencies import (
        require_administrator,
        require_authenticated_csrf,
        require_subscriber,
    )
    from app.identity_routes import router as identity_router
    from app.route_authorization import dependencies_for_route

    security_dependencies = {
        require_administrator,
        require_authenticated_csrf,
        require_subscriber,
    }
    for route in identity_router.routes:
        for method in (getattr(route, "methods", None) or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            expected = {
                item.dependency
                for item in dependencies_for_route(
                    method, route.path, policy=IDENTITY_ROUTE_ACCESS_POLICY
                )
            }
            actual = {
                item.call for item in route.dependant.dependencies
                if item.call in security_dependencies
            }
            assert actual == expected, (method, route.path, actual, expected)



def test_identity_auth_endpoints_are_excluded_from_general_openapi_schema():
    paths = main.app.openapi()["paths"]
    assert all(path not in paths for _method, path in IDENTITY_ROUTE_ACCESS_POLICY)



def test_every_subscriber_product_route_has_an_entitlement_mapping_and_dependency():
    from app.entitlements import ROUTE_ENTITLEMENTS, require_product_entitlement, require_product_subscriber
    from app import main

    subscriber_paths = {
        path for (method, path), access in ROUTE_ACCESS_POLICY.items()
        if access == "subscriber_entitled"
    }
    assert set(ROUTE_ENTITLEMENTS) == subscriber_paths

    for route in main.app.routes:
        if getattr(route, "path", None) not in subscriber_paths:
            continue
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_product_subscriber in calls
        assert require_product_entitlement in calls


def test_admin_product_mutations_have_mode_aware_admin_and_csrf_dependencies():
    from app.entitlements import require_product_administrator, require_product_csrf
    from app import main

    for path in ("/api/limits/refresh", "/api/refresh", "/api/simulation/games/refresh"):
        route = next(route for route in main.app.routes if getattr(route, "path", None) == path)
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_product_administrator in calls
        assert require_product_csrf in calls
