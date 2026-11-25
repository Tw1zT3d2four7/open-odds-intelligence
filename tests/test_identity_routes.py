import time

import pytest
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.dependencies import get_identity_store, get_rate_limiter
from app.email_delivery import EmailDeliveryError
from app.identity import IdentityStore
from app.email_queue import TransactionalEmailQueue
from app.rate_limit import SQLiteRateLimiter
from app.identity_routes import get_email_sender, router


class FakeSender:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, recipient, subject, *, text, html=None, idempotency_key=None):
        if self.fail:
            raise EmailDeliveryError("provider details must not leak")
        self.messages.append({
            "recipient": recipient, "subject": subject, "text": text,
            "html": html, "idempotency_key": idempotency_key,
        })
        return "email_test_id"


class IdentityRouteHarness:
    def __init__(self, tmp_path, monkeypatch):
        self.store = IdentityStore(tmp_path / "accounts.sqlite3")
        self.limiter = SQLiteRateLimiter(
            tmp_path / "accounts.sqlite3",
            hash_key="identity-route-test-hash-key-with-at-least-32-bytes",
        )
        self.sender = FakeSender()
        monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ooi.example")
        monkeypatch.setattr(config, "EMAIL_QUEUE_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
        app = FastAPI()
        app.include_router(router)
        app.state.email_sender = self.sender
        app.dependency_overrides[get_identity_store] = lambda: self.store
        app.dependency_overrides[get_rate_limiter] = lambda: self.limiter
        self.client = TestClient(app, base_url="https://ooi.example")
        self._csrf()

    def deliver_pending(self):
        queue = TransactionalEmailQueue(self.store)
        delivered = 0
        while queue.process_one(self.sender):
            delivered += 1
        return delivered

    def _csrf(self):
        response = self.client.get("/auth/csrf")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def post(self, path, payload, *, csrf=True):
        headers = {"origin": "https://ooi.example"}
        if csrf:
            token = self.client.cookies.get("__Host-ooi_csrf")
            headers["x-ooi-csrf-token"] = token
        return self.client.post(path, json=payload, headers=headers)


def test_registration_creates_unverified_subscriber_and_sends_one_time_link(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    response = harness.post("/auth/register", {
        "email": "Person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 202
    assert "verification email" in response.json()["message"]
    assert harness.sender.messages == []
    assert harness.deliver_pending() == 1
    assert len(harness.sender.messages) == 1
    message = harness.sender.messages[0]
    assert message["recipient"] == "Person@example.com"
    assert "#token=" in message["text"]
    assert "Person@example.com" not in message["idempotency_key"]
    with harness.store.connect() as db:
        row = db.execute(
            "SELECT account_type, email_verified_at FROM identities"
        ).fetchone()
        delivery = db.execute(
            "SELECT status, provider_message_id FROM email_delivery_events"
        ).fetchone()
    assert row["account_type"] == "subscriber"
    assert row["email_verified_at"] is None
    assert delivery["status"] == "sent"
    assert delivery["provider_message_id"] == "email_test_id"


def test_duplicate_registration_returns_same_generic_response_without_resending(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.store.create_subscriber("person@example.com", "a strong password 123")
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "another strong password 456",
    })
    assert response.status_code == 202
    assert "If this address can be registered" in response.json()["message"]
    assert harness.sender.messages == []


def test_verification_link_is_consumed_and_subscriber_can_login(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.store.create_subscriber("person@example.com", "a strong password 123")
    challenge = harness.store.create_email_verification_challenge("person@example.com")
    response = harness.post("/auth/verify-email", {"token": challenge})
    assert response.status_code == 200
    login = harness.post("/auth/login", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert login.status_code == 200
    assert login.json()["ok"] is True
    assert "__Host-ooi_subscriber" in login.headers.get("set-cookie", "")


def test_unverified_subscriber_cannot_login(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.store.create_subscriber("person@example.com", "a strong password 123")
    response = harness.post("/auth/login", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 401


def test_password_reset_request_is_generic_and_reset_revokes_sessions(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber("person@example.com", "a strong password 123")
    challenge = harness.store.create_email_verification_challenge("person@example.com")
    harness.store.consume_email_verification_challenge(challenge)
    token, _ = harness.store.create_session(identity_id)

    requested = harness.post("/auth/password-reset/request", {"email": "person@example.com"})
    assert requested.status_code == 202
    assert "If an active account" in requested.json()["message"]
    assert harness.deliver_pending() == 1
    reset_message = harness.sender.messages[-1]
    reset_token = reset_message["text"].split("#token=", 1)[1].split()[0]
    consumed = harness.post("/auth/password-reset/consume", {
        "token": reset_token,
        "new_password": "an entirely new strong password 789",
    })
    assert consumed.status_code == 200
    assert harness.store.get_session(token) is None


def test_registration_delivery_failure_does_not_leak_provider_error(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.sender.fail = True
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 202
    assert "provider details" not in response.text
    assert harness.deliver_pending() == 1
    with harness.store.connect() as db:
        delivery = db.execute(
            "SELECT status, failure_code FROM email_delivery_events"
        ).fetchone()
    assert delivery["status"] == "failed"
    assert delivery["failure_code"] == "unexpected"


def test_auth_mutations_require_origin_and_csrf_proof(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "a strong password 123",
    }, csrf=False)
    assert response.status_code == 403


def test_admin_login_requires_password_and_totp_before_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.provision_administrator(
        "admin@example.com", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(identity_id)
    secret = pyotp.parse_uri(uri).secret
    totp = pyotp.TOTP(secret)
    harness.store.confirm_administrator_mfa_enrollment(identity_id, totp.now())
    code = totp.at(int(time.time()) + 30)
    response = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
        "totp_code": code,
    })
    assert response.status_code == 200
    assert "__Host-ooi_admin" in response.headers.get("set-cookie", "")
    assert "__Host-ooi_subscriber" not in response.headers.get("set-cookie", "")


def test_admin_login_rejects_missing_or_invalid_mfa(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.provision_administrator(
        "admin@example.com", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(identity_id)
    secret = pyotp.parse_uri(uri).secret
    harness.store.confirm_administrator_mfa_enrollment(identity_id, pyotp.TOTP(secret).now())

    missing = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
    })
    assert missing.status_code == 401
    invalid = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
        "totp_code": "000000",
    })
    assert invalid.status_code == 401


def test_admin_recovery_code_can_establish_mfa_session_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.provision_administrator(
        "admin@example.com", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(identity_id)
    secret = pyotp.parse_uri(uri).secret
    recovery_codes = harness.store.confirm_administrator_mfa_enrollment(
        identity_id, pyotp.TOTP(secret).now()
    )
    response = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
        "recovery_code": recovery_codes[0],
    })
    assert response.status_code == 200
    second = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
        "recovery_code": recovery_codes[0],
    })
    assert second.status_code == 401



def test_email_origin_must_be_configured_before_registration_creates_account(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 503
    with harness.store.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
    assert count == 0


def test_verification_resend_has_persistent_cooldown(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.store.create_subscriber("person@example.com", "a strong password 123")
    first = harness.post("/auth/verification/resend", {"email": "person@example.com"})
    assert first.status_code == 202
    assert harness.deliver_pending() == 1
    second = harness.post("/auth/verification/resend", {"email": "person@example.com"})
    assert second.status_code == 429
    assert len(harness.sender.messages) == 1


def test_password_reset_request_is_account_enumeration_resistant(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    unknown = harness.post("/auth/password-reset/request", {"email": "unknown@example.com"})
    harness.store.create_subscriber("known@example.com", "a strong password 123")
    known = harness.post("/auth/password-reset/request", {"email": "known@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert harness.deliver_pending() == 1
    assert len(harness.sender.messages) == 1



def test_successful_subscriber_login_revokes_previous_session(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber("person@example.com", "a strong password 123")
    verification = harness.store.create_email_verification_challenge("person@example.com")
    harness.store.consume_email_verification_challenge(verification)
    previous_token, _ = harness.store.create_session(identity_id)
    harness.client.cookies.set(
        "__Host-ooi_subscriber", previous_token,
        domain="ooi.example", path="/",
    )

    response = harness.post("/auth/login", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 200
    assert harness.store.get_session(previous_token) is None
    assert harness.client.cookies.get("__Host-ooi_subscriber") != previous_token


def test_subscriber_logout_revokes_session_and_clears_cookies(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber("person@example.com", "a strong password 123")
    verification = harness.store.create_email_verification_challenge("person@example.com")
    harness.store.consume_email_verification_challenge(verification)
    response = harness.post("/auth/login", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 200
    token = harness.client.cookies.get("__Host-ooi_subscriber")
    assert token and harness.store.get_session(token)

    logout = harness.post("/auth/logout", {})
    assert logout.status_code == 200
    assert harness.store.get_session(token) is None
    assert harness.client.cookies.get("__Host-ooi_subscriber") is None



def test_administrator_logout_revokes_mfa_authenticated_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.provision_administrator(
        "admin@example.com", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(identity_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(identity_id, totp.now())
    code = totp.at(int(time.time()) + 30)

    login = harness.post("/admin/auth/login", {
        "email": "admin@example.com",
        "password": "an administrator password 123",
        "totp_code": code,
    })
    assert login.status_code == 200
    token = harness.client.cookies.get("__Host-ooi_admin")
    assert token and harness.store.get_session(token)

    logout = harness.post("/admin/auth/logout", {})
    assert logout.status_code == 200
    assert harness.store.get_session(token) is None
    assert harness.client.cookies.get("__Host-ooi_admin") is None


def test_subscriber_session_cookie_cannot_authorize_administrator_logout(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber("person@example.com", "a strong password 123")
    verification = harness.store.create_email_verification_challenge("person@example.com")
    harness.store.consume_email_verification_challenge(verification)
    token, _ = harness.store.create_session(identity_id)
    harness.client.cookies.set("__Host-ooi_admin", token, domain="ooi.example", path="/")

    response = harness.post("/admin/auth/logout", {})
    assert response.status_code == 401
    assert harness.store.get_session(token) is not None



def test_registration_storage_failure_returns_sanitized_service_error(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)

    def fail_create(*_args, **_kwargs):
        raise RuntimeError("private identity database path")

    monkeypatch.setattr(harness.store, "create_subscriber", fail_create)
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 503
    assert "private identity database path" not in response.text



def test_open_email_circuit_skips_provider_and_records_delivery_state(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    seed_identity = harness.store.create_subscriber(
        "seed@example.com", "a strong password 123"
    )
    for _ in range(5):
        harness.store.record_email_delivery_event(
            seed_identity, "verification", "failed",
            failure_code="provider_unavailable",
        )

    response = harness.post("/auth/register", {
        "email": "new-person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 202
    assert harness.sender.messages == []
    assert harness.deliver_pending() == 1
    with harness.store.connect() as db:
        queued = db.execute(
            "SELECT state, attempt_count, last_failure_code "
            "FROM transactional_email_queue ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert queued["state"] == "pending"
    assert queued["attempt_count"] == 0
    assert queued["last_failure_code"] == "circuit_open"



@pytest.mark.parametrize(
    "path",
    [
        "/login", "/register", "/admin/login",
        "/forgot-password", "/verify-email", "/reset-password",
    ],
)
def test_auth_entry_pages_are_no_store_and_share_the_secure_auth_shell(
    tmp_path, monkeypatch, path
):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    response = harness.client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "Open Odds Intelligence" in response.text
    assert "__Host-ooi_csrf" in response.text
