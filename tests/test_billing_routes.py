"""HTTP boundary tests for signature-verified Stripe webhook intake."""
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.dependencies import get_identity_store
from app.identity import IdentityStore
from app.billing_routes import router


SECRET = "whsec_" + "t" * 40
TIMESTAMP = 1_800_000_000


def _payload(event_id="evt_http_123", *, livemode=False):
    return json.dumps({
        "id": event_id,
        "type": "customer.subscription.updated",
        "livemode": livemode,
        "data": {"object": {"id": "sub_test_123", "status": "active"}},
    }, separators=(",", ":")).encode()


def _signature(payload):
    digest = hmac.new(
        SECRET.encode(), str(TIMESTAMP).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return f"t={TIMESTAMP},v1={digest}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", False)
    # Freeze the verifier's clock through its module so timestamp tests are deterministic.
    import app.stripe_webhooks as verifier
    monkeypatch.setattr(verifier.time, "time", lambda: TIMESTAMP)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_identity_store] = lambda: store
    return TestClient(app), store


def test_webhook_intake_verifies_and_persists_only_fingerprint(client):
    http, store = client
    payload = _payload()
    response = http.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _signature(payload)},
    )
    assert response.status_code == 200
    assert response.json() == {
        "received": True, "duplicate": False, "processing_state": "received"
    }
    assert response.headers["cache-control"] == "no-store"
    with store.connect() as db:
        row = db.execute(
            "SELECT payload_sha256, processing_state FROM billing_webhook_events "
            "WHERE stripe_event_id='evt_http_123'"
        ).fetchone()
        columns = {item["name"] for item in db.execute(
            "PRAGMA table_info(billing_webhook_events)"
        )}
    assert row["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["processing_state"] == "received"
    assert "payload" not in columns and "raw_payload" not in columns


def test_webhook_duplicate_delivery_is_acknowledged_idempotently(client):
    http, _store = client
    payload = _payload()
    headers = {"stripe-signature": _signature(payload)}
    first = http.post("/webhooks/stripe", content=payload, headers=headers)
    second = http.post("/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


@pytest.mark.parametrize("headers", [
    {},
    {"stripe-signature": "t=1,v1=" + "0" * 64},
])
def test_webhook_rejects_missing_or_invalid_signatures(client, headers):
    http, store = client
    response = http.post("/webhooks/stripe", content=_payload(), headers=headers)
    assert response.status_code == 400
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_events").fetchone()[0] == 0


def test_webhook_rejects_test_live_mode_mismatch(client):
    http, store = client
    payload = _payload(livemode=True)
    response = http.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _signature(payload)},
    )
    assert response.status_code == 400
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_events").fetchone()[0] == 0


def test_webhook_rejects_oversized_and_empty_payloads(client):
    http, store = client
    response = http.post(
        "/webhooks/stripe", content=b"x" * 1_048_577,
        headers={"stripe-signature": "t=1,v1=" + "0" * 64},
    )
    assert response.status_code == 413
    empty = http.post("/webhooks/stripe", content=b"")
    assert empty.status_code == 413
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_events").fetchone()[0] == 0


def test_webhook_fails_closed_when_signing_secret_is_missing(client, monkeypatch):
    http, _store = client
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "")
    response = http.post("/webhooks/stripe", content=_payload())
    assert response.status_code == 503


def test_identity_mode_routes_stripe_webhook_to_signature_verifier(tmp_path, monkeypatch):
    from app import main
    store = IdentityStore(tmp_path / "middleware-accounts.sqlite3")
    monkeypatch.setattr(config, "AUTH_MODE", "identity")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", False)
    import app.stripe_webhooks as verifier
    monkeypatch.setattr(verifier.time, "time", lambda: TIMESTAMP)
    main.app.dependency_overrides[get_identity_store] = lambda: store
    try:
        response = TestClient(main.app).post(
            "/webhooks/stripe", content=_payload(),
            headers={"stripe-signature": "t=1,v1=" + "0" * 64},
        )
    finally:
        main.app.dependency_overrides.pop(get_identity_store, None)
    # 400 proves middleware admitted the request to cryptographic verification;
    # a middleware policy failure would return 503 before the handler.
    assert response.status_code == 400


def test_webhook_uses_stripe_signature_instead_of_legacy_browser_basic_auth(tmp_path, monkeypatch):
    from app import main
    store = IdentityStore(tmp_path / "legacy-middleware-accounts.sqlite3")
    monkeypatch.setattr(config, "AUTH_MODE", "legacy_basic")
    monkeypatch.setattr(config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "legacy-browser-password")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SECRET)
    import app.stripe_webhooks as verifier
    monkeypatch.setattr(verifier.time, "time", lambda: TIMESTAMP)
    main.app.dependency_overrides[get_identity_store] = lambda: store
    try:
        response = TestClient(main.app).post(
            "/webhooks/stripe", content=_payload(),
            headers={"stripe-signature": "t=1,v1=" + "0" * 64},
        )
    finally:
        main.app.dependency_overrides.pop(get_identity_store, None)
    assert response.status_code == 400
