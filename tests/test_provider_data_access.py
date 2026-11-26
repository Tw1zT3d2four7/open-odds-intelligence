from app.database import OddsDatabase
from app.markets import analyze_markets
from app.providers.merge import merge_provider_events
from app.service import OddsService


def _event(provider="feed", access_class="free", event_id="event-1"):
    return {
        "id": event_id,
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-12-01T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [{
            "key": "book",
            "title": "Book",
            "markets": [{
                "key": "h2h",
                "outcomes": [{
                    "name": "Home",
                    "price": -110,
                    "source_provider": provider,
                    "source_account_group": "default",
                    "source_access_class": access_class,
                    "size": {"max": 1000},
                    "size_source_provider": "size-feed",
                    "size_source_account_group": "default",
                    "size_source_access_class": "premium",
                }],
            }],
        }],
    }


def test_provider_merge_assigns_explicit_access_class_and_fails_closed():
    free = merge_provider_events(
        [("free-feed", [_event("free-feed")])],
        provider_access_classes={"free-feed": "free"},
    )
    premium = merge_provider_events(
        [("paid-feed", [_event("paid-feed")])],
        provider_access_classes={"paid-feed": "premium"},
    )
    unknown = merge_provider_events(
        [("unclassified-feed", [_event("unclassified-feed")])],
        provider_access_classes={},
    )

    assert free[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["source_access_class"] == "free"
    assert premium[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["source_access_class"] == "premium"
    assert unknown[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["source_access_class"] == "restricted"


def test_source_access_class_survives_sqlite_snapshot_and_history(tmp_path):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    event = _event("feed", "premium")
    db.save_snapshot("basketball_nba", [event], provider="feed")

    restored = db.latest_events("basketball_nba")
    outcome = restored[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["source_access_class"] == "premium"

    free_history = db.history("event-1", "h2h", "Home", bookmaker_key="book", include_premium=False)
    paid_history = db.history(
        "event-1", "h2h", "Home", bookmaker_key="book", include_premium=True
    )
    assert free_history == []
    assert len(paid_history) == 1
    assert paid_history[0]["source_access_class"] == "premium"


def test_service_cache_is_separated_between_free_and_premium_scopes(tmp_path):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [
        _event("free-feed", "free", "event-1-free"),
        _event("paid-feed", "premium", "event-0-premium"),
    ])
    service = OddsService(database=db)

    free = service.events("basketball_nba", limit=1, include_premium=False)
    paid = service.events("basketball_nba", include_premium=True)
    legacy = service.events("basketball_nba", include_premium=None)

    assert [event["id"] for event in free] == ["event-1-free"]
    assert {event["id"] for event in paid} == {"event-1-free", "event-0-premium"}
    assert {event["id"] for event in legacy} == {"event-1-free", "event-0-premium"}

def test_premium_size_metadata_is_not_exposed_with_free_quote(tmp_path):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [_event("free-feed", "free")])
    service = OddsService(database=db)

    free_outcome = service.events("basketball_nba", include_premium=False)[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    paid_outcome = service.events("basketball_nba", include_premium=True)[0]["bookmakers"][0]["markets"][0]["outcomes"][0]

    assert "size" not in free_outcome
    assert "size_source_provider" not in free_outcome
    assert paid_outcome["size"] == {"max": 1000}
    assert paid_outcome["size_source_access_class"] == "premium"

def test_premium_provider_polling_is_disabled_by_default(tmp_path, monkeypatch):
    from app import config

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    service = OddsService(database=db)
    monkeypatch.setattr(config, "ODDS_PREMIUM_PROVIDER_POLLING_ENABLED", False)
    monkeypatch.setattr(config, "ODDS_PROVIDER_ACCESS_CLASSES", {"paid-feed": "premium"})
    monkeypatch.setattr(service, "active_provider_names", lambda: ["paid-feed"])

    result = service._refresh_locked()

    assert result["ok"] is False
    assert "No configured API keys" in result["error"]

def test_free_quote_is_retained_when_premium_source_has_same_selection(tmp_path):
    free_event = _event("free-feed", "free")
    paid_event = _event("paid-feed", "premium")
    free_event["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-25T10:00:00Z"
    paid_event["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-25T11:00:00Z"
    merged = merge_provider_events(
        [("free-feed", [free_event]), ("paid-feed", [paid_event])],
        provider_access_classes={"free-feed": "free", "paid-feed": "premium"},
    )
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", merged)
    service = OddsService(database=db)

    free_outcome = service.events("basketball_nba", include_premium=False)[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    paid_outcome = service.events("basketball_nba", include_premium=True)[0]["bookmakers"][0]["markets"][0]["outcomes"][0]

    assert free_outcome["source_provider"] == "free-feed"
    assert paid_outcome["source_provider"] == "paid-feed"

def test_clv_history_is_scoped_and_legacy_rows_fail_closed(tmp_path):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    base = {
        "sport": "NBA",
        "sport_key": "basketball_nba",
        "matchup": "Away @ Home",
        "commence_time": "2099-12-01T00:00:00Z",
        "market": "h2h",
        "outcome": "Home",
        "bookmaker": "Book",
        "price": -110,
        "fair_probability": 0.55,
        "ev_percent": 5.0,
        "source_provider": "the_odds_api",
        "source_account_group": "default",
    }
    for event_id, access_class in (
        ("free-entry", "free"),
        ("premium-entry", "premium"),
        ("legacy-entry", "restricted"),
    ):
        db.record_clv_candidates([{
            **base,
            "event_id": event_id,
            "bookmaker_key": event_id,
            "source_access_class": access_class,
        }], observed_at="2026-11-25T12:00:00Z")

    free = db.clv_report(include_premium=False)
    paid = db.clv_report(include_premium=True)
    legacy = db.clv_report()

    assert {row["event_id"] for row in free["entries"]} == {"free-entry"}
    assert {row["event_id"] for row in paid["entries"]} == {"free-entry", "premium-entry"}
    assert {row["event_id"] for row in legacy["entries"]} == {
        "free-entry", "premium-entry", "legacy-entry"
    }

def test_derived_ev_inherits_most_restrictive_consensus_source():
    event = {
        "id": "derived-event",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2099-12-01T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [],
    }
    for book, access_class, home_price, away_price in (
        ("free-book", "free", 150, -170),
        ("premium-book", "premium", 180, -210),
        ("peer-one", "free", 160, -180),
        ("peer-two", "free", 155, -175),
    ):
        event["bookmakers"].append({
            "key": book,
            "title": book,
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Home", "price": home_price, "source_access_class": access_class},
                    {"name": "Away", "price": away_price, "source_access_class": access_class},
                ],
            }],
        })

    result = analyze_markets([event], min_ev=-100, min_consensus_books=1, best_only=False)
    assert result["opportunities"]
    assert all(row["source_access_class"] == "premium" for row in result["opportunities"])

def test_route_entitlement_sets_free_or_paid_source_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import config
    from app.entitlements import require_product_entitlement

    class Store:
        def __init__(self, premium):
            self.premium = premium

        def has_subscriber_entitlement(self, identity_id, entitlement):
            return entitlement == "odds" or (entitlement == "premium_data" and self.premium)

    monkeypatch.setattr(config, "AUTH_MODE", "identity")
    for premium in (False, True):
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(identity_store=Store(premium))),
            url=SimpleNamespace(path="/api/odds"),
            state=SimpleNamespace(),
        )
        require_product_entitlement(request, session={"identity_id": "subscriber"})
        assert request.state.include_premium_data is premium

def test_unclassified_provider_is_never_polled_even_when_premium_polling_is_enabled(
    tmp_path, monkeypatch
):
    from app import config

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    service = OddsService(database=db)
    monkeypatch.setattr(config, "ODDS_PREMIUM_PROVIDER_POLLING_ENABLED", True)
    monkeypatch.setattr(config, "ODDS_PROVIDER_ACCESS_CLASSES", {"oddize": "restricted"})
    monkeypatch.setattr(service, "active_provider_names", lambda: ["oddize"])

    result = service._refresh_locked()

    assert result["ok"] is False
    assert "No configured API keys" in result["error"]



def test_internal_clv_event_view_preserves_same_tier_account_groups(tmp_path):
    first = _event("feed", "free", "account-groups")
    second = _event("feed", "free", "account-groups")
    first["bookmakers"][0]["markets"][0]["outcomes"][0]["source_account_group"] = "free-a"
    second["bookmakers"][0]["markets"][0]["outcomes"][0]["source_account_group"] = "free-b"
    first["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-26T01:00:00Z"
    second["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-26T02:00:00Z"
    merged = merge_provider_events(
        [("feed", [first]), ("feed", [second])],
        provider_access_classes={"feed": "free"},
        provider_account_access_classes={
            "feed": {"free-a": "free", "free-b": "free"}
        },
    )
    db = OddsDatabase(tmp_path / "clv-account-view.sqlite3")
    db.save_snapshot("basketball_nba", merged)
    service = OddsService(database=db)

    subscriber_view = service.events("basketball_nba", include_premium=False)
    clv_view = service.events(
        "basketball_nba", include_premium=False, preserve_account_groups=True
    )

    assert len(subscriber_view[0]["bookmakers"][0]["markets"][0]["outcomes"]) == 1
    assert {
        row["source_account_group"]
        for row in clv_view[0]["bookmakers"][0]["markets"][0]["outcomes"]
    } == {"free-a", "free-b"}
