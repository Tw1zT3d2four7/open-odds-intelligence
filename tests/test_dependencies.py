from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    ADMINISTRATOR_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    SUBSCRIBER_COOKIE,
)
from app.identity import IdentityStore
def _verify_subscriber(store, email):
    challenge = store.create_email_verification_challenge(email)
    assert challenge
    assert store.consume_email_verification_challenge(challenge) is True


from app.dependencies import (
    get_identity_store,
    get_rate_limiter,
    require_subscriber,
    require_administrator,
    require_authenticated_csrf,
)


class StubStore:
    def __init__(self, sessions=None, fail=False):
        self.sessions = sessions or {}
        self.fail = fail
        self.calls = []

    def get_session(self, token):
        self.calls.append(token)
        if self.fail:
            raise RuntimeError("private database detail")
        return self.sessions.get(token)


def make_app(store):
    app = FastAPI()
    app.state.identity_store = store

    @app.get("/subscriber")
    def subscriber(session=Depends(require_subscriber)):
        return {"account_type": session["account_type"]}

    @app.get("/administrator")
    def administrator(session=Depends(require_administrator)):
        return {"account_type": session["account_type"]}

    @app.post("/subscriber/change")
    def subscriber_change(
        session=Depends(require_subscriber),
        _csrf=Depends(require_authenticated_csrf),
    ):
        return {"ok": True, "identity_id": session["identity_id"]}

    @app.post("/administrator/change")
    def administrator_change(
        session=Depends(require_administrator),
        _csrf=Depends(require_authenticated_csrf),
    ):
        return {"ok": True, "identity_id": session["identity_id"]}

    return app


def test_subscriber_dependency_accepts_only_subscriber_cookie():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    client.cookies.set(SUBSCRIBER_COOKIE, "sub-token")
    response = client.get("/subscriber")
    assert response.status_code == 200
    assert response.json() == {"account_type": "subscriber"}
    assert store.calls == ["sub-token"]


def test_administrator_dependency_accepts_only_administrator_cookie():
    store = StubStore({"admin-token": {"account_type": "administrator", "identity_id": "a1"}})
    client = TestClient(make_app(store))
    client.cookies.set(ADMINISTRATOR_COOKIE, "admin-token")
    response = client.get("/administrator")
    assert response.status_code == 200
    assert response.json() == {"account_type": "administrator"}


def test_subscriber_cookie_cannot_authenticate_administrator_route():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    client.cookies.set(SUBSCRIBER_COOKIE, "sub-token")
    response = client.get("/administrator")
    assert response.status_code == 401


def test_administrator_cookie_cannot_authenticate_subscriber_route():
    store = StubStore({"admin-token": {"account_type": "administrator", "identity_id": "a1"}})
    client = TestClient(make_app(store))
    client.cookies.set(ADMINISTRATOR_COOKIE, "admin-token")
    response = client.get("/subscriber")
    assert response.status_code == 401


def test_missing_expired_or_revoked_session_is_rejected():
    store = StubStore({"expired-or-revoked": None})
    client = TestClient(make_app(store))
    assert client.get("/subscriber").status_code == 401
    client.cookies.set(SUBSCRIBER_COOKIE, "expired-or-revoked")
    assert client.get("/subscriber").status_code == 401


def test_store_initialization_failure_fails_closed_without_leaking_details(monkeypatch):
    import app.dependencies as dependencies

    app = make_app(StubStore())
    client = TestClient(app)
    app.state.identity_store = None

    def broken_store():
        raise RuntimeError("private database detail")

    monkeypatch.setattr(dependencies, "IdentityStore", broken_store)
    client.cookies.set(SUBSCRIBER_COOKIE, "token")
    response = client.get("/subscriber")
    assert response.status_code == 503
    assert "private database detail" not in response.text


