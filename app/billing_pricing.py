"""Approved subscriber prices and transparent Stripe fee gross-up estimates.

These are display/catalog prices only. Checkout remains disabled until Stripe
Price IDs, account-specific fees, entitlements, and launch acceptance are set.
"""
from decimal import Decimal, ROUND_HALF_UP
import re

CURRENCY = "usd"
STRIPE_DOMESTIC_CARD_RATE = Decimal("0.029")
STRIPE_BILLING_PAYG_RATE = Decimal("0.007")
STRIPE_FIXED_CARD_FEE_CENTS = 30


def _rounded_percent_cents(amount_cents: int, rate: Decimal) -> int:
    return int(
        (Decimal(amount_cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def estimated_stripe_fee_cents(amount_cents: int) -> int:
    """Estimate domestic card + Stripe Billing PAYG fees for one recurring charge."""
    if type(amount_cents) is not int or amount_cents < 0:
        raise ValueError("A non-negative integer amount in cents is required.")
    if amount_cents == 0:
        return 0
    card_fee = _rounded_percent_cents(amount_cents, STRIPE_DOMESTIC_CARD_RATE)
    billing_fee = _rounded_percent_cents(amount_cents, STRIPE_BILLING_PAYG_RATE)
    return card_fee + billing_fee + STRIPE_FIXED_CARD_FEE_CENTS


def gross_price_cents_for_net(target_net_cents: int) -> int:
    """Find the smallest cent-denominated price whose estimated net meets target."""
    if type(target_net_cents) is not int or target_net_cents < 0:
        raise ValueError("A non-negative integer target in cents is required.")
    if target_net_cents == 0:
        return 0
    gross = target_net_cents
    while gross - estimated_stripe_fee_cents(gross) < target_net_cents:
        gross += 1
    return gross


# User-approved net proceeds: $10.00 monthly and $100.00 annually.
MONTHLY_TARGET_NET_CENTS = 1_000
ANNUAL_TARGET_NET_CENTS = 10_000

# Gross prices cover the published US domestic card rate (2.9% + $0.30)
# and Stripe Billing pay-as-you-go (0.7%). International/currency-conversion
# fees, custom Stripe pricing, tax, refunds, and disputes are not included.
MONTHLY_PRICE_CENTS = gross_price_cents_for_net(MONTHLY_TARGET_NET_CENTS)
ANNUAL_PRICE_CENTS = gross_price_cents_for_net(ANNUAL_TARGET_NET_CENTS)

PLAN_PRICES = {
    "month": {
        "amount_cents": MONTHLY_PRICE_CENTS,
        "interval": "month",
        "target_net_cents": MONTHLY_TARGET_NET_CENTS,
    },
    "year": {
        "amount_cents": ANNUAL_PRICE_CENTS,
        "interval": "year",
        "target_net_cents": ANNUAL_TARGET_NET_CENTS,
    },
}


def validate_recurring_price(price: dict, interval: str, configured_price_id: str) -> bool:
    """Validate one server-configured Stripe test Price against the approved catalog."""
    if interval not in PLAN_PRICES or not isinstance(price, dict):
        return False
    if (
        not isinstance(configured_price_id, str)
        or not re.fullmatch(r"price_[A-Za-z0-9]+", configured_price_id)
    ):
        return False
    recurring = price.get("recurring")
    expected = PLAN_PRICES[interval]
    return bool(
        price.get("id") == configured_price_id
        and price.get("active") is True
        and price.get("livemode") is False
        and price.get("currency") == CURRENCY
        and type(price.get("unit_amount")) is int
        and price.get("unit_amount") == expected["amount_cents"]
        and isinstance(recurring, dict)
        and recurring.get("interval") == interval
        and type(recurring.get("interval_count")) is int
        and recurring.get("interval_count") == 1
    )
