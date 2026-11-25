"""Tests for safe, opt-in Stripe Checkout configuration."""
import json
import os
import subprocess
import sys


def _load_config(env_overrides):
    env = os.environ.copy()
    for key in (
        "STRIPE_SECRET_KEY",
        "STRIPE_PRICE_MONTHLY_ID",
        "STRIPE_PRICE_YEARLY_ID",
        "STRIPE_CHECKOUT_ENABLED",
        "STRIPE_CUSTOMER_PORTAL_ENABLED",
    ):
        env.pop(key, None)
    env.update(env_overrides)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app import config; "
                "print(json.dumps({"
                "'secret': config.STRIPE_SECRET_KEY, 'monthly': config.STRIPE_PRICE_MONTHLY_ID, "
                "'yearly': config.STRIPE_PRICE_YEARLY_ID, "
                "'checkout': config.STRIPE_CHECKOUT_ENABLED, "
                "'portal': config.STRIPE_CUSTOMER_PORTAL_ENABLED"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_checkout_and_portal_are_disabled_and_price_ids_empty_by_default():
    config = _load_config({})
    assert config == {
        "secret": "",
        "monthly": "",
        "yearly": "",
        "checkout": False,
        "portal": False,
    }


def test_price_ids_are_preserved_exactly_and_do_not_enable_checkout():
    config = _load_config({
        "STRIPE_SECRET_KEY": "sk_test_CaseSensitiveSecret123",
        "STRIPE_PRICE_MONTHLY_ID": "price_MonthlyCaseSensitive123",
        "STRIPE_PRICE_YEARLY_ID": "price_YearlyCaseSensitive456",
    })
    assert config["secret"] == "sk_test_CaseSensitiveSecret123"
    assert config["monthly"] == "price_MonthlyCaseSensitive123"
    assert config["yearly"] == "price_YearlyCaseSensitive456"
    assert config["checkout"] is False
    assert config["portal"] is False


def test_checkout_and_portal_require_separate_explicit_flags():
    config = _load_config({
        "STRIPE_PRICE_MONTHLY_ID": "price_month",
        "STRIPE_PRICE_YEARLY_ID": "price_year",
        "STRIPE_CHECKOUT_ENABLED": "true",
    })
    assert config["checkout"] is True
    assert config["portal"] is False