def test_mutating_subscriber_route_requires_csrf_origin_cookie_and_header():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    cookies = {
        SUBSCRIBER_COOKIE: "sub-token",
        CSRF_COOKIE: "csrf-value",
    }
    client.cookies.set(SUBSCRIBER_COOKIE, "sub-token")
    client.cookies.set(CSRF_COOKIE, "csrf-value")
    denied = client.post("/subscriber/change")
    assert denied.status_code == 403

    accepted = client.post(
        "/subscriber/change",
        headers={
            "origin": "https://testserver",
            CSRF_HEADER: "csrf-value",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True, "identity_id": "s1"}



def test_session_lookup_database_failure_fails_closed_without_leaking_details():
    store = StubStore(fail=True)
    client = TestClient(make_app(store))
    client.cookies.set(SUBSCRIBER_COOKIE, "token")
    response = client.get("/subscriber")
    assert response.status_code == 503
    assert "private database detail" not in response.text



def test_subscriber_dependency_uses_real_hashed_session_store(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("subscriber@example.test", "a-strong-test-password")
    _verify_subscriber(store, "subscriber@example.test")
    token, _session_id = store.create_session(identity_id)
    client = TestClient(make_app(store))

    client.cookies.set(SUBSCRIBER_COOKIE, token)
    accepted = client.get("/subscriber")
    assert accepted.status_code == 200
    assert accepted.json() == {"account_type": "subscriber"}

    assert store.revoke_session(token) is True
    revoked = client.get("/subscriber")
    assert revoked.status_code == 401


def test_rate_limiter_dependency_reuses_app_scoped_instance():
    limiter = object()
    app = FastAPI()
    app.state.rate_limiter = limiter

    @app.get("/limiter")
    def inspect(limiter_value=Depends(get_rate_limiter)):
        return {"same": limiter_value is limiter}

    response = TestClient(app).get("/limiter")
    assert response.status_code == 200
    assert response.json() == {"same": True}


def test_rate_limiter_dependency_fails_closed_without_hash_secret(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "RATE_LIMIT_HASH_KEY", "")
    monkeypatch.setattr(config, "ACCOUNTS_DATABASE_PATH", str(tmp_path / "accounts.sqlite3"))
    app = FastAPI()

    @app.get("/limiter")
    def inspect(limiter=Depends(get_rate_limiter)):
        return {"available": limiter is not None}

    response = TestClient(app).get("/limiter")
    assert response.status_code == 503
    assert "RATE_LIMIT_HASH_KEY" not in response.text



def test_rate_limiter_dependency_initializes_once_when_secret_is_configured(monkeypatch, tmp_path):
    from app import config

    database = tmp_path / "accounts.sqlite3"
    monkeypatch.setattr(config, "RATE_LIMIT_HASH_KEY", "configured-test-hash-key-with-at-least-32-bytes")
    monkeypatch.setattr(config, "ACCOUNTS_DATABASE_PATH", str(database))
    app = FastAPI()

    @app.get("/limiter")
    def inspect(limiter=Depends(get_rate_limiter)):
        limiter.consume("test:bucket", limit=2, window_seconds=60)
        return {"type": type(limiter).__name__}

    client = TestClient(app)
    assert client.get("/limiter").json() == {"type": "SQLiteRateLimiter"}
    first = app.state.rate_limiter
    assert client.get("/limiter").json() == {"type": "SQLiteRateLimiter"}
    assert app.state.rate_limiter is first
    assert database.exists()



def test_administrator_http_session_requires_mfa_proof_and_csrf(monkeypatch, tmp_path):
    from datetime import timedelta

    import pyotp
    from cryptography.fernet import Fernet

    from app import config, identity as identity_module

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator(
        "admin@example.test", "a-strong-administrator-password"
    )

    # Password authentication alone cannot establish an administrator session.
    import pytest
    with pytest.raises(ValueError, match="MFA verification"):
        store.create_session(admin_id)

    provisioning_uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(provisioning_uri)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())

    next_step = identity_module._now() + timedelta(seconds=30)
    monkeypatch.setattr(identity_module, "_now", lambda: next_step)
    assert store.verify_administrator_mfa(admin_id, totp.at(next_step)) is True
    token, _session_id = store.create_session(admin_id)

    client = TestClient(make_app(store))
    client.cookies.set(ADMINISTRATOR_COOKIE, token)
    accepted_read = client.get("/administrator")
    assert accepted_read.status_code == 200
    assert accepted_read.json() == {"account_type": "administrator"}

    client.cookies.set(CSRF_COOKIE, "admin-csrf-value")
    denied_mutation = client.post("/administrator/change")
    assert denied_mutation.status_code == 403

    accepted_mutation = client.post(
        "/administrator/change",
        headers={
            "origin": "https://testserver",
            CSRF_HEADER: "admin-csrf-value",
        },
    )
    assert accepted_mutation.status_code == 200
    assert accepted_mutation.json() == {"ok": True, "identity_id": admin_id}

    # A subscriber session can never satisfy the administrator dependency.
    subscriber_id = store.create_subscriber(
        "subscriber@example.test", "a-strong-subscriber-password"
    )
    _verify_subscriber(store, "subscriber@example.test")
    subscriber_token, _ = store.create_session(subscriber_id)
    client.cookies.clear()
    client.cookies.set(ADMINISTRATOR_COOKIE, subscriber_token)
    denied_cross_class = client.get("/administrator")
    assert denied_cross_class.status_code == 401


def test_inactive_identity_is_denied_at_http_dependency_boundary(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "inactive@example.test", "a-strong-inactive-password"
    )
    _verify_subscriber(store, "inactive@example.test")
    token, _session_id = store.create_session(identity_id)
    with store.connect() as db:
        db.execute("UPDATE identities SET is_active=0 WHERE id=?", (identity_id,))

    client = TestClient(make_app(store))
    client.cookies.set(SUBSCRIBER_COOKIE, token)
    response = client.get("/subscriber")
    assert response.status_code == 401



def test_absolute_expiry_is_enforced_at_http_dependency_boundary(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "expired@example.test", "a-strong-expired-password"
    )
    _verify_subscriber(store, "expired@example.test")
    token, session_id = store.create_session(identity_id)
    with store.connect() as db:
        db.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (session_id,),
        )

    client = TestClient(make_app(store))
    client.cookies.set(SUBSCRIBER_COOKIE, token)
    response = client.get("/subscriber")
    assert response.status_code == 401
