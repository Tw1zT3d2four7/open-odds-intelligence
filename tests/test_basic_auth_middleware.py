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
