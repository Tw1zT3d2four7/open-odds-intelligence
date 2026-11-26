from app.database import OddsDatabase
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
                    "source_access_class": access_class,
                    "size": {"max": 1000},
                    "size_source_provider": "size-feed",
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

    assert [event["id"] for event in free] == ["event-1-free"]
    assert {event["id"] for event in paid} == {"event-1-free", "event-0-premium"}

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
