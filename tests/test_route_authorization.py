import pytest

from app.dependencies import require_authenticated_csrf
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
    ) == [require_subscriber, require_entitlement, require_authenticated_csrf]


def test_administrator_mutations_require_admin_session_and_csrf():
    assert _dependencies(
        "POST", "/api/refresh"
    ) == [require_administrator, require_authenticated_csrf]


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
    route_policy.ROUTE_ACCESS_POLICY[("TRACE", "/api/test-public-trace")] = "public_minimal"
    try:
        with pytest.raises(ValueError, match="state-changing"):
            dependencies_for_route("POST", "/api/test-public-write")
        with pytest.raises(ValueError, match="state-changing"):
            dependencies_for_route("TRACE", "/api/test-public-trace")
    finally:
        route_policy.ROUTE_ACCESS_POLICY.pop(("POST", "/api/test-public-write"))
        route_policy.ROUTE_ACCESS_POLICY.pop(("TRACE", "/api/test-public-trace"))



def test_dependency_assembly_covers_every_declared_route_class():
    from app.route_policy import ROUTE_ACCESS_POLICY

    def entitlement_guard():
        return True

    for (method, path), access_class in ROUTE_ACCESS_POLICY.items():
        resolved = _dependencies(method, path, entitlement_guard)
        if access_class == "public_minimal":
            assert resolved == [], (method, path)
        elif access_class == "administrator_only":
            expected = [require_administrator]
            if method not in {"GET", "HEAD", "OPTIONS"}:
                expected.append(require_authenticated_csrf)
            assert resolved == expected, (method, path)
        elif access_class == "subscriber_entitled":
            expected = [require_subscriber, entitlement_guard]
            if method not in {"GET", "HEAD", "OPTIONS"}:
                expected.append(require_authenticated_csrf)
            assert resolved == expected, (method, path)
        else:
            pytest.fail(f"Unsupported access class for {method} {path}: {access_class}")


def test_uncommon_authenticated_http_methods_also_require_csrf():
    from app import route_policy

    route_policy.ROUTE_ACCESS_POLICY[("TRACE", "/api/test-admin-trace")] = "administrator_only"
    try:
        assert _dependencies("TRACE", "/api/test-admin-trace") == [
            require_administrator, require_authenticated_csrf
        ]
    finally:
        route_policy.ROUTE_ACCESS_POLICY.pop(("TRACE", "/api/test-admin-trace"))



def _make_policy_integration_app():
    from fastapi import Depends, FastAPI, HTTPException
    from app.auth import (
        ADMINISTRATOR_COOKIE,
        CSRF_COOKIE,
        CSRF_HEADER,
        SUBSCRIBER_COOKIE,
    )

    class SessionStore:
        sessions = {
            "subscriber-entitled": {
                "account_type": "subscriber", "identity_id": "sub-1",
                "entitled": True,
            },
            "subscriber-free": {
                "account_type": "subscriber", "identity_id": "sub-2",
                "entitled": False,
            },
            "administrator": {
                "account_type": "administrator", "identity_id": "admin-1",
            },
        }

        def get_session(self, token):
            return self.sessions.get(token)

    app = FastAPI()
    app.state.identity_store = SessionStore()

    def entitlement_guard(session=Depends(require_subscriber)):
        if not session.get("entitled"):
            raise HTTPException(status_code=403, detail="Feature not included in plan.")

    @app.get(
        "/api/odds",
        dependencies=dependencies_for_route(
            "GET", "/api/odds", entitlement_dependency=entitlement_guard
        ),
    )
    def odds_endpoint():
        return {"ok": True}

    @app.head(
        "/api/odds",
        dependencies=dependencies_for_route(
            "HEAD", "/api/odds", entitlement_dependency=entitlement_guard
        ),
    )
    def odds_head_endpoint():
        return {"ok": True}

    @app.post(
        "/api/refresh",
        dependencies=dependencies_for_route("POST", "/api/refresh"),
    )
    def refresh_endpoint():
        return {"ok": True}

    @app.get(
        "/api/health",
        dependencies=dependencies_for_route("GET", "/api/health"),
    )
    def health_endpoint():
        return {"status": "ok"}

    return app, {
        "admin_cookie": ADMINISTRATOR_COOKIE,
        "csrf_cookie": CSRF_COOKIE,
        "csrf_header": CSRF_HEADER,
        "subscriber_cookie": SUBSCRIBER_COOKIE,
    }


def test_policy_dependencies_enforce_subscriber_entitlement_over_http():
    from fastapi.testclient import TestClient

    app, cookies = _make_policy_integration_app()
    client = TestClient(app)

    assert client.get("/api/odds").status_code == 401
    client.cookies.set(cookies["subscriber_cookie"], "subscriber-free")
    free = client.get("/api/odds")
    assert free.status_code == 403

    client.cookies.clear()
    client.cookies.set(cookies["subscriber_cookie"], "subscriber-entitled")
    entitled = client.get("/api/odds")
    assert entitled.status_code == 200
    assert entitled.json() == {"ok": True}


def test_policy_dependencies_enforce_admin_session_and_csrf_over_http():
    from fastapi.testclient import TestClient

    app, cookies = _make_policy_integration_app()
    client = TestClient(app)

    client.cookies.set(cookies["subscriber_cookie"], "subscriber-entitled")
    subscriber = client.post(
        "/api/refresh",
        headers={
            "origin": "https://testserver",
            cookies["csrf_header"]: "csrf-value",
        },
    )
    assert subscriber.status_code == 401

    client.cookies.clear()
    client.cookies.set(cookies["admin_cookie"], "administrator")
    missing_csrf = client.post("/api/refresh")
    assert missing_csrf.status_code == 403

    client.cookies.set(cookies["csrf_cookie"], "csrf-value")
    accepted = client.post(
        "/api/refresh",
        headers={
            "origin": "https://testserver",
            cookies["csrf_header"]: "csrf-value",
        },
    )
    assert accepted.status_code == 200


def test_policy_dependencies_keep_minimal_health_route_public_over_http():
    from fastapi.testclient import TestClient

    app, _cookies = _make_policy_integration_app()
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}



def test_head_product_route_requires_subscriber_entitlement_without_csrf():
    def entitlement_guard():
        return True

    assert _dependencies(
        "HEAD", "/api/odds", entitlement_guard
    ) == [require_subscriber, entitlement_guard]



def test_head_product_endpoint_enforces_the_get_entitlement_policy_over_http():
    from fastapi.testclient import TestClient

    app, cookies = _make_policy_integration_app()
    client = TestClient(app)

    assert client.head("/api/odds").status_code == 401
    client.cookies.set(cookies["subscriber_cookie"], "subscriber-free")
    assert client.head("/api/odds").status_code == 403
    client.cookies.clear()
    client.cookies.set(cookies["subscriber_cookie"], "subscriber-entitled")
    assert client.head("/api/odds").status_code == 200



def test_route_policy_typo_fails_closed():
    from app import route_policy

    route_policy.ROUTE_ACCESS_POLICY[("GET", "/api/test-unsupported")] = "subscriber"
    try:
        with pytest.raises(ValueError, match="not supported"):
            dependencies_for_route(
                "GET", "/api/test-unsupported",
                entitlement_dependency=lambda: True,
            )
    finally:
        route_policy.ROUTE_ACCESS_POLICY.pop(("GET", "/api/test-unsupported"))


def test_non_callable_entitlement_dependency_fails_closed():
    with pytest.raises(RuntimeError, match="entitlement dependency"):
        dependencies_for_route("GET", "/api/odds", entitlement_dependency=object())
