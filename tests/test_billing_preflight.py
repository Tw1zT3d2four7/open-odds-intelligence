"""Read-only Stripe test-mode preflight tests."""
from cryptography.fernet import Fernet

from app import config, billing_preflight


def _configure_valid_test_environment(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_PreflightCaseSensitive")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_LIVEMODE", False)
    monkeypatch.setattr(config, "STRIPE_TEST_MODE_ACKNOWLEDGED", True)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_testPreflightSecret12345")
    monkeypatch.setattr(
        config, "STRIPE_WEBHOOK_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://test-ooi.example")
    monkeypatch.setattr(config, "STRIPE_PRICE_MONTHLY_ID", "price_PreflightMonthly")
    monkeypatch.setattr(config, "STRIPE_PRICE_YEARLY_ID", "price_PreflightAnnual")


def test_preflight_checks_both_server_owned_prices_without_creating_objects(monkeypatch):
    _configure_valid_test_environment(monkeypatch)
    calls = []

    def fake_stripe(method, path, *, params=None, idempotency_key=None):
        calls.append((method, path, params))
        if path.endswith("price_PreflightMonthly"):
            return {
                "id": "price_PreflightMonthly", "active": True, "livemode": False,
                "currency": "usd", "unit_amount": 1068,
                "recurring": {"interval": "month", "interval_count": 1},
            }
        if path.endswith("price_PreflightAnnual"):
            return {
                "id": "price_PreflightAnnual", "active": True, "livemode": False,
                "currency": "usd", "unit_amount": 10405,
                "recurring": {"interval": "year", "interval_count": 1},
            }
        raise AssertionError(path)

    monkeypatch.setattr(billing_preflight.stripe_api, "stripe_request", fake_stripe)
    result = billing_preflight.run_preflight()
    assert result["test_mode_acknowledged"] is True
    assert result["monthly_price"] is True
    assert result["annual_price"] is True
    assert result["provider_requests_made"] == 2
    assert calls == [
        ("GET", "/prices/price_PreflightMonthly", None),
        ("GET", "/prices/price_PreflightAnnual", None),
    ]


def test_preflight_fails_closed_before_provider_calls_without_webhook_storage(monkeypatch):
    _configure_valid_test_environment(monkeypatch)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        billing_preflight.stripe_api, "stripe_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be called")),
    )
    result = billing_preflight.run_preflight()
    assert result["test_mode_acknowledged"] is True
    assert result["webhook_storage"] is False
    assert result["monthly_price"] is False
    assert result["annual_price"] is False
    assert result["provider_requests_made"] == 0


def test_preflight_rejects_live_credentials_and_non_https_origin(monkeypatch):
    _configure_valid_test_environment(monkeypatch)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_not_allowed")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://test-ooi.example")
    monkeypatch.setattr(
        billing_preflight.stripe_api, "stripe_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be called")),
    )
    result = billing_preflight.run_preflight()
    assert result["test_api_key"] is False
    assert result["public_https_origin"] is False
    assert result["provider_requests_made"] == 0
