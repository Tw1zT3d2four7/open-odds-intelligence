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
