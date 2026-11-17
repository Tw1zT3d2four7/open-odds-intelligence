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
