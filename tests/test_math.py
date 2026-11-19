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

def test_consensus_can_exclude_book_and_uses_median():
    from app.math import market_consensus
    event = {"id": "game-1", "bookmakers": [
        {"key": "book-a", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": -110}, {"name": "Away", "price": -110}]}]},
        {"key": "book-b", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": -150}, {"name": "Away", "price": 130}]}]},
        {"key": "book-c", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": -160}, {"name": "Away", "price": 140}]}]},
    ]}
    all_books = market_consensus([event])["game-1"]
    without_a = market_consensus([event], exclude_bookmaker_key="book-a")["game-1"]
    assert all_books["Home"] != pytest.approx(without_a["Home"])
    assert without_a["Home"] > 0.5


@pytest.mark.parametrize("price", [True, False, 0, float("inf"), float("-inf"), float("nan"), None, ""])
def test_american_odds_helpers_reject_boolean_and_non_finite_prices(price):
    with pytest.raises(ValueError):
        american_to_decimal(price)
    with pytest.raises(ValueError):
        american_implied_probability(price)


def test_market_consensus_skips_books_with_boolean_prices():
    from app.math import market_consensus

    event = {"id": "boolean-price", "bookmakers": [
        {"key": "invalid", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": True}, {"name": "Away", "price": -110}]}]},
        {"key": "valid", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
    ]}
    consensus = market_consensus([event])["boolean-price"]
    assert set(consensus) == {"Home", "Away"}
    assert consensus["Home"] == pytest.approx(120 / 220)
