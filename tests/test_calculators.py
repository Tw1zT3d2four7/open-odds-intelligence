import pytest

from app.calculators import (
    american_to_decimal, convert_odds, decimal_to_american, ev_kelly,
    hedge, no_vig, parlay, payout,
)


def test_odds_conversions_round_trip():
    assert american_to_decimal(-110) == pytest.approx(1.9090909)
    assert decimal_to_american(2.0) == 100
    assert convert_odds(-110, "american", "decimal") == pytest.approx(1.909091)
    assert convert_odds(1.91, "decimal", "american") == -110


def test_payout_reports_profit_return_and_break_even():
    result = payout(100, -110)
    assert result["profit"] == pytest.approx(90.91)
    assert result["total_return"] == pytest.approx(190.91)
    assert result["break_even_probability"] == pytest.approx(110 / 210)


def test_parlay_combines_any_number_of_independent_legs():
    result = parlay([-110, 150, 100])
    expected = (1 + 100 / 110) * 2.5 * 2
    assert result["legs"] == 3
    assert result["decimal_odds"] == pytest.approx(expected)


def test_no_vig_two_way_market_normalizes_probabilities():
    result = no_vig(-110, -110)
    assert result["overround"] == pytest.approx(4.761905)
    assert result["first_fair_probability"] == pytest.approx(0.5)
    assert result["second_fair_probability"] == pytest.approx(0.5)


def test_ev_and_fractional_kelly_are_bankroll_sized():
    result = ev_kelly(2.0, 0.55, 1000, 0.25, "decimal")
    assert result["ev_per_100"] == pytest.approx(10)
    assert result["edge_percentage_points"] == pytest.approx(5)
    assert result["full_kelly_stake"] == pytest.approx(100)
    assert result["fractional_kelly_stake"] == pytest.approx(25)


def test_hedge_equalizes_both_outcomes():
    result = hedge(100, 150, 200)
    assert result["hedge_stake"] == pytest.approx(83.33, abs=0.01)
    assert result["profit_if_original_wins"] == pytest.approx(66.67, abs=0.01)
    assert result["profit_if_hedge_wins"] == pytest.approx(66.67, abs=0.01)


@pytest.mark.parametrize("call", [
    lambda: payout(0, -110),
    lambda: payout(100, 0),
    lambda: parlay([]),
    lambda: no_vig(-110, 0),
    lambda: ev_kelly(-110, 1.2, 100),
    lambda: hedge(-10, -110, -110),
])
def test_invalid_calculator_inputs_are_rejected(call):
    with pytest.raises(ValueError):
        call()
