from app.providers.merge import merge_provider_events


def _event(event_id, books, home="Home", away="Away", start="2026-11-23T20:00:00Z"):
    return {
        "id": event_id,
        "sport_key": "americanfootball_nfl",
        "sport_title": "NFL",
        "commence_time": start,
        "home_team": home,
        "away_team": away,
        "bookmakers": books,
    }


def _book(key, outcomes, market="h2h"):
    return {"key": key, "title": key.title(), "markets": [
        {"key": market, "outcomes": outcomes}
    ]}


def test_merge_adds_books_and_markets_for_same_event_with_different_provider_ids():
    rundown = _event("rundown-event", [_book("draftkings", [
        {"name": "Home", "price": -110, "updated_at": "2026-11-23T18:00:00Z"}
    ])])
    odds_api = _event("odds-api-event", [
        _book("fanduel", [{"name": "Home", "price": -105}]),
        _book("draftkings", [{"name": "Home", "price": -115,
                              "updated_at": "2026-11-23T18:05:00Z"}]),
        _book("caesars", [{"name": "Home", "price": 100}], market="player_points"),
    ])

    merged = merge_provider_events([
        ("therundown", [rundown]),
        ("the_odds_api", [odds_api]),
    ])

    assert len(merged) == 1
    assert merged[0]["id"] == "rundown-event"
    assert merged[0]["sources"] == ["therundown", "the_odds_api"]
    books = {book["key"]: book for book in merged[0]["bookmakers"]}
    assert set(books) == {"draftkings", "fanduel", "caesars"}
    selected = books["draftkings"]["markets"][0]["outcomes"][0]
    assert selected["price"] == -115
    assert selected["source_provider"] == "the_odds_api"


def test_merge_prefers_first_provider_when_quote_timestamps_are_missing():
    a = _event("a", [_book("book", [{"name": "Home", "price": -110}])])
    b = _event("b", [_book("book", [{"name": "Home", "price": -120}])])
    merged = merge_provider_events([("first", [a]), ("second", [b])])
    outcome = merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["price"] == -110
    assert outcome["source_provider"] == "first"


def test_merge_does_not_combine_reversed_home_away_or_different_kickoffs():
    primary = _event("a", [_book("book-a", [{"name": "Home", "price": -110}])])
    reversed_event = _event("b", [], home="Away", away="Home")
    different_time = _event("c", [], start="2026-11-24T20:00:00Z")
    merged = merge_provider_events([
        ("first", [primary]),
        ("second", [reversed_event, different_time]),
    ])
    assert len(merged) == 3


def test_merge_deduplicates_same_event_and_same_exact_outcome():
    event = _event("a", [_book("book", [{"name": "Home", "point": -3.5, "price": -110}])])
    duplicate = _event("a", [_book("book", [{"name": "Home", "point": -3.5, "price": -110}])])
    merged = merge_provider_events([("one", [event]), ("two", [duplicate])])
    outcomes = merged[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert len(outcomes) == 1


def test_merge_matches_oddize_team_abbreviations_to_full_provider_names():
    full_names = _event("rundown", [_book("draftkings", [
        {"name": "Boston Celtics", "price": -110}
    ])], home="Boston Celtics", away="New York Knicks")
    abbreviations = _event("oddize", [_book("bet365", [
        {"name": "BOS", "price": -105}
    ])], home="BOS", away="NYK")

    merged = merge_provider_events([
        ("therundown", [full_names]),
        ("oddize", [abbreviations]),
    ])

    assert len(merged) == 1
    assert merged[0]["home_team"] == "Boston Celtics"
    books = {book["key"]: book for book in merged[0]["bookmakers"]}
    bet365_outcome = books["bet365"]["markets"][0]["outcomes"][0]
    assert bet365_outcome["name"] == "Boston Celtics"
