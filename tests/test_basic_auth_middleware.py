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


@pytest.mark.parametrize("path", ["/", "/openapi.json", "/docs", "/redoc"])
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
