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



def test_identity_mode_protects_product_routes_and_exposes_only_staged_auth_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    from app.identity import IdentityStore
    monkeypatch.setattr(
        main.app.state, "identity_store",
        IdentityStore(tmp_path / "identity-mode.sqlite3"), raising=False,
    )
    monkeypatch.setattr(main.service, "status", lambda: {"status": "ok"})
    monkeypatch.setattr(main.limits_monitor, "status", lambda: {"status": "ok"})

    client = TestClient(main.app)
    dashboard = client.get("/")
    assert dashboard.status_code == 401
    assert client.get("/api/odds").status_code == 401

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


def test_identity_mode_never_falls_back_to_configured_legacy_basic_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "legacy-secret")
    from app.identity import IdentityStore
    monkeypatch.setattr(
        main.app.state, "identity_store",
        IdentityStore(tmp_path / "identity-basic-fallback.sqlite3"), raising=False,
    )

    client = TestClient(main.app)
    response = client.get("/", auth=("operator", "legacy-secret"))
    assert response.status_code == 401



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



def test_identity_mode_product_api_requires_a_server_side_entitlement(monkeypatch, tmp_path):
    import pyotp
    from cryptography.fernet import Fernet
    from app.auth import SUBSCRIBER_COOKIE
    from app.identity import IdentityStore

    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(
        main.config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    store = IdentityStore(tmp_path / "entitlement-http.sqlite3")
    monkeypatch.setattr(main.app.state, "identity_store", store, raising=False)

    subscriber_id = store.create_subscriber(
        "product-user@example.test", "a strong product user password"
    )
    verification = store.create_email_verification_challenge("product-user@example.test")
    assert store.consume_email_verification_challenge(verification) is True
    subscriber_token, _ = store.create_session(subscriber_id)

    monkeypatch.setattr(main.service, "events", lambda *args, **kwargs: [])
    monkeypatch.setattr(main.service, "status", lambda: {})

    client = TestClient(main.app)
    client.cookies.set(SUBSCRIBER_COOKIE, subscriber_token)
    denied = client.get("/api/odds")
    assert denied.status_code == 403

    admin_id = store.provision_administrator(
        "product-admin@example.test", "a strong product admin password"
    )
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(uri)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    store.set_subscriber_entitlements(
        subscriber_id, ["odds"], actor_identity_id=admin_id
    )

    allowed = client.get("/api/odds")
    assert allowed.status_code == 200
    assert allowed.json()["events"] == []


def test_legacy_product_requests_do_not_initialize_identity_storage(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_MODE", "legacy_basic")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    monkeypatch.delattr(main.app.state, "identity_store", raising=False)
    client = TestClient(main.app)

    response = client.get("/api/odds")
    assert response.status_code == 200
    assert not hasattr(main.app.state, "identity_store")



def test_identity_mode_admin_refresh_requires_mfa_session_and_csrf(monkeypatch, tmp_path):
    import time
    import pyotp
    from cryptography.fernet import Fernet
    from app.auth import ADMINISTRATOR_COOKIE, CSRF_COOKIE, CSRF_HEADER
    from app.identity import IdentityStore

    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(
        main.config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    store = IdentityStore(tmp_path / "admin-refresh.sqlite3")
    monkeypatch.setattr(main.app.state, "identity_store", store, raising=False)
    monkeypatch.setattr(main.service, "refresh", lambda sport=None: {"refreshed": True})

    admin_id = store.provision_administrator(
        "refresh-admin@example.test", "a strong refresh admin password"
    )
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    store.verify_administrator_mfa(admin_id, totp.at(int(time.time()) + 30))
    admin_token, _ = store.create_session(admin_id)

    client = TestClient(main.app, base_url="https://testserver")
    no_session = client.post(
        "/api/refresh", headers={"origin": "https://testserver"}
    )
    assert no_session.status_code == 401

    client.cookies.set(ADMINISTRATOR_COOKIE, admin_token)
    no_csrf = client.post(
        "/api/refresh", headers={"origin": "https://testserver"}
    )
    assert no_csrf.status_code == 403

    client.cookies.set(CSRF_COOKIE, "refresh-csrf")
    accepted = client.post(
        "/api/refresh",
        headers={
            "origin": "https://testserver",
            CSRF_HEADER: "refresh-csrf",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"refreshed": True}



def test_identity_mode_framework_docs_require_administrator_session(monkeypatch, tmp_path):
    import time
    import pyotp
    from cryptography.fernet import Fernet
    from app.auth import ADMINISTRATOR_COOKIE
    from app.identity import IdentityStore

    monkeypatch.setattr(main.config, "AUTH_MODE", "identity")
    monkeypatch.setattr(
        main.config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    store = IdentityStore(tmp_path / "docs-admin.sqlite3")
    monkeypatch.setattr(main.app.state, "identity_store", store, raising=False)

    client = TestClient(main.app, base_url="https://testserver")
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/redoc").status_code == 401

    admin_id = store.provision_administrator(
        "docs-admin@example.test", "a strong docs admin password"
    )
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    store.verify_administrator_mfa(admin_id, totp.at(int(time.time()) + 30))
    token, _ = store.create_session(admin_id)
    client.cookies.set(ADMINISTRATOR_COOKIE, token)

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/odds" in schema.json()["paths"]
    assert "/auth/register" not in schema.json()["paths"]
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
