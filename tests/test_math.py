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
    assert consensus["Home"] == pytest.approx((120 / 220) / ((120 / 220) + 0.5))


@pytest.mark.parametrize("price", [-99, -50, -1, 1, 50, 99])
def test_market_odds_helpers_reject_prices_inside_invalid_range(price):
    with pytest.raises(ValueError, match="\+100 or greater"):
        american_to_decimal(price)
    with pytest.raises(ValueError, match="\+100 or greater"):
        american_implied_probability(price)


@pytest.mark.parametrize("price, expected", [
    (-100, 2.0),
    (100, 2.0),
    (-101, pytest.approx(1 + 100 / 101)),
    (101, 2.01),
])
def test_market_odds_helpers_accept_valid_boundary_prices(price, expected):
    assert american_to_decimal(price) == expected
    assert american_implied_probability(price) == pytest.approx(
        100 / (price + 100) if price > 0 else abs(price) / (abs(price) + 100)
    )


@pytest.mark.parametrize("price", [-110.5, 110.5, "-110.5", "110.5"])
def test_market_math_rejects_fractional_american_prices(price):
    with pytest.raises(ValueError, match="whole number"):
        american_to_decimal(price)
    with pytest.raises(ValueError, match="whole number"):
        american_implied_probability(price)


def test_market_consensus_skips_invalid_american_prices_and_malformed_books():
    from app.math import market_consensus

    valid = {"key": "valid", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Home", "price": -120}, {"name": "Away", "price": 100},
    ]}]}
    events = [
        None,
        {"id": [], "bookmakers": [valid]},
        {
            "id": "robust-consensus",
            "bookmakers": [
                None,
                {"key": "bad-container", "markets": None},
                {"key": "bad-market", "markets": [None, "not-a-market"]},
                {"key": "invalid-range", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 99}, {"name": "Away", "price": -110},
                ]}]},
                {"key": "fractional", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -110.5}, {"name": "Away", "price": -110},
                ]}]},
                valid,
            ],
        },
    ]
    consensus = market_consensus(events)
    assert set(consensus) == {"robust-consensus"}
    assert set(consensus["robust-consensus"]) == {"Home", "Away"}
    assert consensus["robust-consensus"]["Home"] == pytest.approx(
        (120 / 220) / ((120 / 220) + 0.5)
    )
