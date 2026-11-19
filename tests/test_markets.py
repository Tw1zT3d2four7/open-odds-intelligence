from app.markets import market_board


def test_board_selects_best_price_only_for_identical_market_line():
    events = [{
        "id": "evt", "sport_title": "Football", "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-18T00:00:00Z", "bookmakers": [
            {"key": "a", "title": "Book A", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": -110},
                    {"name": "Over", "point": 45.5, "price": 105}
                ]}]},
            {"key": "b", "title": "Book B", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": 100},
                    {"name": "Over", "point": 45.5, "price": -115}
                ]}]}
        ]
    }]
    rows = market_board(events)
    line_445 = next(r for r in rows if r["point"] == 44.5)
    line_455 = next(r for r in rows if r["point"] == 45.5)
    assert (line_445["bookmaker_key"], line_445["price"]) == ("b", 100)
    assert (line_455["bookmaker_key"], line_455["price"]) == ("a", 105)


def test_board_keeps_market_types_separate():
    events = [{
        "id": "evt", "home_team": "H", "away_team": "A", "bookmakers": [{
            "key": "a", "title": "A", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Home", "price": -110}]},
                {"key": "spreads", "outcomes": [{"name": "Home", "point": -2.5, "price": -110}]}
            ]
        }]
    }]
    rows = market_board(events)
    assert {r["market"] for r in rows} == {"h2h", "spreads"}



def test_player_props_are_separated_by_player_description():
    from app.markets import market_board, analyze_markets

    events = [{
        "id": "props", "sport_title": "Basketball",
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "player_points", "outcomes": [
                    {"name": "Over", "description": "Player One", "point": 24.5, "price": 110},
                    {"name": "Under", "description": "Player One", "point": 24.5, "price": -130},
                    {"name": "Over", "description": "Player Two", "point": 19.5, "price": 105},
                    {"name": "Under", "description": "Player Two", "point": 19.5, "price": -125},
                ]}]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "player_points", "outcomes": [
                    {"name": "Over", "description": "Player One", "point": 24.5, "price": -110},
                    {"name": "Under", "description": "Player One", "point": 24.5, "price": -110},
                    {"name": "Over", "description": "Player Two", "point": 19.5, "price": -110},
                    {"name": "Under", "description": "Player Two", "point": 19.5, "price": -110},
                ]}]},
            {"key": "book-c", "title": "Book C", "markets": [{
                "key": "player_points", "outcomes": [
                    {"name": "Over", "description": "Player One", "point": 24.5, "price": -115},
                    {"name": "Under", "description": "Player One", "point": 24.5, "price": -105},
                    {"name": "Over", "description": "Player Two", "point": 19.5, "price": -115},
                    {"name": "Under", "description": "Player Two", "point": 19.5, "price": -105},
                ]}]},
        ],
    }]
    board = market_board(events)
    assert {(row["outcome_description"], row["point"]) for row in board} == {
        ("Player One", 24.5), ("Player Two", 19.5)
    }
    analysis = analyze_markets(events, min_ev=-100, min_consensus_books=2)
    assert {row["outcome_description"] for row in analysis["opportunities"]} == {
        "Player One", "Player Two"
    }

def test_spread_market_identities_match_across_signed_point_conventions():
    from app.markets import market_board, analyze_markets

    events = [{
        "id": "spread-signs", "sport_title": "Football",
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": -3.5, "price": 105},
                    {"name": "Away", "point": 3.5, "price": -125},
                ]}]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": 3.5, "price": -110},
                    {"name": "Away", "point": -3.5, "price": 100},
                ]}]},
            {"key": "book-c", "title": "Book C", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": 3.5, "price": -115},
                    {"name": "Away", "point": -3.5, "price": 105},
                ]}]},
        ],
    }]

    board = market_board(events)
    home = [row for row in board if row["outcome"] == "Home"]
    assert len(home) == 1
    assert home[0]["bookmaker_key"] == "book-a"
    assert home[0]["price"] == 105

    analysis = analyze_markets(events, min_ev=-100, min_consensus_books=2)
    home_a = next(row for row in analysis["opportunities"]
                  if row["outcome"] == "Home" and row["bookmaker_key"] == "book-a")
    assert home_a["consensus_books"] == 2
    assert home_a["ev_percent"] > 0



