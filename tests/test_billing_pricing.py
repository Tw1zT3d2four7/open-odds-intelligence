"""Tests for approved subscription prices and Stripe fee gross-up."""
import pytest

from app.billing_pricing import (
    ANNUAL_PRICE_CENTS,
    ANNUAL_TARGET_NET_CENTS,
    MONTHLY_PRICE_CENTS,
    MONTHLY_TARGET_NET_CENTS,
    PLAN_PRICES,
    estimated_stripe_fee_cents,
    gross_price_cents_for_net,
)


def test_monthly_price_nets_ten_dollars_after_estimated_stripe_fees():
    assert MONTHLY_PRICE_CENTS == 1068
    assert MONTHLY_PRICE_CENTS - estimated_stripe_fee_cents(MONTHLY_PRICE_CENTS) == 1000


def test_annual_price_nets_one_hundred_dollars_after_estimated_stripe_fees():
    assert ANNUAL_PRICE_CENTS == 10405
    assert ANNUAL_PRICE_CENTS - estimated_stripe_fee_cents(ANNUAL_PRICE_CENTS) == 10000


def test_prices_are_monthly_and_yearly_recurring_intervals():
    assert PLAN_PRICES["month"]["interval"] == "month"
    assert PLAN_PRICES["year"]["interval"] == "year"
    assert PLAN_PRICES["month"]["target_net_cents"] == MONTHLY_TARGET_NET_CENTS
    assert PLAN_PRICES["year"]["target_net_cents"] == ANNUAL_TARGET_NET_CENTS


@pytest.mark.parametrize("value", [-1, True, 1.25, "100"])
def test_fee_estimate_rejects_invalid_amounts(value):
    with pytest.raises(ValueError):
        estimated_stripe_fee_cents(value)


@pytest.mark.parametrize("value", [-1, True, 1.25, "100"])
def test_gross_up_rejects_invalid_targets(value):
    with pytest.raises(ValueError):
        gross_price_cents_for_net(value)


def test_gross_up_returns_minimum_cent_price():
    for target in (1000, 10000):
        gross = gross_price_cents_for_net(target)
        assert gross - estimated_stripe_fee_cents(gross) >= target
        assert gross - 1 - estimated_stripe_fee_cents(gross - 1) < target



@pytest.mark.parametrize(
    ("interval", "price_id", "amount", "stripe_interval", "livemode", "active", "expected"),
    [
        ("month", "price_MonthlyExact", 1068, "month", False, True, True),
        ("year", "price_YearlyExact", 10405, "year", False, True, True),
        ("month", "price_MonthlyExact", 1067, "month", False, True, False),
        ("year", "price_YearlyExact", 10405, "month", False, True, False),
        ("month", "price_MonthlyExact", 1068, "month", True, True, False),
        ("month", "price_MonthlyExact", 1068, "month", False, False, False),
    ],
)
def test_recurring_price_validation_is_exact(
    interval, price_id, amount, stripe_interval, livemode, active, expected
):
    from app.billing_pricing import validate_recurring_price

    price = {
        "id": price_id,
        "active": active,
        "livemode": livemode,
        "currency": "usd",
        "unit_amount": amount,
        "recurring": {"interval": stripe_interval, "interval_count": 1},
    }
    assert validate_recurring_price(price, interval, price_id) is expected


def test_recurring_price_validation_rejects_untrusted_or_malformed_objects():
    from app.billing_pricing import validate_recurring_price

    assert validate_recurring_price({}, "month", "price_MonthlyExact") is False
    assert validate_recurring_price([], "month", "price_MonthlyExact") is False
    assert validate_recurring_price({}, [], "price_MonthlyExact") is False
    assert validate_recurring_price({}, "month", "price_bad!") is False
