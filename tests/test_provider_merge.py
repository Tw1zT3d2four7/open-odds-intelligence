from app.providers.merge import merge_provider_events, merge_provider_snapshot


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
    for outcome in outcomes:
        outcome.setdefault("source_account_group", "default")
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


def test_abbreviation_event_merge():
    full = _event('rundown', [_book('draftkings', [{'name': 'Boston Celtics', 'price': -110}])], home='Boston Celtics', away='New York Knicks')
    short = _event('oddize', [_book('bet365', [{'name': 'BOS', 'price': -105}])], home='BOS', away='NYK')
    merged = merge_provider_events([('therundown', [full]), ('oddize', [short])])
    assert len(merged) == 1
    assert merged[0]['home_team'] == 'Boston Celtics'
    books = {book['key']: book for book in merged[0]['bookmakers']}
    assert books['bet365']['markets'][0]['outcomes'][0]['name'] == 'Boston Celtics'


def test_merge_preserves_oddslink_posted_limit_when_newer_price_replaces_quote():
    oddslink = _event("oddslink", [_book("draftkings", [
        {"name": "KC", "price": -110, "point": -3.5,
         "updated_at": "2026-11-23T18:00:00Z",
         "size": {"kind": "posted_limit", "amount": 25000}, "main": True}
    ])], home="Kansas City Chiefs", away="Las Vegas Raiders")
    newer = _event("the_odds_api", [_book("draftkings", [
        {"name": "Kansas City Chiefs", "price": -105, "point": -3.5,
         "updated_at": "2026-11-23T18:05:00Z"}
    ])], home="Kansas City Chiefs", away="Las Vegas Raiders")

    merged = merge_provider_events([("oddslink", [oddslink]), ("the_odds_api", [newer])])
    outcome = merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["source_provider"] == "the_odds_api"
    assert outcome["size"] == {"kind": "posted_limit", "amount": 25000}
    assert outcome["size_source_provider"] == "oddslink"
    assert outcome["main"] is True


def test_tennis_events_merge_when_providers_reverse_participant_order():
    atp = {
        "id": "oddslink-atp", "sport_key": "tennis_atp", "sport_title": "ATP",
        "commence_time": "2026-11-23T12:00:00Z",
        "home_team": "Player A", "away_team": "Player B",
        "bookmakers": [_book("book-one", [{"name": "Player A", "price": -120}])],
    }
    odds_api = {
        "id": "odds-api-atp", "sport_key": "tennis_atp", "sport_title": "ATP",
        "commence_time": "2026-11-23T12:00:00Z",
        "home_team": "Player B", "away_team": "Player A",
        "bookmakers": [_book("book-two", [{"name": "Player A", "price": -115}])],
    }

    merged = merge_provider_events([("oddslink", [atp]), ("the_odds_api", [odds_api])])

    assert len(merged) == 1
    assert merged[0]["home_team"] == "Player A"
    assert merged[0]["away_team"] == "Player B"
    books = {book["key"]: book for book in merged[0]["bookmakers"]}
    assert set(books) == {"book-one", "book-two"}
    assert books["book-two"]["markets"][0]["outcomes"][0]["name"] == "Player A"



def test_incremental_refresh_preserves_other_provider_quotes_and_replaces_refreshed_quotes():
    original_rundown = _event("rundown-id", [
        _book("draftkings", [{"name": "Home", "price": -110}])
    ])
    original_oddize = _event("oddize-id", [
        _book("bet365", [{"name": "Home", "price": -105}])
    ])
    existing = merge_provider_events([
        ("therundown", [original_rundown]),
        ("oddize", [original_oddize]),
    ])

    refreshed_rundown = _event("rundown-id", [
        _book("draftkings", [{"name": "Home", "price": -120}]),
        _book("fanduel", [{"name": "Home", "price": -115}]),
    ])
    merged, providers = merge_provider_snapshot(
        existing,
        ["therundown"],
        [("therundown", [refreshed_rundown])],
    )

    assert providers == ["oddize", "therundown"]
    assert len(merged) == 1
    books = {book["key"]: book for book in merged[0]["bookmakers"]}
    assert set(books) == {"draftkings", "fanduel", "bet365"}
    prices = {
        book_key: book["markets"][0]["outcomes"][0]["price"]
        for book_key, book in books.items()
    }
    assert prices == {"draftkings": -120, "fanduel": -115, "bet365": -105}
    sources = {
        book_key: book["markets"][0]["outcomes"][0]["source_provider"]
        for book_key, book in books.items()
    }
    assert sources == {
        "draftkings": "therundown",
        "fanduel": "therundown",
        "bet365": "oddize",
    }


