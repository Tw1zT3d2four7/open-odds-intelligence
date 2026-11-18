import threading

from app import config
from app.service import OddsService
from app.database import OddsDatabase
from app import limits as limits_module


def test_manual_odds_refresh_is_skipped_while_provider_request_is_in_flight(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class FakeProvider:
        name = "fake"
        usage = {}

        def __init__(self, *args, **kwargs):
            pass

        def fetch_odds(self, sport):
            calls.append(sport)
            entered.set()
            assert release.wait(timeout=2)
            return []

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FakeProvider)

    service = OddsService(OddsDatabase(tmp_path / "odds.sqlite3"))
    results = []
    worker = threading.Thread(target=lambda: results.append(service.refresh()))
    worker.start()
    assert entered.wait(timeout=2)

    skipped = service.refresh()
    assert skipped["skipped"] is True
    assert len(calls) == 1

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0]["ok"] is True


def test_manual_limits_refresh_is_skipped_while_poll_is_in_flight(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class FakeClient:
        usage = {}

        def __init__(self, key):
            assert key == "CaseSensitiveOddsLinkKey"

        def fetch(self, sport):
            calls.append(sport)
            entered.set()
            assert release.wait(timeout=2)
            return []

    monkeypatch.setattr(limits_module.config, "ODDSLINK_API_KEY", "CaseSensitiveOddsLinkKey")
    monkeypatch.setattr(limits_module.config, "LIMITS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(limits_module, "OddsLinkClient", FakeClient)

    monitor = limits_module.MarketLimitsMonitor(tmp_path / "limits.sqlite3")
    results = []
    worker = threading.Thread(target=lambda: results.append(monitor.refresh()))
    worker.start()
    assert entered.wait(timeout=2)

    skipped = monitor.refresh()
    assert skipped["skipped"] is True
    assert len(calls) == 1

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0]["ok"] is True
