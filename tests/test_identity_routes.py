import time

import pytest
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.dependencies import get_identity_store, get_odds_service, get_rate_limiter
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
        "email": "  Person@Example.com  ",
        "password": "a strong password 123",
    })
    assert response.status_code == 202
    assert "verification email" in response.json()["message"]
    assert harness.sender.messages == []
    assert harness.deliver_pending() == 1
    assert len(harness.sender.messages) == 1
    message = harness.sender.messages[0]
    assert message["recipient"] == "person@example.com"
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
    assert login.headers["Cache-Control"] == "no-store"
    assert login.headers["Pragma"] == "no-cache"
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
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
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
    assert logout.headers["Cache-Control"] == "no-store"
    assert logout.headers["Pragma"] == "no-cache"
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
    assert logout.headers["Cache-Control"] == "no-store"
    assert logout.headers["Pragma"] == "no-cache"
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



def test_auth_page_uses_a_fresh_csp_script_nonce_for_each_response(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    first = harness.client.get("/login")
    second = harness.client.get("/login")

    first_nonce = first.text.split('<script nonce="', 1)[1].split('"', 1)[0]
    second_nonce = second.text.split('<script nonce="', 1)[1].split('"', 1)[0]
    assert first_nonce != second_nonce
    assert f"script-src 'nonce-{first_nonce}'" in first.headers["content-security-policy"]
    assert f"script-src 'nonce-{second_nonce}'" in second.headers["content-security-policy"]


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
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    nonce = response.text.split('<script nonce="', 1)[1].split('"', 1)[0]
    assert f"script-src 'nonce-{nonce}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert '<script nonce="' in response.text
    assert "innerHTML" not in response.text
    assert "Open Odds Intelligence" in response.text
    assert "__Host-ooi_csrf" in response.text



@pytest.mark.parametrize(
    "origin",
    [
        "http://ooi.example",
        "https://ooi.example/account",
        "https://ooi.example?next=/",
        "https://ooi.example:invalid",
    ],
)
def test_auth_email_origin_rejects_noncanonical_base_urls(
    tmp_path, monkeypatch, origin
):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", origin)
    response = harness.post("/auth/register", {
        "email": "person@example.com",
        "password": "a strong password 123",
    })
    assert response.status_code == 503
    with harness.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 0



def test_complimentary_subscriber_admin_flow_requires_mfa_csrf_and_activation(tmp_path, monkeypatch):
    from urllib.parse import unquote

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    admin_id = harness.store.provision_administrator(
        "promo-admin@example.test", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())

    payload = {
        "email": "giveaway@example.test",
        "entitlements": ["dashboard", "odds"],
        "duration_days": 30,
        "campaign": "November giveaway",
        "reason_code": "giveaway_winner",
    }
    denied = harness.post("/admin/subscribers/complimentary", payload)
    assert denied.status_code == 401

    login = harness.post("/admin/auth/login", {
        "email": "promo-admin@example.test",
        "password": "an administrator password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200
    missing_csrf = harness.client.post(
        "/admin/subscribers/complimentary", json=payload,
        headers={"origin": "https://ooi.example"},
    )
    assert missing_csrf.status_code == 403

    created = harness.post("/admin/subscribers/complimentary", payload)
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["billing_status"] == "complimentary"
    assert body["activation_email_queued"] is True
    assert body["entitlements"] == ["dashboard", "odds"]
    assert "activation_token" not in created.text
    assert harness.deliver_pending() == 1
    message = harness.sender.messages[0]
    assert message["recipient"] == "giveaway@example.test"
    assert "reset-password?mode=activation#token=" in message["text"]
    token = unquote(message["text"].rsplit("#token=", 1)[1].split()[0])
    activated = harness.post("/auth/password-reset/consume", {
        "token": token,
        "new_password": "giveaway winner secure password",
    })
    assert activated.status_code == 200
    assert harness.store.authenticate(
        "giveaway@example.test", "giveaway winner secure password", "subscriber"
    ) == body["identity_id"]
    csrf = harness.client.cookies.get("__Host-ooi_csrf")
    extended = harness.client.put(
        "/admin/subscribers/complimentary",
        json={
            "identity_id": body["identity_id"],
            "entitlements": ["dashboard"],
            "additional_days": 14,
            "campaign": "November giveaway extension",
            "reason_code": "giveaway_winner",
        },
        headers={"origin": "https://ooi.example", "x-ooi-csrf-token": csrf},
    )
    assert extended.status_code == 200
    assert extended.headers["cache-control"] == "no-store"
    assert extended.json()["entitlements"] == ["dashboard"]
    assert extended.json()["expires_at"] > body["expires_at"]
    promo_directory = harness.client.get("/admin/subscribers", params={"complimentary_only": "true"})
    assert promo_directory.status_code == 200
    assert promo_directory.json()["total"] == 1
    nonpromo_directory = harness.client.get("/admin/subscribers", params={"complimentary_only": "false"})
    assert nonpromo_directory.status_code == 200
    assert nonpromo_directory.json()["total"] == 0
    with harness.store.connect() as db:
        audit = db.execute(
            "SELECT actor_identity_id, event_type FROM security_audit_events WHERE subject_identity_id=? AND event_type='subscriber.complimentary_access_granted'",
            (body["identity_id"],),
        ).fetchone()
    assert audit["actor_identity_id"] == admin_id


def test_admin_entitlement_api_requires_mfa_session_and_csrf(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "subscriber-entitlement@example.test", "a strong subscriber password"
    )
    admin_id = harness.store.provision_administrator(
        "entitlement-admin@example.test", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())

    denied = harness.client.get(
        "/admin/subscriber-entitlements",
        params={"identity_id": subscriber_id},
    )
    assert denied.status_code == 401

    login = harness.post("/admin/auth/login", {
        "email": "entitlement-admin@example.test",
        "password": "an administrator password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    payload = {
        "identity_id": subscriber_id, "entitlements": ["dashboard", "odds"],
        "reason_code": "support_request",
    }
    missing_csrf = harness.client.put("/admin/subscriber-entitlements", json=payload)
    assert missing_csrf.status_code == 403

    csrf = harness.client.cookies.get("__Host-ooi_csrf")
    updated = harness.client.put(
        "/admin/subscriber-entitlements",
        json=payload,
        headers={
            "origin": "https://ooi.example",
            "x-ooi-csrf-token": csrf,
        },
    )
    assert updated.status_code == 200
    assert updated.headers["cache-control"] == "no-store"
    assert updated.json() == {
        "identity_id": subscriber_id,
        "entitlements": ["dashboard", "odds"],
    }

    invalid_reason = harness.client.put(
        "/admin/subscriber-entitlements",
        json={**payload, "reason_code": "unapproved"},
        headers={
            "origin": "https://ooi.example",
            "x-ooi-csrf-token": csrf,
        },
    )
    assert invalid_reason.status_code == 422

    readback = harness.client.get(
        "/admin/subscriber-entitlements",
        params={"identity_id": subscriber_id},
    )
    assert readback.status_code == 200
    assert readback.headers["cache-control"] == "no-store"
    assert readback.json()["entitlements"] == ["dashboard", "odds"]
    assert "subscriber-entitlement@example.test" not in readback.text



def test_admin_console_is_private_no_store_and_separate_from_subscriber_access(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)

    # No session and a valid subscriber session must both be denied.
    assert harness.client.get("/admin").status_code == 401
    subscriber_id = harness.store.create_subscriber(
        "console-subscriber@example.test", "a strong subscriber password"
    )
    verification = harness.store.create_email_verification_challenge(
        "console-subscriber@example.test"
    )
    assert harness.store.consume_email_verification_challenge(verification)
    subscriber_token, _ = harness.store.create_session(subscriber_id)
    harness.client.cookies.set(
        "__Host-ooi_subscriber", subscriber_token, domain="ooi.example", path="/"
    )
    assert harness.client.get("/admin").status_code == 401
    harness.client.cookies.delete("__Host-ooi_subscriber", domain="ooi.example", path="/")

    # Only a password + fresh TOTP administrator session opens the console.
    admin_id = harness.store.provision_administrator(
        "console-admin@example.test", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "console-admin@example.test",
        "password": "an administrator password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    response = harness.client.get("/admin")
    assert response.status_code == 200
    assert "Open Odds Intelligence Admin" in response.text
    assert "System Configuration" in response.text
    assert 'id="entitlement-reason"' in response.text
    assert "reason_code:reasonCode" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    nonce = response.text.split('<script nonce="', 1)[1].split('"', 1)[0]
    assert f"script-src 'nonce-{nonce}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert '<script nonce="' in response.text
    assert "frame-ancestors 'none'" in csp



def test_admin_audit_api_is_mfa_protected_paginated_and_non_cacheable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "audit-private@example.test", "a strong subscriber password"
    )
    admin_id = harness.store.provision_administrator(
        "audit-admin@example.test", "an administrator password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())

    denied = harness.client.get("/admin/audit-events")
    assert denied.status_code == 401

    login = harness.post("/admin/auth/login", {
        "email": "audit-admin@example.test",
        "password": "an administrator password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    response = harness.client.get("/admin/audit-events", params={"limit": 2})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["limit"] == 2
    assert payload["total"] >= 2
    assert len(payload["events"]) == 2
    assert all("event_type" in event and "occurred_at" in event for event in payload["events"])
    assert "audit-private@example.test" not in response.text
    assert "audit-admin@example.test" not in response.text

    filtered = harness.client.get(
        "/admin/audit-events",
        params={"event_type": "administrator.provisioned"},
    )
    assert filtered.status_code == 200
    assert all(event["event_type"] == "administrator.provisioned"
               for event in filtered.json()["events"])
    assert harness.client.get("/admin/audit-events", params={"limit": 0}).status_code == 422
    assert subscriber_id


def test_admin_subscriber_directory_requires_mfa_and_is_paginated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "directory-user@example.test", "a strong directory password 123"
    )
    admin_id = harness.store.provision_administrator(
        "directory-admin@example.test", "a strong directory admin password 123"
    )
    assert harness.client.get("/admin/subscribers").status_code == 401

    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "directory-admin@example.test",
        "password": "a strong directory admin password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    response = harness.client.get("/admin/subscribers", params={"limit": 1})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["subscribers"]) == 1
    subscriber = payload["subscribers"][0]
    assert subscriber["identity_id"] == subscriber_id
    assert subscriber["email"] == "directory-user@example.test"
    assert subscriber["email_verified"] is False
    assert "password_hash" not in subscriber
    assert "session" not in subscriber

    filtered = harness.client.get(
        "/admin/subscribers", params={"email_query": "directory-user"}
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert harness.client.get("/admin/subscribers", params={"limit": 0}).status_code == 422



def test_admin_billing_overview_is_mfa_protected_aggregate_only_and_no_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "billing-ops-user@example.test", "a strong billing ops password"
    )
    admin_id = harness.store.provision_administrator(
        "billing-ops-admin@example.test", "a strong billing ops admin password"
    )
    assert harness.client.get("/admin/billing").status_code == 401

    now = "2026-11-25T00:00:00+00:00"
    with harness.store.connect() as db:
        db.execute(
            """INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at)
               VALUES (?, 'cus_ops_private', ?, ?)""", (subscriber_id, now, now)
        )
        db.execute(
            """INSERT INTO billing_subscriptions(
                id, identity_id, stripe_subscription_id, stripe_customer_id,
                plan_code, billing_interval, status, cancel_at_period_end,
                created_at, updated_at, last_event_created_at
            ) VALUES ('ops-sub', ?, 'sub_ops_private', 'cus_ops_private',
                      'basic', 'month', 'past_due', 0, ?, ?, ?)""",
            (subscriber_id, now, now, now)
        )
        db.execute(
            """INSERT INTO billing_invoices(
                id, identity_id, stripe_invoice_id, stripe_customer_id,
                stripe_subscription_id, status, amount_due, amount_paid, currency,
                payment_failed_at, last_event_created_at, created_at, updated_at
            ) VALUES ('ops-invoice', ?, 'in_ops_private', 'cus_ops_private',
                      'sub_ops_private', 'open', 2500, 0, 'usd', ?, ?, ?, ?)""",
            (subscriber_id, now, now, now, now)
        )
        db.execute(
            """INSERT INTO billing_webhook_events(
                stripe_event_id, event_type, livemode, payload_sha256,
                processing_state, attempt_count, received_at, updated_at
            ) VALUES ('evt_ops_private', 'invoice.payment_failed', 0, ?, 'failed', 1, ?, ?)""",
            ("a" * 64, now, now)
        )

    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "billing-ops-admin@example.test",
        "password": "a strong billing ops admin password",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    response = harness.client.get("/admin/billing")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    data = response.json()
    assert data["customer_count"] == 1
    assert data["subscription_count"] == 1
    assert data["invoice_count"] == 1
    assert data["failed_webhook_count"] == 1
    assert data["subscriptions_by_status_and_interval"] == [
        {"status": "past_due", "billing_interval": "month", "count": 1}
    ]
    assert data["invoices_by_status"] == [
        {"status": "open", "count": 1, "payment_failures": 1}
    ]
    assert data["checkout_available"] is False
    assert data["customer_portal_available"] is False
    assert data["live_billing_enabled"] is False

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_AdminReadinessCase")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", False)
    monkeypatch.setattr(config, "STRIPE_TEST_MODE_ACKNOWLEDGED", True)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_testAdminReadinessSecret123")
    monkeypatch.setattr(
        config, "STRIPE_WEBHOOK_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(config, "STRIPE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(config, "STRIPE_CUSTOMER_PORTAL_ENABLED", True)
    monkeypatch.setattr(config, "STRIPE_PRICE_MONTHLY_ID", "price_MonthlyReadiness")
    monkeypatch.setattr(config, "STRIPE_PRICE_YEARLY_ID", "price_YearlyReadiness")
    enabled = harness.client.get("/admin/billing")
    assert enabled.status_code == 200
    assert enabled.json()["checkout_available"] is True
    assert enabled.json()["customer_portal_available"] is True
    assert enabled.json()["live_billing_enabled"] is False

    for private_value in ("billing-ops-user@example.test", "cus_ops_private",
                          "sub_ops_private", "in_ops_private", "evt_ops_private",
                          "encrypted_payload", "lease_token"):
        assert private_value not in response.text


def test_admin_subscriber_status_api_requires_mfa_csrf_and_records_actor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "status-user@example.test", "a strong status user password 123"
    )
    admin_id = harness.store.provision_administrator(
        "status-admin@example.test", "a strong status admin password 123"
    )
    path = f"/admin/subscribers/{subscriber_id}/status"
    assert harness.post(path, {
        "is_active": False, "reason_code": "support_request"
    }).status_code == 401

    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "status-admin@example.test",
        "password": "a strong status admin password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    denied = harness.post(path, {
        "is_active": False, "reason_code": "support_request"
    }, csrf=False)
    assert denied.status_code == 403
    changed = harness.post(path, {
        "is_active": False, "reason_code": "support_request"
    })
    assert changed.status_code == 200
    assert changed.headers["cache-control"] == "no-store"
    assert changed.json()["is_active"] is False
    assert harness.client.get("/admin/subscribers").json()["subscribers"][0]["is_active"] is False

    invalid = harness.post(path, {"is_active": True, "reason_code": "bad_code"})
    assert invalid.status_code == 422
    reactivated = harness.post(path, {
        "is_active": True, "reason_code": "support_request"
    })
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, event_type FROM security_audit_events
               WHERE subject_identity_id=? AND event_type='subscriber.reactivated'""",
            (subscriber_id,),
        ).fetchone()
    assert event["actor_identity_id"] == admin_id



def test_admin_overview_is_mfa_protected_no_store_and_secret_minimized(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)

    class FakeOddsService:
        def status(self):
            return {
                "providers": ["the_odds_api"],
                "configured": True,
                "data_freshness": "current",
                "last_updated": "2026-11-25T12:00:00+00:00",
                "stored_snapshots": 12,
                "stored_events": 48,
                "stored_bookmakers": 9,
                "sport_freshness": [{
                    "sport": "basketball_nba", "freshness": "current",
                    "last_updated": "2026-11-25T12:00:00+00:00",
                    "age_seconds": 120, "event_count": 48,
                }],
                "alerts_enabled": True,
                "alerts_sent": 7,
                "last_alert_at": "2026-11-25T11:59:00+00:00",
                "last_alert_check": "2026-11-25T12:00:00+00:00",
                "last_error": "provider error containing secret-token-value",
                "last_alert_error": None,
                "provider_keys": {"the_odds_api": {"raw_key": "must-not-leak", "account_groups": [{
                    "account_group": "premium-account", "configured_keys": 2,
                    "available_keys": 1, "quota_cooldown_keys": 1, "disabled_keys": 0,
                    "usage": {
                        "credits_remaining": 42,
                        "quota_floor_triggered": "credits_remaining",
                        "quota_floor_threshold": 50,
                        "usage_observed_at": "2026-11-25T12:00:00Z",
                        "api_key": "must-not-leak",
                    },
                }]}},
                "provider_account_access_classes": {
                    "the_odds_api": {"premium-account": "premium"}
                },
                "provider_account_quota_floors": {
                    "the_odds_api": {"premium-account": {"credits_remaining": 100}}
                },
                "provider_access_classes": {"the_odds_api": "free"},
                "premium_provider_polling_enabled": False,
                "provider_usage": {"raw": "must-not-leak"},
            }

    harness.client.app.dependency_overrides[get_odds_service] = lambda: FakeOddsService()
    assert harness.client.get("/admin/overview").status_code == 401
    admin_id = harness.store.provision_administrator(
        "overview-admin@example.test", "a strong overview admin password 123"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "overview-admin@example.test",
        "password": "a strong overview admin password 123",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200

    response = harness.client.get("/admin/overview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["stored_events"] == 48
    assert payload["sports"][0]["sport"] == "basketball_nba"
    assert payload["alerts"]["sent"] == 7
    assert payload["provider_error_present"] is True
    assert payload["provider_accounts"] == [{
        "provider": "the_odds_api",
        "account_group": "premium-account",
        "access_class": "premium",
        "configured_keys": 2,
        "available_keys": 1,
        "quota_cooldown_keys": 1,
        "disabled_keys": 0,
        "status": "available",
        "usage": {
            "credits_remaining": 42,
            "quota_floor_triggered": "credits_remaining",
            "quota_floor_threshold": 50,
            "usage_observed_at": "2026-11-25T12:00:00Z",
        },
        "quota_floors": {"credits_remaining": 100},
    }]
    assert payload["premium_provider_polling_enabled"] is False
    assert "provider_keys" not in payload
    assert "provider_usage" not in payload
    assert "secret-token-value" not in response.text
    assert "must-not-leak" not in response.text


def test_admin_session_api_requires_mfa_csrf_and_protects_current_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "session-api-user@example.test", "a strong session api password"
    )
    challenge = harness.store.create_email_verification_challenge("session-api-user@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    subscriber_token, subscriber_session_id = harness.store.create_session(subscriber_id)
    assert harness.client.get("/admin/sessions").status_code == 401

    admin_id = harness.store.provision_administrator(
        "session-api-admin@example.test", "a strong session api admin password"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "session-api-admin@example.test",
        "password": "a strong session api admin password",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200
    admin_token = harness.client.cookies.get("__Host-ooi_admin")
    current_session = harness.store.get_session(admin_token)
    assert current_session and current_session["session_id"]

    listing = harness.client.get("/admin/sessions")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    data = listing.json()
    target = next(row for row in data["sessions"] if row["session_id"] == subscriber_session_id)
    assert target["identity_id"] == subscriber_id
    assert "token_hash" not in listing.text
    assert "session-api-user@example.test" not in listing.text

    path = f"/admin/sessions/{subscriber_session_id}/revoke"
    assert harness.post(path, {"reason_code": "security_review"}, csrf=False).status_code == 403
    revoked = harness.post(path, {"reason_code": "security_review"})
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True}
    assert harness.store.get_session(subscriber_token) is None
    assert harness.post(
        f"/admin/sessions/{current_session['session_id']}/revoke",
        {"reason_code": "security_review"},
    ).status_code == 422
    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id FROM security_audit_events
               WHERE event_type='administrator.session_revoked'
               AND subject_identity_id=?""", (subscriber_id,)
        ).fetchone()
    assert event["actor_identity_id"] == admin_id


def test_admin_bulk_subscriber_session_revocation_requires_csrf_and_is_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    subscriber_id = harness.store.create_subscriber(
        "bulk-session-user@example.test", "a strong bulk session user password"
    )
    challenge = harness.store.create_email_verification_challenge("bulk-session-user@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    tokens = [harness.store.create_session(subscriber_id) for _ in range(2)]
    admin_id = harness.store.provision_administrator(
        "bulk-session-admin@example.test", "a strong bulk session admin password"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "bulk-session-admin@example.test",
        "password": "a strong bulk session admin password",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200
    path = f"/admin/subscribers/{subscriber_id}/sessions/revoke"
    assert harness.post(path, {"reason_code": "security_review"}, csrf=False).status_code == 403
    response = harness.post(path, {"reason_code": "security_review"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"identity_id": subscriber_id, "revoked_sessions": 2}
    assert all(harness.store.get_session(token) is None for token, _sid in tokens)
    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, metadata_json FROM security_audit_events
               WHERE event_type='administrator.subscriber_sessions_revoked'
               AND subject_identity_id=?""", (subscriber_id,)
        ).fetchone()
    assert event["actor_identity_id"] == admin_id
    assert '"revoked_count":2' in event["metadata_json"]
    assert "bulk-session-user@example.test" not in event["metadata_json"]


def test_subscriber_account_page_and_summary_are_private_and_minimal(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    unauthorized_page = harness.client.get("/account")
    unauthorized_summary = harness.client.get("/auth/account")
    assert unauthorized_page.status_code == 401
    assert unauthorized_summary.status_code == 401

    harness.store.create_subscriber("account-user@example.test", "a strong account password")
    challenge = harness.store.create_email_verification_challenge("account-user@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "account-user@example.test",
        "password": "a strong account password",
    })
    assert login.status_code == 200

    page = harness.client.get("/account")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["pragma"] == "no-cache"
    nonce = page.headers["content-security-policy"].split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">' in page.text
    assert "innerHTML" not in page.text
    second_page = harness.client.get("/account")
    second_nonce = second_page.headers["content-security-policy"].split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    assert second_nonce != nonce

    response = harness.client.get("/auth/account")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == {
        "email": "account-user@example.test",
        "account_status": "active",
        "email_verified": True,
        "feature_access": [
            "arbitrage", "clv", "dashboard", "history", "market_board",
            "market_limits", "market_movement", "odds", "opportunities",
            "player_props", "simulation",
        ],
    }
    assert "__Host-ooi_subscriber" not in response.text
    assert "token_hash" not in response.text


def test_administrator_session_cannot_access_subscriber_account(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    admin_id = harness.store.provision_administrator(
        "account-admin@example.test", "a strong account admin password"
    )
    uri = harness.store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.TOTP(pyotp.parse_uri(uri).secret)
    harness.store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    login = harness.post("/admin/auth/login", {
        "email": "account-admin@example.test",
        "password": "a strong account admin password",
        "totp_code": totp.at(int(time.time()) + 30),
    })
    assert login.status_code == 200
    assert harness.client.get("/account").status_code == 401
    assert harness.client.get("/auth/account").status_code == 401


def test_subscriber_password_change_requires_csrf_and_revokes_all_sessions(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "password-route@example.test", "correct horse battery"
    )
    challenge = harness.store.create_email_verification_challenge("password-route@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "password-route@example.test",
        "password": "correct horse battery",
    })
    assert login.status_code == 200
    current_token = harness.client.cookies.get("__Host-ooi_subscriber")
    second_token, _ = harness.store.create_session(identity_id)

    payload = {
        "current_password": "correct horse battery",
        "new_password": "a completely new secure password",
    }
    denied = harness.post("/auth/password/change", payload, csrf=False)
    assert denied.status_code == 403
    assert harness.store.get_session(current_token) is not None

    wrong = harness.post("/auth/password/change", {
        "current_password": "not the current password",
        "new_password": "a completely new secure password",
    })
    assert wrong.status_code == 401
    assert harness.store.get_session(current_token) is not None

    changed = harness.post("/auth/password/change", payload)
    assert changed.status_code == 200
    assert changed.headers["cache-control"] == "no-store"
    assert changed.json()["ok"] is True
    assert harness.store.get_session(current_token) is None
    assert harness.store.get_session(second_token) is None
    assert harness.client.cookies.get("__Host-ooi_subscriber") is None
    assert harness.client.cookies.get("__Host-ooi_csrf") is None
    assert harness.store.authenticate(
        "password-route@example.test", "correct horse battery", "subscriber"
    ) is None
    assert harness.store.authenticate(
        "password-route@example.test", "a completely new secure password", "subscriber"
    ) == identity_id
    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, metadata_json FROM security_audit_events
               WHERE event_type='subscriber.password_changed'
               AND subject_identity_id=?""",
            (identity_id,),
        ).fetchone()
    assert event["actor_identity_id"] == identity_id
    assert event["metadata_json"] == "{}"


def test_subscriber_password_change_is_rate_limited_by_identity(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "password-rate@example.test", "correct horse battery"
    )
    challenge = harness.store.create_email_verification_challenge("password-rate@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "password-rate@example.test",
        "password": "correct horse battery",
    })
    assert login.status_code == 200
    payload = {
        "current_password": "incorrect current password",
        "new_password": "a completely new secure password",
    }
    for _ in range(5):
        response = harness.post("/auth/password/change", payload)
        assert response.status_code == 401
    limited = harness.post("/auth/password/change", payload)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    active_token = harness.client.cookies.get("__Host-ooi_subscriber")
    assert harness.store.get_session(active_token) is not None


def test_subscriber_can_list_and_revoke_only_own_sessions(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "session-owner@example.test", "a strong session owner password"
    )
    challenge = harness.store.create_email_verification_challenge("session-owner@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "session-owner@example.test",
        "password": "a strong session owner password",
    })
    assert login.status_code == 200
    current_token = harness.client.cookies.get("__Host-ooi_subscriber")
    current_session = harness.store.get_session(current_token)
    other_token, other_session_id = harness.store.create_session(identity_id)

    listing = harness.client.get("/auth/sessions")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    data = listing.json()
    assert data["total"] == 2
    assert len(data["sessions"]) == 2
    current = next(row for row in data["sessions"] if row["is_current"])
    other = next(row for row in data["sessions"] if not row["is_current"])
    assert current["session_id"] == current_session["session_id"]
    assert other["session_id"] == other_session_id
    assert "identity_id" not in current
    assert "token_hash" not in str(data)
    assert current_token not in str(data)

    denied = harness.post(
        f"/auth/sessions/{other_session_id}/revoke", {}, csrf=False
    )
    assert denied.status_code == 403
    assert harness.store.get_session(other_token) is not None

    revoked = harness.post(f"/auth/sessions/{other_session_id}/revoke", {})
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True, "current_session": False}
    assert harness.store.get_session(other_token) is None
    assert harness.store.get_session(current_token) is not None

    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, subject_identity_id, metadata_json
               FROM security_audit_events
               WHERE event_type='subscriber.session_revoked'
               ORDER BY occurred_at DESC LIMIT 1"""
        ).fetchone()
    assert event["actor_identity_id"] == identity_id
    assert event["subject_identity_id"] == identity_id
    assert event["metadata_json"] == '{"self_service":true}'


def test_subscriber_session_revocation_cannot_target_another_account(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    first_id = harness.store.create_subscriber(
        "first-session-owner@example.test", "a strong first owner password"
    )
    second_id = harness.store.create_subscriber(
        "second-session-owner@example.test", "a strong second owner password"
    )
    first_challenge = harness.store.create_email_verification_challenge("first-session-owner@example.test")
    second_challenge = harness.store.create_email_verification_challenge("second-session-owner@example.test")
    assert harness.store.consume_email_verification_challenge(first_challenge) is True
    assert harness.store.consume_email_verification_challenge(second_challenge) is True
    first_token, _ = harness.store.create_session(first_id)
    second_token, second_session_id = harness.store.create_session(second_id)
    harness.client.cookies.set("__Host-ooi_subscriber", first_token, domain="ooi.example", path="/")
    harness._csrf()

    response = harness.post(f"/auth/sessions/{second_session_id}/revoke", {})
    assert response.status_code == 404
    assert harness.store.get_session(second_token) is not None


def test_subscriber_can_revoke_current_session_and_is_signed_out(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "current-session@example.test", "a strong current session password"
    )
    challenge = harness.store.create_email_verification_challenge("current-session@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "current-session@example.test",
        "password": "a strong current session password",
    })
    assert login.status_code == 200
    token = harness.client.cookies.get("__Host-ooi_subscriber")
    session = harness.store.get_session(token)

    response = harness.post(f"/auth/sessions/{session['session_id']}/revoke", {})
    assert response.status_code == 200
    assert response.json() == {"revoked": True, "current_session": True}
    assert harness.store.get_session(token) is None
    assert harness.client.cookies.get("__Host-ooi_subscriber") is None
    assert harness.client.cookies.get("__Host-ooi_csrf") is None
    assert harness.client.get("/auth/account").status_code == 401
    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, subject_identity_id
               FROM security_audit_events
               WHERE event_type='subscriber.session_revoked'
               AND subject_identity_id=?""",
            (identity_id,),
        ).fetchone()
    assert event["actor_identity_id"] == identity_id
    assert event["subject_identity_id"] == identity_id