def test_successful_empty_provider_refresh_removes_only_that_providers_old_quotes():
    rundown = _event("rundown-id", [
        _book("draftkings", [{"name": "Home", "price": -110}])
    ])
    oddize = _event("oddize-id", [
        _book("bet365", [{"name": "Home", "price": -105}])
    ])
    existing = merge_provider_events([
        ("therundown", [rundown]),
        ("oddize", [oddize]),
    ])

    merged, providers = merge_provider_snapshot(
        existing,
        ["therundown"],
        [("therundown", [])],
    )

    assert providers == ["oddize", "therundown"]
    assert len(merged) == 1
    assert [book["key"] for book in merged[0]["bookmakers"]] == ["bet365"]
    assert merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["source_provider"] == "oddize"



def test_same_provider_account_groups_keep_free_and_premium_quotes_separate():
    free_event = _event("same-event", [_book("book", [
        {"name": "Home", "price": -110, "source_account_group": "free-account"}
    ])])
    premium_event = _event("same-event", [_book("book", [
        {"name": "Home", "price": -125, "source_account_group": "premium-account"}
    ])])

    merged = merge_provider_events(
        [("the_odds_api", [free_event]),
         ("the_odds_api", [premium_event])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes={
            "the_odds_api": {
                "free-account": "free",
                "premium-account": "premium",
            }
        },
    )

    outcomes = merged[0]["bookmakers"][0]["markets"][0]["outcomes"]
    by_class = {row["source_access_class"]: row for row in outcomes}
    assert set(by_class) == {"free", "premium"}
    assert by_class["free"]["price"] == -110
    assert by_class["premium"]["price"] == -125
    assert by_class["premium"]["source_account_group"] == "premium-account"


def test_unlisted_account_group_is_restricted_when_provider_overrides_exist():
    event = _event("same-event", [_book("book", [
        {"name": "Home", "price": -110, "source_account_group": "unexpected"}
    ])])

    merged = merge_provider_events(
        [("the_odds_api", [event])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes={
            "the_odds_api": {"known-free": "free"}
        },
    )

    outcome = merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["source_access_class"] == "restricted"

    legacy_event = _event("legacy-event", [_book("book", [
        {"name": "Home", "price": -110}
    ])])
    legacy_merged = merge_provider_events(
        [("the_odds_api", [legacy_event])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes={
            "the_odds_api": {"default": "free", "known-free": "free"}
        },
    )
    legacy_outcome = legacy_merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert legacy_outcome["source_access_class"] == "restricted"



def test_incremental_refresh_replaces_only_the_leased_account_group():
    free = _event("same-event", [_book("book", [
        {"name": "Home", "price": -110, "source_account_group": "free-account"}
    ])])
    premium = _event("same-event", [_book("book", [
        {"name": "Home", "price": -125, "source_account_group": "premium-account"}
    ])])
    access = {
        "the_odds_api": {"free-account": "free", "premium-account": "premium"}
    }
    existing = merge_provider_events(
        [("the_odds_api", [free]), ("the_odds_api", [premium])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes=access,
    )

    refreshed_free = _event("same-event", [_book("book", [
        {"name": "Home", "price": -105, "source_account_group": "free-account"}
    ])])
    merged, _ = merge_provider_snapshot(
        existing,
        ["the_odds_api"],
        [("the_odds_api", [refreshed_free])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes=access,
        refreshed_provider_account_groups={"the_odds_api": {"free-account"}},
    )
    outcomes = merged[0]["bookmakers"][0]["markets"][0]["outcomes"]
    by_class = {row["source_access_class"]: row for row in outcomes}
    assert by_class["free"]["price"] == -105
    assert by_class["premium"]["price"] == -125

    # An empty successful free-account refresh removes only that account's old
    # quotes; premium-account data remains available in the snapshot.
    empty_refresh, _ = merge_provider_snapshot(
        merged,
        ["the_odds_api"],
        [("the_odds_api", [])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes=access,
        refreshed_provider_account_groups={"the_odds_api": {"free-account"}},
    )
    remaining = empty_refresh[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert [row["source_access_class"] for row in remaining] == ["premium"]
    assert remaining[0]["price"] == -125



def test_same_tier_account_groups_keep_duplicate_book_quotes_independent():
    first = _event("same-event", [_book("book", [
        {"name": "Home", "price": -110, "source_account_group": "free-account-a"}
    ])])
    second = _event("same-event", [_book("book", [
        {"name": "Home", "price": -105, "source_account_group": "free-account-b"}
    ])])

    merged = merge_provider_events(
        [("the_odds_api", [first]), ("the_odds_api", [second])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes={
            "the_odds_api": {
                "free-account-a": "free",
                "free-account-b": "free",
            }
        },
    )

    outcomes = merged[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert {row["source_account_group"] for row in outcomes} == {
        "free-account-a", "free-account-b"
    }
    assert {row["price"] for row in outcomes} == {-110, -105}



def test_size_from_another_provider_without_its_account_group_is_restricted():
    event = _event("size-source", [_book("book", [
        {
            "name": "Home", "price": -110,
            "source_account_group": "free-account",
            "size": {"stake": 10},
            "size_source_provider": "oddize",
        }
    ])])

    merged = merge_provider_events(
        [("the_odds_api", [event])],
        provider_access_classes={"the_odds_api": "free", "oddize": "free"},
        provider_account_access_classes={
            "the_odds_api": {"free-account": "free"},
            "oddize": {"premium-account": "premium"},
        },
    )

    outcome = merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["source_access_class"] == "free"
    assert outcome["size_source_access_class"] == "restricted"



def test_incremental_refresh_replaces_only_one_of_two_same_tier_groups():
    first = _event("same-event", [_book("book", [
        {"name": "Home", "price": -110, "source_account_group": "free-a"}
    ])])
    second = _event("same-event", [_book("book", [
        {"name": "Home", "price": -120, "source_account_group": "free-b"}
    ])])
    access = {"the_odds_api": {"free-a": "free", "free-b": "free"}}
    existing = merge_provider_events(
        [("the_odds_api", [first]), ("the_odds_api", [second])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes=access,
    )

    refreshed = _event("same-event", [_book("book", [
        {"name": "Home", "price": -105, "source_account_group": "free-a"}
    ])])
    merged, _ = merge_provider_snapshot(
        existing,
        ["the_odds_api"],
        [("the_odds_api", [refreshed])],
        provider_access_classes={"the_odds_api": "free"},
        provider_account_access_classes=access,
        refreshed_provider_account_groups={"the_odds_api": {"free-a"}},
    )

    outcomes = merged[0]["bookmakers"][0]["markets"][0]["outcomes"]
    prices = {row["source_account_group"]: row["price"] for row in outcomes}
    assert prices == {"free-a": -105, "free-b": -120}



def test_provider_level_fallback_does_not_classify_missing_account_group_as_free():
    event = _event("legacy-without-group", [_book("book", [
        {"name": "Home", "price": -110}
    ])])
    outcome = event["bookmakers"][0]["markets"][0]["outcomes"][0]
    outcome.pop("source_account_group", None)

    merged = merge_provider_events(
        [("the_odds_api", [event])],
        provider_access_classes={"the_odds_api": "free"},
    )

    assert merged[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["source_access_class"] == "restricted"
