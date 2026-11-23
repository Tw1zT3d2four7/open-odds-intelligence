from concurrent.futures import ThreadPoolExecutor

from app.database import OddsDatabase
from app.service import OddsService


def _event(home="Home"):
    return [{
        "id": "cache-event",
        "sport_title": "Basketball",
        "commence_time": "2026-11-24T00:00:00Z",
        "home_team": home,
        "away_team": "Away",
        "bookmakers": [{
            "key": "book-a",
            "title": "Book A",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": -110},
                    {"name": "Away", "price": -110},
                ],
            }],
        }],
    }]


def test_concurrent_dashboard_reads_share_one_snapshot_reconstruction(tmp_path, monkeypatch):
    db = OddsDatabase(tmp_path / "events-cache.sqlite3")
    db.save_snapshot(
        "basketball_nba", _event(), "test", "2026-11-23T12:00:00Z"
    )
    service = OddsService(db)
    original = db.latest_events
    calls = []

    def counted(sport=None):
        calls.append(sport)
        return original(sport)

    monkeypatch.setattr(db, "latest_events", counted)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.events(), range(8)))

    assert len(calls) == 1
    assert all(result is results[0] for result in results)
    assert results[0][0]["home_team"] == "Home"


def test_snapshot_cache_is_cleared_after_a_new_refresh_snapshot(tmp_path, monkeypatch):
    db = OddsDatabase(tmp_path / "events-cache-invalidation.sqlite3")
    db.save_snapshot(
        "basketball_nba", _event(), "test", "2026-11-23T12:00:00Z"
    )
    service = OddsService(db)
    original = db.latest_events
    calls = []

    def counted(sport=None):
        calls.append(sport)
        return original(sport)

    monkeypatch.setattr(db, "latest_events", counted)

    assert service.events()[0]["home_team"] == "Home"
    assert service.events()[0]["home_team"] == "Home"
    assert len(calls) == 1

    db.save_snapshot(
        "basketball_nba", _event("New Home"), "test", "2026-11-23T12:05:00Z"
    )
    service._invalidate_events_cache()

    assert service.events()[0]["home_team"] == "New Home"
    assert len(calls) == 2