def test_market_analysis_preserves_canonical_sport_key():
    from app.markets import analyze_markets

    events = [{
        "id": "sport-key-event", "sport_key": "soccer_epl",
        "sport_title": "Soccer - English Premier League",
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "target", "title": "Target", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 150}, {"name": "Away", "price": -170},
                    {"name": "Draw", "price": 220},
                ]}]},
            {"key": "peer-a", "title": "Peer A", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 140}, {"name": "Away", "price": -160},
                    {"name": "Draw", "price": 210},
                ]}]},
            {"key": "peer-b", "title": "Peer B", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 145}, {"name": "Away", "price": -165},
                    {"name": "Draw", "price": 215},
                ]}]},
        ],
    }]
    result = analyze_markets(events, min_ev=-100, min_consensus_books=2)
    assert result["opportunities"]
    assert all(row["sport_key"] == "soccer_epl" for row in result["opportunities"])


def test_two_way_arbitrage_uses_distinct_books_and_balanced_stakes():
    from app.markets import analyze_markets

    events = [{
        "id": "arb-two-way", "sport_key": "americanfootball_nfl",
        "sport_title": "NFL", "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "home-book", "title": "Home Book", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 120},
                    {"name": "Away", "price": -140},
                ]}]},
            {"key": "away-book", "title": "Away Book", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": -140},
                    {"name": "Away", "price": 120},
                ]}]},
        ],
    }]
    result = analyze_markets(events)
    assert len(result["arbitrage"]) == 1
    arb = result["arbitrage"][0]
    assert {leg["bookmaker_key"] for leg in arb["legs"]} == {"home-book", "away-book"}
    assert sum(leg["stake_percent"] for leg in arb["legs"]) == 100
    assert arb["arb_percent"] > 0


def test_three_way_soccer_market_never_emits_two_way_arbitrage():
    from app.markets import analyze_markets

    events = [{
        "id": "soccer-three-way", "sport_key": "soccer_epl",
        "sport_title": "EPL", "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 250}, {"name": "Draw", "price": 220},
                    {"name": "Away", "price": 250},
                ]}]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 240}, {"name": "Draw", "price": 230},
                    {"name": "Away", "price": 260},
                ]}]},
        ],
    }]
    result = analyze_markets(events, min_ev=-100, min_consensus_books=1)
    assert result["opportunities"]
    assert result["arbitrage"] == []


def test_incomplete_two_way_market_is_excluded_from_ev_and_arbitrage():
    from app.markets import analyze_markets

    events = [{
        "id": "incomplete-two-way", "sport_key": "basketball_nba",
        "sport_title": "NBA", "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "h2h", "outcomes": [{"name": "Home", "price": 150}]
            }]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "h2h", "outcomes": [{"name": "Away", "price": 150}]
            }]},
            {"key": "book-c", "title": "Book C", "markets": [{
                "key": "h2h", "outcomes": [{"name": "Home", "price": 150}]
            }]},
        ],
    }]
    result = analyze_markets(events, min_ev=-100, min_consensus_books=1)
    assert result == {"opportunities": [], "arbitrage": []}


def test_missing_or_non_finite_non_h2h_lines_are_excluded_from_board_and_analysis():
    from app.markets import analyze_markets

    events = [{
        "id": "bad-lines", "sport_key": "americanfootball_nfl",
        "sport_title": "NFL", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "price": 150},
                    {"name": "Away", "point": float("nan"), "price": -170},
                ]}]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": "not-a-line", "price": 145},
                    {"name": "Away", "point": float("inf"), "price": -165},
                ]}]},
        ],
    }]

    assert market_board(events) == []
    assert analyze_markets(events, min_ev=-100, min_consensus_books=1) == {
        "opportunities": [], "arbitrage": []
    }


def test_valid_numeric_string_lines_are_canonicalized_for_analysis():
    from app.markets import analyze_markets

    events = [{
        "id": "string-lines", "sport_key": "americanfootball_nfl",
        "sport_title": "NFL", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": "-3.5", "price": 110},
                    {"name": "Away", "point": "3.5", "price": -130},
                ]}]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "spreads", "outcomes": [
                    {"name": "Home", "point": -3.5, "price": -110},
                    {"name": "Away", "point": 3.5, "price": -110},
                ]}]},
        ],
    }]
    result = analyze_markets(events, min_ev=-100, min_consensus_books=1)
    # Best-price output is one row per outcome/line, not one per book.
    assert len(result["opportunities"]) == 2
    assert {row["outcome"] for row in result["opportunities"]} == {"Home", "Away"}
    assert all(row["consensus_books"] == 1 for row in result["opportunities"])
