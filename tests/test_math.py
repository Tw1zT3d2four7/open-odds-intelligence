import pytest
from app.math import american_to_decimal, american_implied_probability, no_vig_probabilities, expected_value_percent, two_way_arbitrage

def test_american_conversion():
    assert american_to_decimal(150) == pytest.approx(2.5)
    assert american_to_decimal(-150) == pytest.approx(1.6666667)
    assert american_implied_probability(-110) == pytest.approx(110/210)

def test_no_vig():
    p = no_vig_probabilities([-110, -110])
    assert sum(p) == pytest.approx(1)
    assert p == pytest.approx([0.5, 0.5])

def test_ev():
    assert expected_value_percent(110, 0.5) == pytest.approx(5)

def test_arbitrage():
    assert two_way_arbitrage([110, 110])["arb_percent"] > 0
    assert two_way_arbitrage([-110, -110]) is None

def test_invalid_odds():
    with pytest.raises(ValueError):
        american_to_decimal(0)
