from app.markets import analyze_markets


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
    assert {row["bookmaker_key"] for row in result["opportunities"]} == {"a", "b", "c"}
    assert all(row["consensus_books"] == 2 for row in result["opportunities"])
    assert result["arbitrage"] == []
