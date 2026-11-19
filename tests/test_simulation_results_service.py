from datetime import datetime, timedelta, timezone

from app import config
from app.database import OddsDatabase
from app.service import OddsService


def test_service_syncs_score_only_results_and_stores_final_settlement(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    commence = (now - timedelta(hours=2)).isoformat()
    event = {
        "id": "service-result-1", "sport_title": "EPL", "commence_time": commence,
        "home_team": "Home Club", "away_team": "Away Club",
        "bookmakers": [{"key": "book", "title": "Book", "markets": [{
            "key": "totals", "outcomes": [
                {"name": "Over", "point": 2.5, "price": -110},
                {"name": "Under", "point": 2.5, "price": -110},
            ],
        }]}],
    }
    result = {
        "event_id": "service-result-1", "sport_key": "soccer_epl",
        "commence_time": commence, "home_team": "Home Club", "away_team": "Away Club",
        "home_score": 2, "away_score": 0, "status": "final",
        "source": "therundown", "source_record_id": "service-result-1",
    }
    database = OddsDatabase(tmp_path / "service.sqlite3")
    database.save_snapshot("soccer_epl", [event], provider="therundown", fetched_at=(now - timedelta(hours=3)).isoformat())
    monkeypatch.setattr(config, "SIMULATION_RESULTS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "ODDS_PROVIDER", "therundown")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "test-key")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["soccer_epl"])
    monkeypatch.setattr(config, "RUNDOWN_UTC_OFFSET_MINUTES", 0)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_RETRY_SECONDS", 3600)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_MAX_GROUPS_PER_RUN", 10)
    calls = []

    class FakeProvider:
        name = "therundown"
        usage = {"datapoints": 2}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_scores(self, sport, dates):
            calls.append((sport, dates))
            return [result]

    monkeypatch.setattr("app.service.TheRundownProvider", FakeProvider)
    service = OddsService(database=database)
    report = service.sync_simulation_results()
    assert report["inserted"] == 1
    assert len(calls) == 1
    assert len(database.simulation_settlements()) == 1
    second = service.sync_simulation_results()
    assert second["groups_checked"] == 0
    assert len(calls) == 1


def test_service_score_sync_is_disabled_for_non_rundown_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SIMULATION_RESULTS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "test-key")
    service = OddsService(database=OddsDatabase(tmp_path / "service.sqlite3"))
    result = service.sync_simulation_results()
    assert result["skipped"] is True
    assert service._simulation_results_status["enabled"] is False