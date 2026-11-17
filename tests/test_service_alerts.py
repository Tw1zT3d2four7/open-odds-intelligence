from app import config
from app.database import OddsDatabase
from app.service import OddsService


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def sample_event():
    return {
        "id": "event-1",
        "sport_title": "Basketball",
        "home_team": "Home",
        "away_team": "Away",
        "commence_time": "2030-01-01T00:00:00Z",
        "bookmakers": [
            {"key": "book_a", "title": "Book A", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 130}, {"name": "Away", "price": -150}]}]},
            {"key": "book_b", "title": "Book B", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -110}, {"name": "Away", "price": -110}]}]},
            {"key": "book_c", "title": "Book C", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -115}, {"name": "Away", "price": -105}]}]},
        ],
    }


def test_discord_alert_is_sent_and_deduplicated(tmp_path, monkeypatch):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [sample_event()])
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 3.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 0.5)
    sent = []

    def fake_urlopen(request, timeout):
        sent.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr("app.service.urlopen", fake_urlopen)

    first_count = service._dispatch_alerts()
    assert first_count >= 1
    assert len(sent) == first_count
    assert all(item[1] == 8 for item in sent)
    assert all(b"EV" in item[0].data or b"Arbitrage" in item[0].data for item in sent)
    assert service._dispatch_alerts() == 0
    assert len(sent) == first_count
    assert service.status()["alerts_sent"] == first_count
    assert service.status()["alerts_qualified"] >= 1
    assert service.status()["last_alert_check"] is not None


def test_discord_delivery_error_is_exposed_and_retryable(tmp_path, monkeypatch):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [sample_event()])
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 3.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 0.5)
    calls = []

    def failing_urlopen(request, timeout):
        calls.append(request)
        raise RuntimeError("Discord test failure")

    monkeypatch.setattr("app.service.urlopen", failing_urlopen)

    assert service._dispatch_alerts() == 0
    assert "Discord test failure" in service.status()["alert_error"]
    assert service.db.alert_status()["sent"] == 0
    monkeypatch.setattr("app.service.urlopen", lambda request, timeout: FakeResponse())
    retry_count = service._dispatch_alerts()
    assert retry_count >= 1
    assert len(calls) == 1
    assert service.status()["alert_error"] is None
