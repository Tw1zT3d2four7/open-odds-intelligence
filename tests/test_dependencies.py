from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    ADMINISTRATOR_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    SUBSCRIBER_COOKIE,
)
from app.identity import IdentityStore
from app.dependencies import (
    get_identity_store,
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

    return app


def test_subscriber_dependency_accepts_only_subscriber_cookie():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    response = client.get("/subscriber", cookies={SUBSCRIBER_COOKIE: "sub-token"})
    assert response.status_code == 200
    assert response.json() == {"account_type": "subscriber"}
    assert store.calls == ["sub-token"]


def test_administrator_dependency_accepts_only_administrator_cookie():
    store = StubStore({"admin-token": {"account_type": "administrator", "identity_id": "a1"}})
    client = TestClient(make_app(store))
    response = client.get("/administrator", cookies={ADMINISTRATOR_COOKIE: "admin-token"})
    assert response.status_code == 200
    assert response.json() == {"account_type": "administrator"}


def test_subscriber_cookie_cannot_authenticate_administrator_route():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    response = client.get("/administrator", cookies={SUBSCRIBER_COOKIE: "sub-token"})
    assert response.status_code == 401


def test_administrator_cookie_cannot_authenticate_subscriber_route():
    store = StubStore({"admin-token": {"account_type": "administrator", "identity_id": "a1"}})
    client = TestClient(make_app(store))
    response = client.get("/subscriber", cookies={ADMINISTRATOR_COOKIE: "admin-token"})
    assert response.status_code == 401


def test_missing_expired_or_revoked_session_is_rejected():
    store = StubStore({"expired-or-revoked": None})
    client = TestClient(make_app(store))
    assert client.get("/subscriber").status_code == 401
    assert client.get(
        "/subscriber", cookies={SUBSCRIBER_COOKIE: "expired-or-revoked"}
    ).status_code == 401


def test_store_initialization_failure_fails_closed_without_leaking_details(monkeypatch):
    import app.dependencies as dependencies

    app = make_app(StubStore())
    client = TestClient(app)
    app.state.identity_store = None

    def broken_store():
        raise RuntimeError("private database detail")

    monkeypatch.setattr(dependencies, "IdentityStore", broken_store)
    response = client.get("/subscriber", cookies={SUBSCRIBER_COOKIE: "token"})
    assert response.status_code == 503
    assert "private database detail" not in response.text


def test_mutating_subscriber_route_requires_csrf_origin_cookie_and_header():
    store = StubStore({"sub-token": {"account_type": "subscriber", "identity_id": "s1"}})
    client = TestClient(make_app(store))
    cookies = {
        SUBSCRIBER_COOKIE: "sub-token",
        CSRF_COOKIE: "csrf-value",
    }
    denied = client.post("/subscriber/change", cookies=cookies)
    assert denied.status_code == 403

    accepted = client.post(
        "/subscriber/change",
        cookies=cookies,
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
    response = client.get("/subscriber", cookies={SUBSCRIBER_COOKIE: "token"})
    assert response.status_code == 503
    assert "private database detail" not in response.text



def test_subscriber_dependency_uses_real_hashed_session_store(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("subscriber@example.test", "a-strong-test-password")
    token, _session_id = store.create_session(identity_id)
    client = TestClient(make_app(store))

    accepted = client.get("/subscriber", cookies={SUBSCRIBER_COOKIE: token})
    assert accepted.status_code == 200
    assert accepted.json() == {"account_type": "subscriber"}

    assert store.revoke_session(token) is True
    revoked = client.get("/subscriber", cookies={SUBSCRIBER_COOKIE: token})
    assert revoked.status_code == 401
