"""Security and server-side price selection tests for hosted Stripe billing."""
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, stripe_api
from app.dependencies import get_identity_store, require_authenticated_csrf, require_subscriber
from app.identity import IdentityStore
from app.identity_routes import router


@pytest.fixture
def billing_harness(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("buyer@example.com", "a strong password 123")
    token = store.create_email_verification_challenge("buyer@example.com")
    assert store.consume_email_verification_challenge(token)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ooi.example")
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_UnitTestSecretCaseSensitive")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", False)
    monkeypatch.setattr(config, "STRIPE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(config, "STRIPE_CUSTOMER_PORTAL_ENABLED", True)
    monkeypatch.setattr(config, "STRIPE_PRICE_MONTHLY_ID", "price_MonthlyCaseSensitive")
    monkeypatch.setattr(config, "STRIPE_PRICE_YEARLY_ID", "price_YearlyCaseSensitive")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_identity_store] = lambda: store
    app.dependency_overrides[require_subscriber] = lambda: {
        "identity_id": identity_id, "email": "buyer@example.com"
    }
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    return TestClient(app, base_url="https://ooi.example"), store, identity_id


def test_checkout_uses_server_owned_monthly_price_and_persists_customer_mapping(
    billing_harness, monkeypatch
):
    client, store, identity_id = billing_harness
    calls = []

    def fake_stripe(method, path, *, params=None, idempotency_key=None):
        calls.append((method, path, params, idempotency_key))
        if path.startswith("/prices/"):
            return {
                "id": "price_MonthlyCaseSensitive", "active": True, "livemode": False,
                "currency": "usd", "unit_amount": 1068,
                "recurring": {"interval": "month", "interval_count": 1},
            }
        if path == "/customers":
            return {"id": "cus_test123"}
        if path == "/checkout/sessions":
            return {"id": "cs_test_123", "url": "https://checkout.stripe.com/c/pay/cs_test_123"}
        raise AssertionError(path)

    monkeypatch.setattr(stripe_api, "stripe_request", fake_stripe)
    response = client.post(
        "/auth/billing/checkout",
        json={"interval": "month", "price_id": "price_attacker_supplied"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 422  # Strict request model rejects client price fields.
    response = client.post(
        "/auth/billing/checkout",
        json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://checkout.stripe.com/")
    assert store.billing_customer_id(identity_id) == "cus_test123"
    checkout = next(call for call in calls if call[1] == "/checkout/sessions")
    assert checkout[2]["line_items[0][price]"] == "price_MonthlyCaseSensitive"
    assert checkout[2]["customer"] == "cus_test123"
    assert checkout[2]["mode"] == "subscription"
    assert checkout[2]["subscription_data[metadata][identity_id]"] == identity_id
    assert "sk_test" not in response.text
    billing = client.get("/auth/billing")
    assert billing.status_code == 200
    assert billing.json()["checkout_available"] is False
    assert billing.json()["customer_portal_available"] is True


def test_checkout_uses_annual_price_only_for_year_interval(billing_harness, monkeypatch):
    client, _store, _identity_id = billing_harness
    calls = []

    def fake_stripe(method, path, *, params=None, idempotency_key=None):
        calls.append((path, params))
        if path == "/prices/price_YearlyCaseSensitive":
            return {
                "id": "price_YearlyCaseSensitive", "active": True, "livemode": False,
                "currency": "usd", "unit_amount": 10405,
                "recurring": {"interval": "year", "interval_count": 1},
            }
        if path == "/customers":
            return {"id": "cus_testannual"}
        if path == "/checkout/sessions":
            return {"id": "cs_test_annual", "url": "https://checkout.stripe.com/c/pay/cs_test_annual"}
        raise AssertionError(path)

    monkeypatch.setattr(stripe_api, "stripe_request", fake_stripe)
    response = client.post(
        "/auth/billing/checkout", json={"interval": "year"},
        headers={"Idempotency-Key": "annual-key-123456789"},
    )
    assert response.status_code == 200
    checkout = next(params for path, params in calls if path == "/checkout/sessions")
    assert checkout["line_items[0][price]"] == "price_YearlyCaseSensitive"


def test_checkout_fails_closed_for_live_key_or_live_webhook_mode(billing_harness, monkeypatch):
    client, _store, _identity_id = billing_harness
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_never-use")
    response = client.post(
        "/auth/billing/checkout", json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 503
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_UnitTestSecretCaseSensitive")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", True)
    response = client.post(
        "/auth/billing/checkout", json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 503


def test_checkout_blocks_duplicate_nonterminal_subscription(billing_harness, monkeypatch):
    client, store, identity_id = billing_harness
    store.link_billing_customer(identity_id, "cus_existingSubscription")
    with store.connect() as db:
        db.execute(
            """INSERT INTO billing_subscriptions(
                id, identity_id, stripe_subscription_id, stripe_customer_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            ("local-sub", identity_id, "sub_existing", "cus_existingSubscription",
             "2026-11-25T00:00:00+00:00", "2026-11-25T00:00:00+00:00"),
        )
    monkeypatch.setattr(
        stripe_api, "stripe_request",
        lambda *args, **kwargs: pytest.fail("Checkout must not create a duplicate subscription"),
    )
    response = client.post(
        "/auth/billing/checkout", json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 409
    assert "existing subscription" in response.json()["detail"]


def test_checkout_requires_idempotency_key_and_enabled_flag(billing_harness, monkeypatch):
    client, _store, _identity_id = billing_harness
    response = client.post("/auth/billing/checkout", json={"interval": "month"})
    assert response.status_code == 400
    monkeypatch.setattr(config, "STRIPE_CHECKOUT_ENABLED", False)
    response = client.post(
        "/auth/billing/checkout", json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 503


def test_checkout_rejects_price_with_wrong_amount_or_interval(billing_harness, monkeypatch):
    client, _store, _identity_id = billing_harness
    calls = []

    def fake_stripe(method, path, *, params=None, idempotency_key=None):
        calls.append(path)
        return {
            "id": "price_MonthlyCaseSensitive", "active": True, "livemode": False,
            "currency": "usd", "unit_amount": 999,
            "recurring": {"interval": "month", "interval_count": 1},
        }

    monkeypatch.setattr(stripe_api, "stripe_request", fake_stripe)
    response = client.post(
        "/auth/billing/checkout", json={"interval": "month"},
        headers={"Idempotency-Key": "client-key-1234567890"},
    )
    assert response.status_code == 503
    assert calls == ["/prices/price_MonthlyCaseSensitive"]


def test_customer_portal_requires_existing_local_customer_mapping(billing_harness, monkeypatch):
    client, store, identity_id = billing_harness
    calls = []

    def fake_stripe(method, path, *, params=None, idempotency_key=None):
        calls.append((path, params))
        return {"url": "https://billing.stripe.com/p/session/test_123"}

    monkeypatch.setattr(stripe_api, "stripe_request", fake_stripe)
    missing = client.post("/auth/billing/portal")
    assert missing.status_code == 409
    assert calls == []
    store.link_billing_customer(identity_id, "cus_testLinked")
    response = client.post("/auth/billing/portal")
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://billing.stripe.com/")
    assert calls[0][1]["customer"] == "cus_testLinked"


def test_customer_portal_does_not_accept_untrusted_customer_ids(billing_harness, monkeypatch):
    client, store, identity_id = billing_harness
    store.link_billing_customer(identity_id, "cus_testLinked")
    calls = []
    monkeypatch.setattr(
        stripe_api, "stripe_request",
        lambda *args, **kwargs: calls.append(kwargs) or {
            "url": "https://billing.stripe.com/p/session/test_123"
        },
    )
    response = client.post("/auth/billing/portal", json={"customer_id": "cus_attacker"})
    assert response.status_code == 200
    assert calls[0]["params"]["customer"] == "cus_testLinked"


def test_stripe_api_client_rejects_non_test_secret_without_network(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_do_not_use")
    monkeypatch.setattr(
        stripe_api, "urlopen",
        lambda *args, **kwargs: pytest.fail("network must not be called for live credentials"),
    )
    with pytest.raises(stripe_api.StripeAPIError):
        stripe_api.stripe_request("POST", "/customers", params={"email": "buyer@example.com"})
