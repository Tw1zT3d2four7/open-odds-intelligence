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
    import json
    payloads = [json.loads(item[0].data.decode()) for item in sent]
    assert all(payload.get("allowed_mentions") == {"parse": []} for payload in payloads)
    assert all(len(payload.get("embeds", [])) == 1 for payload in payloads)
    embeds = [payload["embeds"][0] for payload in payloads]
    assert any("VALUE ALERT" in embed["title"] for embed in embeds)
    assert any("ARBITRAGE ALERT" in embed["title"] for embed in embeds)
    assert all(embed.get("description") for embed in embeds)
    assert all(embed.get("footer", {}).get("text") for embed in embeds)
    ev_embed = next(embed for embed in embeds if "VALUE ALERT" in embed["title"])
    assert {"Selection", "Sportsbook", "American odds", "Fair probability", "Expected value", "Game time"} <= {
        field["name"] for field in ev_embed["fields"]
    }
    arb_embed = next(embed for embed in embeds if "ARBITRAGE ALERT" in embed["title"])
    assert any(field["name"].startswith("LEG 1") for field in arb_embed["fields"])
    assert any("Suggested stake split" in field["value"] for field in arb_embed["fields"])
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
    assert len(calls) >= 1
    assert service.status()["alert_error"] is None


def test_one_failed_delivery_does_not_block_other_alerts_and_is_retryable(tmp_path, monkeypatch):
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [sample_event()])
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 3.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 0.5)
    calls = []

    def fail_once(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("temporary Discord failure")
        return FakeResponse()

    monkeypatch.setattr("app.service.urlopen", fail_once)
    delivered = service._dispatch_alerts()
    assert delivered >= 1
    assert len(calls) > delivered
    assert "temporary Discord failure" in service.status()["last_alert_error"]
    assert service.status()["last_alert_sent"] == delivered
    assert service.status()["last_alert_qualified"] >= delivered

    previous_calls = len(calls)
    retry_delivered = service._dispatch_alerts()
    assert retry_delivered == 1
    assert len(calls) == previous_calls + 1
    assert service.status()["last_alert_error"] is None
    assert service.status()["last_alert_sent"] == 1


def test_discord_event_time_handles_missing_and_invalid_values():
    from app.service import _discord_event_time

    assert _discord_event_time(None) == "Not supplied"
    assert _discord_event_time("not-a-date") == "Time unavailable"
    rendered = _discord_event_time("2030-01-01T00:00:00Z")
    assert "<t:" in rendered and ":F>" in rendered and ":R>" in rendered



def test_discord_rate_limit_stops_the_current_delivery_cycle(tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from email.message import Message

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [sample_event()])
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 3.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 0.5)
    calls = []

    def rate_limited(request, timeout):
        calls.append(request)
        headers = Message()
        headers["Retry-After"] = "2"
        raise HTTPError(request.full_url, 429, "Too Many Requests", headers, None)

    monkeypatch.setattr("app.service.urlopen", rate_limited)
    assert service._dispatch_alerts() == 0
    assert len(calls) == 1
    assert "HTTP 429" in service.status()["last_alert_error"]
    assert "deferred to the next check" in service.status()["last_alert_error"]
