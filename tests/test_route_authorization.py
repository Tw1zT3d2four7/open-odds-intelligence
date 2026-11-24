import pytest

from app.auth import require_same_origin_csrf
from app.dependencies import require_administrator, require_subscriber
from app.route_authorization import dependencies_for_route


def _dependencies(method, path, entitlement=None):
    return [
        dependency.dependency
        for dependency in dependencies_for_route(
            method, path, entitlement_dependency=entitlement
        )
    ]


def test_subscriber_routes_require_session_and_explicit_entitlement_dependency():
    def require_entitlement():
        return True

    assert _dependencies(
        "GET", "/api/odds", require_entitlement
    ) == [require_subscriber, require_entitlement]


def test_subscriber_mutations_also_require_csrf():
    def require_entitlement():
        return True

    assert _dependencies(
        "POST", "/api/simulation/run", require_entitlement
    ) == [require_subscriber, require_entitlement, require_same_origin_csrf]


def test_administrator_mutations_require_admin_session_and_csrf():
    assert _dependencies(
        "POST", "/api/refresh"
    ) == [require_administrator, require_same_origin_csrf]


def test_administrator_read_routes_require_admin_session_without_csrf():
    assert _dependencies("GET", "/docs") == [require_administrator]


def test_public_health_route_has_no_browser_auth_dependency():
    assert _dependencies("GET", "/api/health") == []


def test_subscriber_route_without_entitlement_dependency_fails_closed():
    with pytest.raises(RuntimeError, match="entitlement dependency"):
        dependencies_for_route("GET", "/api/odds")


def test_unknown_route_fails_closed():
    with pytest.raises(ValueError, match="explicit access policy"):
        dependencies_for_route("GET", "/api/not-classified")


def test_public_route_cannot_be_state_changing():
    # The current manifest intentionally has no public mutation. If one is
    # ever added, this helper refuses to turn it into a CSRF-free write route.
    from app import route_policy

    route_policy.ROUTE_ACCESS_POLICY[("POST", "/api/test-public-write")] = "public_minimal"
    try:
        with pytest.raises(ValueError, match="state-changing"):
            dependencies_for_route("POST", "/api/test-public-write")
    finally:
        route_policy.ROUTE_ACCESS_POLICY.pop(("POST", "/api/test-public-write"))
