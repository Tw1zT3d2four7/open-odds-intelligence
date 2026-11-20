from statistics import median

from app.math import no_vig_probabilities
from app.markets import analyze_markets, market_board


def event_fixture():
    return [{
        "id": "event-1", "sport_title": "Basketball", "home_team": "Home",
        "away_team": "Away", "commence_time": "2026-11-18T00:00:00Z",
        "bookmakers": [
            {"key": "a", "title": "Book A", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 120}, {"name": "Away", "price": -140}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": -110},
                    {"name": "Under", "point": 44.5, "price": -110}]}
            ]},
            {"key": "b", "title": "Book B", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 130}, {"name": "Away", "price": -150}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": 120},
                    {"name": "Under", "point": 44.5, "price": -140}]}
            ]},
            {"key": "c", "title": "Book C", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 140}, {"name": "Away", "price": -160}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": 125},
                    {"name": "Under", "point": 44.5, "price": -145}]}
            ]}
        ]
    }]


def test_multi_market_ev_uses_other_books_and_returns_market_identity():
    result = analyze_markets(event_fixture(), min_ev=-100, min_consensus_books=2)
    assert result["opportunities"]
    assert {row["market"] for row in result["opportunities"]} == {"h2h", "totals"}
    assert all(row["consensus_books"] == 2 for row in result["opportunities"])
    assert all("bookmaker_key" in row and "fair_probability" in row for row in result["opportunities"])


def test_arbitrage_detects_cross_book_two_way_price():
    event = event_fixture()
    # A generous Home price at B and Away price at A produce a two-way arb.
    event[0]["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = 150
    event[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["price"] = 150
    result = analyze_markets(event, min_ev=101)
    assert result["arbitrage"]
    arb = next(row for row in result["arbitrage"] if row["market"] == "h2h")
    assert len(arb["legs"]) == 2
    assert round(sum(leg["stake_percent"] for leg in arb["legs"]), 2) == 100


def test_spread_consensus_aligns_opposite_signed_points():
    event = event_fixture()
    for book in event[0]["bookmakers"]:
        book["markets"].append({"key": "spreads", "outcomes": [
            {"name": "Home", "point": -3.5, "price": -110},
            {"name": "Away", "point": 3.5, "price": -110}]})
    result = analyze_markets(event, min_ev=-100, min_consensus_books=2)
    spread_rows = [row for row in result["opportunities"] if row["market"] == "spreads"]
    assert spread_rows
    assert {row["point"] for row in spread_rows} == {-3.5, 3.5}


def test_arbitrage_requires_distinct_sportsbooks():
    event = [{
        "id": "same-book-only", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "a", "title": "Book A", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 110}, {"name": "Away", "price": 110}]}]},
            {"key": "b", "title": "Book B", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -300}, {"name": "Away", "price": -300}]}]},
        ],
    }]
    result = analyze_markets(event, min_ev=101)
    assert result["arbitrage"] == []


def test_soccer_h2h_requires_complete_three_way_outcome_sets_for_ev():
    event = [{
        "id": "soccer-event", "sport_key": "soccer_epl",
        "sport_title": "Soccer - English Premier League",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 150}, {"name": "Draw", "price": 220},
                {"name": "Away", "price": 180}]}]},
            {"key": "b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 140}, {"name": "Draw", "price": 230},
                {"name": "Away", "price": 190}]}]},
            {"key": "c", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 155}, {"name": "Draw", "price": 215},
                {"name": "Away", "price": 175}]}]},
            # This book omits Draw; its two-way normalization is not a
            # valid probability estimate for a soccer 1X2 market.
            {"key": "incomplete", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 150}, {"name": "Away", "price": 180}]}]},
        ],
    }]
    result = analyze_markets(event, min_ev=-100, min_consensus_books=2)
    assert result["opportunities"]
    assert "incomplete" not in {row["bookmaker_key"] for row in result["opportunities"]}
    # The board returns the best price per outcome, not every book's quote.
    assert {row["bookmaker_key"] for row in result["opportunities"]} <= {"a", "b", "c"}
    assert all(row["consensus_books"] == 2 for row in result["opportunities"])
    home = next(row for row in result["opportunities"] if row["outcome"] == "Home")
    assert home["bookmaker_key"] == "c"
    expected_fair = median([
        no_vig_probabilities([150, 220, 180])[0],
        no_vig_probabilities([140, 230, 190])[0],
    ])
    assert home["fair_probability"] == round(expected_fair, 6)
    assert result["arbitrage"] == []


def test_player_props_with_shared_lines_remain_separate_by_player():
    books = []
    for key, p1_over, p1_under, p2_over, p2_under in (
        ("a", -110, -110, -105, -115),
        ("b", -115, -105, -110, -110),
        ("c", -105, -115, -115, -105),
    ):
        books.append({
            "key": key,
            "markets": [{
                "key": "player_points",
                "outcomes": [
                    {"name": "Over", "description": "Player One", "point": 20.5, "price": p1_over},
                    {"name": "Under", "description": "Player One", "point": 20.5, "price": p1_under},
                    {"name": "Over", "description": "Player Two", "point": 20.5, "price": p2_over},
                    {"name": "Under", "description": "Player Two", "point": 20.5, "price": p2_under},
                ],
            }],
        })
    event = [{
        "id": "player-prop-event", "sport_key": "basketball_nba",
        "home_team": "Home", "away_team": "Away", "bookmakers": books,
    }]
    result = analyze_markets(event, min_ev=-100, min_consensus_books=2)
    assert {row["outcome_description"] for row in result["opportunities"]} == {
        "Player One", "Player Two"
    }
    assert len(result["opportunities"]) == 4
    assert all(row["consensus_books"] == 2 for row in result["opportunities"])
    assert result["arbitrage"] == []


