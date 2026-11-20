import pytest

from app.calculators import (
    american_to_decimal, convert_odds, decimal_to_american, ev_kelly,
    hedge, no_vig, parlay, payout, run_line_minus_one,
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


def test_run_line_minus_one_wins_when_selected_team_wins_by_two_or_more():
    from app.calculators import run_line_minus_one
    result = run_line_minus_one(100, -110, 2)
    assert result["result"] == "win"
    assert result["profit"] == pytest.approx(90.91)
    assert result["total_return"] == pytest.approx(190.91)


def test_puck_line_minus_one_pushes_when_selected_team_wins_by_exactly_one():
    from app.calculators import run_line_minus_one
    result = run_line_minus_one(100, 150, 1)
    assert result["result"] == "push"
    assert result["profit"] == 0
    assert result["total_return"] == 100


def test_run_line_minus_one_loses_on_tie_or_selected_team_loss():
    from app.calculators import run_line_minus_one
    assert run_line_minus_one(100, -105, 0)["result"] == "loss"
    assert run_line_minus_one(100, -105, -3)["total_return"] == 0


def test_run_line_minus_one_rejects_invalid_stake_or_odds():
    from app.calculators import run_line_minus_one
    with pytest.raises(ValueError):
        run_line_minus_one(0, -110, 2)
    with pytest.raises(ValueError):
        run_line_minus_one(100, 0, 2)


def test_run_line_minus_one_rejects_fractional_score_margin():
    from app.calculators import run_line_minus_one
    with pytest.raises(ValueError, match="whole number"):
        run_line_minus_one(100, -110, 1.5)


def test_synthetic_minus_one_derives_push_probability_and_fair_price():
    from app.calculators import synthetic_minus_one
    result = synthetic_minus_one(-150, 130, 120, -140)
    ml = no_vig(-150, 130)["first_fair_probability"]
    cover = no_vig(120, -140)["first_fair_probability"]
    assert result["moneyline_fair_probability"] == pytest.approx(ml)
    assert result["cover_probability"] == pytest.approx(cover)
    assert result["push_probability"] == pytest.approx(ml - cover)
    loss = 1 - ml
    expected_decimal = 1 + loss / cover
    assert result["loss_probability"] == pytest.approx(loss)
    assert result["decisive_win_probability"] == pytest.approx(cover / (cover + loss))
    assert result["cover_probability"] + result["push_probability"] + result["loss_probability"] == pytest.approx(1)
    assert result["fair_decimal"] == pytest.approx(expected_decimal)
    assert result["fair_american"] == decimal_to_american(expected_decimal)
    # A fair -1 wager has zero expected profit when the one-run/goal push
    # returns the stake and only cover/loss outcomes are settled.


def test_synthetic_minus_one_rejects_inconsistent_market_probabilities():
    from app.calculators import synthetic_minus_one
    with pytest.raises(ValueError, match="Inconsistent prices"):
        synthetic_minus_one(200, -250, -300, 250)



@pytest.mark.parametrize("call", [
    lambda: payout(True, -110),
    lambda: payout(100, True),
    lambda: parlay([True]),
    lambda: no_vig(True, -110),
    lambda: ev_kelly(-110, True, 100),
    lambda: ev_kelly(-110, 0.55, 100, True),
    lambda: hedge(100, -110, True),
])
def test_boolean_numeric_inputs_are_rejected(call):
    with pytest.raises(ValueError):
        call()


def test_run_line_minus_one_rejects_boolean_margin():
    from app.calculators import run_line_minus_one
    with pytest.raises(ValueError, match="score margin"):
        run_line_minus_one(100, -110, True)


@pytest.mark.parametrize("odds", [-99, -50, -1, 1, 50, 99])
def test_american_odds_reject_values_between_minus_and_plus_100(odds):
    with pytest.raises(ValueError, match="\+100 or greater"):
        american_to_decimal(odds)


@pytest.mark.parametrize("odds, expected", [
    (-100, 2.0),
    (100, 2.0),
    (-101, pytest.approx(1 + 100 / 101)),
    (101, 2.01),
])
def test_american_odds_accept_standard_boundary_values(odds, expected):
    assert american_to_decimal(odds) == expected


@pytest.mark.parametrize("call", [
    lambda: payout(10 ** 10000, -110),
    lambda: payout([], -110),
    lambda: ev_kelly(-110, 10 ** 10000, 100),
    lambda: ev_kelly(-110, 0.55, 100, 10 ** 10000),
    lambda: ev_kelly(-110, [], 100),
    lambda: ev_kelly(-110, 0.55, 100, []),
    lambda: run_line_minus_one(100, -110, 10 ** 10000),
    lambda: run_line_minus_one(100, -110, []),
])
def test_calculator_numeric_overflow_and_malformed_values_raise_value_error(call):
    with pytest.raises(ValueError):
        call()



@pytest.mark.parametrize("odds", [-110.5, 110.5, "-110.5", "110.5"])
def test_calculators_reject_fractional_american_odds(odds):
    with pytest.raises(ValueError, match="whole number"):
        american_to_decimal(odds)
    with pytest.raises(ValueError, match="whole number"):
        payout(100, odds)
    with pytest.raises(ValueError, match="whole number"):
        parlay([odds])


@pytest.mark.parametrize("call", [
    lambda: payout(1e308, 1e308, "decimal"),
    lambda: payout(1, 1e308, "decimal"),
    lambda: parlay([1e308, 1e308], "decimal"),
    lambda: ev_kelly(1e308, 1, 100, format="decimal"),
    lambda: hedge(1e308, 1e308, 1.01, "decimal"),
    lambda: run_line_minus_one(1e308, 1e308, 2, "decimal"),
    lambda: decimal_to_american(1e308),
])
def test_calculators_reject_finite_inputs_that_overflow_results(call):
    with pytest.raises(ValueError):
        call()
