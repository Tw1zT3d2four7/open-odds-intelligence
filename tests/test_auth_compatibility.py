from fastapi.testclient import TestClient

from app import main


def test_legacy_basic_auth_unconfigured_mode_preserves_existing_open_behavior(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")

    response = TestClient(main.app).get("/")
    assert response.status_code == 200
    assert "Open Odds Intelligence" in response.text


def test_legacy_basic_auth_requires_credentials_when_both_are_configured(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "case-Sensitive-Secret")

    client = TestClient(main.app)
    denied = client.get("/")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic ")

    accepted = client.get("/", auth=("operator", "case-Sensitive-Secret"))
    assert accepted.status_code == 200

    wrong_password = client.get("/", auth=("operator", "case-sensitive-secret"))
    assert wrong_password.status_code == 401


def test_legacy_basic_auth_partial_configuration_fails_closed_but_health_stays_public(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(main.service, "status", lambda: {"status": "ok"})
    monkeypatch.setattr(main.limits_monitor, "status", lambda: {"status": "ok"})

    client = TestClient(main.app)
    denied = client.get("/")
    assert denied.status_code == 503
    assert "misconfigured" in denied.json()["detail"].lower()

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
