import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture
def client():
    return TestClient(main.app)


def test_legacy_basic_auth_allows_requests_when_both_credentials_are_unset(
    client, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")

    assert client.get("/").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_legacy_basic_auth_fails_closed_on_partial_configuration(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")

    denied = client.get("/")
    assert denied.status_code == 503
    assert "Authentication is misconfigured" in denied.json()["detail"]
    # Container liveness remains independent of browser authentication.
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("path", ["/", "/api/odds", "/openapi.json", "/docs", "/redoc"])
def test_legacy_basic_auth_protects_dashboard_and_framework_docs(
    client, monkeypatch, path
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")

    missing = client.get(path)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic ")

    invalid = client.get(path, auth=("operator", "wrong-secret"))
    assert invalid.status_code == 401

    accepted = client.get(path, auth=("operator", "case-sensitive-secret"))
    assert accepted.status_code == 200


def test_legacy_basic_auth_health_route_remains_public_when_configured(
    client, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")

    response = client.get("/api/health")
    assert response.status_code == 200



@pytest.mark.parametrize(
    "authorization",
    [
        "Basic !!!",
        "Basic bm8tY29sb24=",  # valid base64, but no username/password separator
        "Bearer not-a-basic-token",
        "Basic /w==",  # decoded bytes are not valid UTF-8
    ],
)
def test_legacy_basic_auth_rejects_malformed_authorization_headers(
    client, monkeypatch, authorization
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")

    response = client.get(
        "/api/odds", headers={"authorization": authorization}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


def test_legacy_basic_auth_scheme_is_case_insensitive(client, monkeypatch):
    import base64

    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")
    token = base64.b64encode(b"operator:case-sensitive-secret").decode("ascii")

    response = client.get(
        "/api/odds", headers={"authorization": f"bAsIc {token}"}
    )
    assert response.status_code == 200



@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_legacy_basic_auth_does_not_exempt_mutating_or_preflight_health_methods(
    client, monkeypatch, method
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")

    response = client.request(method, "/api/health")
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


def test_legacy_basic_auth_health_bypass_is_limited_to_safe_methods(
    client, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "case-sensitive-secret")

    assert client.get("/api/health").status_code == 200
    # HEAD is not explicitly registered on this endpoint, so the route may
    # return 405; the auth middleware must still let the safe probe through.
    assert client.head("/api/health").status_code == 405

    denied = client.post("/api/health")
    assert denied.status_code == 401

    authenticated = client.post(
        "/api/health", auth=("operator", "case-sensitive-secret")
    )
    assert authenticated.status_code == 405


def test_identity_admin_errors_and_auth_pages_are_never_cached(client, monkeypatch, tmp_path):
    from app.identity import IdentityStore
    from app.dependencies import get_identity_store

    monkeypatch.setattr(config, "AUTH_MODE", "identity")
    store = IdentityStore(tmp_path / "middleware-identities.sqlite3")
    main.app.dependency_overrides[get_identity_store] = lambda: store
    try:
        page = client.get("/login")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["pragma"] == "no-cache"

        denied = client.get("/admin")
        assert denied.status_code == 401
        assert denied.headers["cache-control"] == "no-store"
        assert denied.headers["pragma"] == "no-cache"

        docs = client.get("/docs")
        assert docs.status_code == 401
        assert docs.headers["cache-control"] == "no-store"
        assert docs.headers["pragma"] == "no-cache"

        schema = client.get("/openapi.json")
        assert schema.status_code == 401
        assert schema.headers["cache-control"] == "no-store"
        assert schema.headers["pragma"] == "no-cache"

        subscriber_page = client.get("/account")
        assert subscriber_page.status_code == 401
        assert subscriber_page.headers["cache-control"] == "no-store"
        assert subscriber_page.headers["pragma"] == "no-cache"

        subscriber_summary = client.get("/auth/account")
        assert subscriber_summary.status_code == 401
        assert subscriber_summary.headers["cache-control"] == "no-store"
        assert subscriber_summary.headers["pragma"] == "no-cache"
    finally:
        main.app.dependency_overrides.pop(get_identity_store, None)


def test_parameterized_admin_routes_are_classified_in_identity_mode_and_hidden_in_legacy_mode(
    client, monkeypatch, tmp_path
):
    from app.identity import IdentityStore
    from app.dependencies import get_identity_store

    store = IdentityStore(tmp_path / "parameterized-route-identities.sqlite3")
    main.app.dependency_overrides[get_identity_store] = lambda: store
    paths_and_payloads = (
        ("/admin/subscribers/subscriber-123/status", {"is_active": False, "reason_code": "other"}),
        ("/admin/subscribers/subscriber-123/sessions/revoke", {"reason_code": "other"}),
        ("/admin/sessions/session-456/revoke", {"reason_code": "other"}),
    )
    try:
        monkeypatch.setattr(config, "AUTH_MODE", "identity")
        for path, payload in paths_and_payloads:
            response = client.post(path, json=payload)
            # A recognized route reaches its administrator dependency (401),
            # rather than being rejected as an unclassified path (503).
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["pragma"] == "no-cache"

        monkeypatch.setattr(config, "AUTH_MODE", "legacy_basic")
        monkeypatch.setattr(config, "AUTH_USERNAME", "")
        monkeypatch.setattr(config, "AUTH_PASSWORD", "")
        for path, payload in paths_and_payloads:
            response = client.post(path, json=payload)
            assert response.status_code == 404
    finally:
        main.app.dependency_overrides.pop(get_identity_store, None)