def test_subscriber_session_listing_is_bounded_and_paginated(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "session-pages@example.test", "a strong session pagination password"
    )
    challenge = harness.store.create_email_verification_challenge("session-pages@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "session-pages@example.test",
        "password": "a strong session pagination password",
    })
    assert login.status_code == 200
    current_token = harness.client.cookies.get("__Host-ooi_subscriber")
    created = [harness.store.create_session(identity_id) for _ in range(27)]
    created_ids = {session_id for _token, session_id in created}

    first = harness.client.get("/auth/sessions?limit=25&offset=0")
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["total"] == 28
    assert first_data["limit"] == 25
    assert first_data["offset"] == 0
    assert len(first_data["sessions"]) == 25
    assert first_data["sessions"][0]["is_current"] is True

    second = harness.client.get("/auth/sessions?limit=25&offset=25")
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["total"] == 28
    assert second_data["offset"] == 25
    assert len(second_data["sessions"]) == 3
    assert all(not row["is_current"] for row in second_data["sessions"])

    first_ids = {row["session_id"] for row in first_data["sessions"]}
    second_ids = {row["session_id"] for row in second_data["sessions"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == created_ids | {
        harness.store.get_session(current_token)["session_id"]
    }
    assert harness.client.get("/auth/sessions?limit=101").status_code == 422


def test_subscriber_can_sign_out_all_devices(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    identity_id = harness.store.create_subscriber(
        "signout-all@example.test", "a strong sign out all password"
    )
    challenge = harness.store.create_email_verification_challenge("signout-all@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "signout-all@example.test",
        "password": "a strong sign out all password",
    })
    assert login.status_code == 200
    current_token = harness.client.cookies.get("__Host-ooi_subscriber")
    other_token, _ = harness.store.create_session(identity_id)

    denied = harness.post("/auth/sessions/revoke-all", {}, csrf=False)
    assert denied.status_code == 403
    assert harness.store.get_session(current_token) is not None
    assert harness.store.get_session(other_token) is not None

    response = harness.post("/auth/sessions/revoke-all", {})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"revoked_sessions": 2, "signed_out": True}
    assert harness.store.get_session(current_token) is None
    assert harness.store.get_session(other_token) is None
    assert harness.client.cookies.get("__Host-ooi_subscriber") is None
    assert harness.client.cookies.get("__Host-ooi_csrf") is None
    assert harness.client.get("/auth/account").status_code == 401

    with harness.store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, subject_identity_id, metadata_json
               FROM security_audit_events
               WHERE event_type='identity.sessions_revoked'
               AND subject_identity_id=?
               ORDER BY occurred_at DESC LIMIT 1""",
            (identity_id,),
        ).fetchone()
    assert event["actor_identity_id"] == identity_id
    assert event["subject_identity_id"] == identity_id
    assert '"revoked_count":2' in event["metadata_json"]


def test_subscriber_billing_summary_is_private_bounded_and_self_only(tmp_path, monkeypatch):
    harness = IdentityRouteHarness(tmp_path, monkeypatch)
    harness.store.create_subscriber("billing-user@example.test", "a strong billing password")
    challenge = harness.store.create_email_verification_challenge("billing-user@example.test")
    assert harness.store.consume_email_verification_challenge(challenge) is True
    login = harness.post("/auth/login", {
        "email": "billing-user@example.test",
        "password": "a strong billing password",
    })
    assert login.status_code == 200

    now = "2026-11-25T00:00:00+00:00"
    with harness.store.connect() as db:
        identity_id = db.execute(
            "SELECT id FROM identities WHERE email='billing-user@example.test'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at)
               VALUES (?, 'cus_private_billing', ?, ?)""",
            (identity_id, now, now),
        )
        db.execute(
            """INSERT INTO billing_subscriptions(
                id, identity_id, stripe_subscription_id, stripe_customer_id,
                plan_code, billing_interval, status, cancel_at_period_end,
                created_at, updated_at, last_event_created_at
            ) VALUES ('local-sub', ?, 'sub_private', 'cus_private_billing',
                      'basic', 'year', 'active', 1, ?, ?, ?)""",
            (identity_id, now, now, now),
        )
        db.execute(
            """INSERT INTO billing_invoices(
                id, identity_id, stripe_invoice_id, stripe_customer_id,
                stripe_subscription_id, status, amount_due, amount_paid, currency,
                last_event_created_at, created_at, updated_at
            ) VALUES ('local-invoice', ?, 'in_private', 'cus_private_billing',
                      'sub_private', 'paid', 12000, 12000, 'usd', ?, ?, ?)""",
            (identity_id, now, now, now),
        )

    response = harness.client.get("/auth/billing")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    data = response.json()
    assert data["checkout_available"] is False
    assert data["customer_portal_available"] is False
    assert data["price_catalog"] == {
        "currency": "usd",
        "monthly": {"amount_cents": 1068, "interval": "month", "recurring": True},
        "annual": {"amount_cents": 10405, "interval": "year", "recurring": True},
        "checkout_available": False,
    }
    assert data["subscriptions"] == [{
        "subscription_id": "sub_private",
        "plan_code": "basic",
        "billing_interval": "year",
        "status": "active",
        "current_period_start": None,
        "current_period_end": None,
        "cancel_at_period_end": True,
        "canceled_at": None,
        "ended_at": None,
        "updated_at": now,
    }]
    assert data["invoices"][0]["invoice_id"] == "in_private"
    assert data["invoices"][0]["amount_paid"] == 12000
    assert "stripe_customer_id" not in response.text
    assert "encrypted_payload" not in response.text
    assert "lease_token" not in response.text

    harness.post("/auth/logout", {})
    assert harness.client.get("/auth/billing").status_code == 401
