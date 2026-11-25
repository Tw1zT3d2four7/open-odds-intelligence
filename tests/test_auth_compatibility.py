import pytest
from fastapi.testclient import TestClient

from app import main


def test_legacy_basic_auth_unconfigured_mode_preserves_existing_open_behavior(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")

    monkeypatch.setattr(main.config, "AUTH_MODE", "legacy_basic")
    client = TestClient(main.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Open Odds Intelligence" in response.text
    assert client.get("/auth/csrf").status_code == 404
    for path in (
        "/login", "/register", "/admin/login", "/forgot-password",
        "/verify-email", "/reset-password", "/auth/csrf",
    ):
        assert client.get(path).status_code == 404


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



def test_identity_mode_fails_closed_until_full_route_cutover_is_enabled(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(main.service, "status", lambda: {"status": "ok"})
    monkeypatch.setattr(main.limits_monitor, "status", lambda: {"status": "ok"})

    client = TestClient(main.app)
    dashboard = client.get("/")
    assert dashboard.status_code == 503
    assert "not enabled" in dashboard.json()["detail"].lower()
    assert client.get("/api/odds").status_code == 503

    # Only the separately protected identity entry points are enabled in this
    # staged mode; the detailed provider health payload is not public.
    csrf = client.get("/auth/csrf")
    assert csrf.status_code == 200
    assert csrf.headers["cache-control"] == "no-store"

    for path in (
        "/login", "/register", "/admin/login", "/forgot-password",
        "/verify-email", "/reset-password",
    ):
        page = client.get(path)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "Open Odds Intelligence" in page.text

    health = client.get("/api/health")
    assert health.status_code == 200
    assert set(health.json()) == {"status", "app"}


def test_identity_mode_never_falls_back_to_configured_legacy_basic_credentials(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "legacy-secret")

    client = TestClient(main.app)
    response = client.get("/", auth=("operator", "legacy-secret"))
    assert response.status_code == 503
    assert "not enabled" in response.json()["detail"].lower()



def test_unknown_authentication_mode_is_rejected_during_configuration_load(monkeypatch):
    import importlib
    import pytest
    from app import config

    monkeypatch.setenv("AUTH_MODE", "open")
    with pytest.raises(ValueError, match="AUTH_MODE"):
        importlib.reload(config)

    # Restore the shared config module for the remaining application tests.
    monkeypatch.setenv("AUTH_MODE", "legacy_basic")
    importlib.reload(config)



@pytest.mark.parametrize(
    "path",
    ["/login", "/register", "/admin/login", "/forgot-password", "/verify-email", "/reset-password"],
)
def test_legacy_basic_credentials_do_not_expose_identity_pages(monkeypatch, path):
    monkeypatch.setattr(main.config, "AUTH_MODE", "legacy_basic")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "legacy-secret")
    client = TestClient(main.app)
    assert client.get(path, auth=("operator", "legacy-secret")).status_code == 404