def test_full_book_ev_mode_preserves_each_qualifying_quote_for_clv():
    event = event_fixture()
    best_prices = analyze_markets(event, min_ev=-100, min_consensus_books=2)
    all_quotes = analyze_markets(
        event, min_ev=-100, min_consensus_books=2, best_only=False
    )
    assert len(best_prices["opportunities"]) == 4
    assert len(all_quotes["opportunities"]) == 12
    assert {row["bookmaker_key"] for row in all_quotes["opportunities"]} == {
        "a", "b", "c"
    }
    assert all(row["consensus_books"] == 2 for row in all_quotes["opportunities"])


def test_market_analysis_ignores_boolean_american_prices():
    event = event_fixture()
    event[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = True
    result = analyze_markets(event, min_ev=-100, min_consensus_books=1)
    assert not any(
        row["event_id"] == event[0]["id"] and row["bookmaker_key"] == "a"
        and row["market"] == "h2h"
        for row in result["opportunities"]
    )
    assert all(
        leg["price"] is not True
        for row in result["arbitrage"] for leg in row["legs"]
    )

def test_market_analysis_ignores_events_without_stable_identity():
    anonymous = event_fixture()[0]
    anonymous.pop("id")
    result = analyze_markets([anonymous, None, "malformed"], min_ev=-100, min_consensus_books=1)
    assert result == {"opportunities": [], "arbitrage": []}



def test_market_analysis_skips_malformed_nested_records_and_keeps_valid_books():
    valid_event = {
        "id": "valid-neighbor", "sport_key": "americanfootball_nfl",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -110}, {"name": "Away", "price": -110},
            ]}]},
            {"key": "b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -110}, {"name": "Away", "price": -110},
            ]}]},
        ],
    }
    malformed_event = {
        "id": "malformed-event",
        "bookmakers": [
            None,
            {"key": "bad-markets", "markets": None},
            {"key": "bad-market-entry", "markets": [None, "not-a-market"]},
            {"key": "bad-outcomes", "markets": [
                {"key": "h2h", "outcomes": None},
                {"key": "h2h", "outcomes": [None, "not-an-outcome"]},
            ]},
        ],
    }

    board = market_board([malformed_event, valid_event, None, "not-an-event"])
    assert len(board) == 2
    assert {row["event_id"] for row in board} == {"valid-neighbor"}

    result = analyze_markets(
        [malformed_event, valid_event, None, "not-an-event"],
        min_ev=-100, min_consensus_books=1,
    )
    assert result["opportunities"]
    assert {row["event_id"] for row in result["opportunities"]} == {"valid-neighbor"}
    assert result["arbitrage"] == []


def test_market_analysis_accepts_non_list_top_level_input_as_empty():
    assert market_board(None) == []
    assert market_board({"id": "not-a-list"}) == []
    assert analyze_markets(None) == {"opportunities": [], "arbitrage": []}


def test_market_analysis_skips_non_scalar_identity_fields():
    malformed = [
        {
            "id": ["not", "an", "event-id"],
            "bookmakers": [],
        },
        {
            "id": "malformed-identity-fields",
            "bookmakers": [
                {"key": [], "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 110}, {"name": "Away", "price": -110},
                ]}]},
                {"key": "bad-market-key", "markets": [{"key": [], "outcomes": [
                    {"name": "Home", "price": 110}, {"name": "Away", "price": -110},
                ]}]},
                {"key": "bad-outcome-name", "markets": [{"key": "h2h", "outcomes": [
                    {"name": [], "price": 110}, {"name": {}, "price": -110},
                ]}]},
            ],
        },
    ]
    assert market_board(malformed) == []
    assert analyze_markets(malformed) == {"opportunities": [], "arbitrage": []}


def test_market_analysis_sanitizes_non_scalar_outcome_descriptions():
    event = {
        "id": "description-shape",
        "bookmakers": [
            {"key": "a", "markets": [{"key": "player_points", "outcomes": [
                {"name": "Over", "description": [], "point": 20.5, "price": -110},
                {"name": "Under", "description": {}, "point": 20.5, "price": -110},
            ]}]},
            {"key": "b", "markets": [{"key": "player_points", "outcomes": [
                {"name": "Over", "description": "Player", "point": 20.5, "price": -110},
                {"name": "Under", "description": "Player", "point": 20.5, "price": -110},
            ]}]},
        ],
    }
    # Invalid descriptions are normalized to empty scope, never used as
    # unhashable grouping keys; the malformed outcomes are safely ignored
    # for player markets rather than being conflated with a named player.
    board = market_board([event])
    assert {row["outcome_description"] for row in board} == {"Player"}
    assert {row["bookmaker_key"] for row in board} == {"b"}
    result = analyze_markets([event], min_ev=-100, min_consensus_books=1)
    assert result["opportunities"] == []
    assert result["arbitrage"] == []
