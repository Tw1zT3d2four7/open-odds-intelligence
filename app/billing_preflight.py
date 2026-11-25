"""Read-only Stripe test-mode configuration preflight.

This command performs GET-only checks for the approved monthly and annual
Stripe Prices. It never creates customers, Checkout Sessions, subscriptions,
Portal sessions, or charges.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from . import config, stripe_api
from .billing_pricing import PLAN_PRICES, validate_recurring_price


def _public_https_origin_ready() -> bool:
    try:
        parsed = urlsplit(config.PUBLIC_BASE_URL)
        parsed.port
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def _webhook_storage_ready() -> bool:
    secret = config.STRIPE_WEBHOOK_SECRET
    key = config.STRIPE_WEBHOOK_ENCRYPTION_KEY
    if (
        not isinstance(secret, str) or not secret.startswith("whsec_")
        or len(secret) < 16 or secret.strip() != secret
        or not isinstance(key, str) or not key
    ):
        return False
    try:
        Fernet(key.encode("ascii", errors="strict"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
    return True


def run_preflight() -> dict[str, object]:
    """Check required non-production configuration and both recurring Price objects."""
    result: dict[str, object] = {
        "test_api_key": bool(
            isinstance(config.STRIPE_SECRET_KEY, str)
            and re.fullmatch(r"sk_test_[A-Za-z0-9]+", config.STRIPE_SECRET_KEY)
        ),
        "test_webhook_mode": not bool(config.STRIPE_WEBHOOK_LIVEMODE),
        "test_mode_acknowledged": bool(config.STRIPE_TEST_MODE_ACKNOWLEDGED),
        "webhook_storage": _webhook_storage_ready(),
        "public_https_origin": _public_https_origin_ready(),
        "checkout_switch": bool(config.STRIPE_CHECKOUT_ENABLED),
        "portal_switch": bool(config.STRIPE_CUSTOMER_PORTAL_ENABLED),
        "monthly_price": False,
        "annual_price": False,
        "provider_requests_made": 0,
    }
    if not all(
        result[key]
        for key in ("test_api_key", "test_webhook_mode", "test_mode_acknowledged", "webhook_storage", "public_https_origin")
    ):
        return result

    for interval, price_key, result_key in (
        ("month", "STRIPE_PRICE_MONTHLY_ID", "monthly_price"),
        ("year", "STRIPE_PRICE_YEARLY_ID", "annual_price"),
    ):
        price_id = getattr(config, price_key, "")
        if not isinstance(price_id, str) or not re.fullmatch(r"price_[A-Za-z0-9]+", price_id):
            continue
        try:
            price = stripe_api.stripe_request("GET", f"/prices/{price_id}")
            result["provider_requests_made"] = int(result["provider_requests_made"]) + 1
        except stripe_api.StripeAPIError:
            continue
        result[result_key] = validate_recurring_price(price, interval, price_id)

    return result


def main() -> int:
    result = run_preflight()
    labels = (
        ("test_api_key", "Stripe API key is test-mode"),
        ("test_webhook_mode", "Webhook mode is test-mode"),
        ("test_mode_acknowledged", "Operator explicitly acknowledged isolated test mode"),
        ("webhook_storage", "Webhook signature/encrypted storage configured"),
        ("public_https_origin", "Public application origin is HTTPS"),
        ("monthly_price", "Monthly recurring Price matches approved catalog"),
        ("annual_price", "Annual recurring Price matches approved catalog"),
    )
    print("Open Odds Intelligence — Stripe test-mode preflight")
    for key, label in labels:
        print(f"[{'OK' if result[key] else 'FAIL'}] {label}")
    print(f"[INFO] Checkout switch: {'ON' if result['checkout_switch'] else 'OFF'}")
    print(f"[INFO] Customer Portal switch: {'ON' if result['portal_switch'] else 'OFF'}")
    print(f"[INFO] Read-only Stripe API requests: {result['provider_requests_made']}")
    print("No customer, Checkout Session, subscription, Portal session, or payment was created.")
    required = (
        "test_api_key", "test_webhook_mode", "test_mode_acknowledged",
        "webhook_storage", "public_https_origin", "monthly_price", "annual_price",
    )
    return 0 if all(result[key] for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
