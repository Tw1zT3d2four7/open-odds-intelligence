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



def test_discord_sends_top_three_from_one_slate_with_timestamp(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 5.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 1.0)
    monkeypatch.setattr(config, "DISCORD_ALERT_MAX_PER_CYCLE", 3)

    events = [
        {"id": "slate-low", "commence_time": "2030-01-01T18:00:00Z"},
        {"id": "slate-mid", "commence_time": "2030-01-01T19:00:00Z"},
        {"id": "slate-high", "commence_time": "2030-01-01T20:00:00Z"},
        {"id": "slate-top", "commence_time": "2030-01-01T21:00:00Z"},
        # A much larger edge on another date must not enter this slate's top three.
        {"id": "tomorrow-best", "commence_time": "2030-01-02T18:00:00Z"},
    ]
    service.events = lambda: events
    edges = {
        "slate-low": 5.5, "slate-mid": 7.0, "slate-high": 8.5,
        "slate-top": 9.0, "tomorrow-best": 99.0,
    }

    def fake_analysis(items, min_ev, min_consensus_books):
        event_id = items[0]["id"]
        if event_id == "tomorrow-best":
            edge = edges[event_id]
        else:
            edge = edges[event_id]
        row = {
            "event_id": event_id, "market": "h2h", "outcome": "Home",
            "outcome_description": "", "point": None, "bookmaker_key": "book",
            "price": 110, "matchup": event_id, "bookmaker": "Book",
            "ev_percent": edge, "consensus_books": 3,
        }
        return {"opportunities": [row], "arbitrage": []}

    monkeypatch.setattr("app.service.analyze_markets", fake_analysis)
    monkeypatch.setattr("app.service._discord_ev_embed",
                        lambda row: {"title": row["event_id"], "fields": []})
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("app.service.urlopen", fake_urlopen)

    assert service._dispatch_alerts() == 3
    embeds = [payload["embeds"][0] for payload in sent]
    assert {embed["title"] for embed in embeds} == {
        "slate-mid", "slate-high", "slate-top"
    }
    assert all(
        any(field["name"] == "Alert timestamp" and "<t:" in field["value"]
            for field in embed["fields"])
        for embed in embeds
    )



def test_discord_alert_lifecycle_edits_started_then_verified_final(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    commence = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    fingerprint = "lifecycle-fingerprint"
    original_embed = {
        "title": "VALUE ALERT • +7.00% EV",
        "description": "**Home vs Away**",
        "fields": [{"name": "Alert timestamp", "value": "sent earlier"}],
    }
    assert db.claim_alert(
        fingerprint, "ev", "VALUE +7.00%", event_id="event-lifecycle",
        commence_time=commence,
    )
    db.mark_alert_message(fingerprint, "discord-message-123", json.dumps(original_embed))
    edits = []

    class PatchResponse(FakeResponse):
        status = 200

    def fake_urlopen(request, timeout):
        edits.append((request, timeout))
        return PatchResponse()

    monkeypatch.setattr("app.service.urlopen", fake_urlopen)

    assert service._update_discord_alert_lifecycle() == []
    assert len(edits) == 1
    request, timeout = edits[0]
    assert request.get_method() == "PATCH"
    assert request.full_url.endswith("/messages/discord-message-123")
    started_embed = json.loads(request.data.decode())["embeds"][0]
    assert started_embed["title"].startswith(
        "GAME STARTED — NO LONGER A PRE-GAME OPPORTUNITY"
    )
    assert db.pending_alert_lifecycle()[0]["lifecycle_status"] == "started"

    with db.connect() as conn:
        conn.execute(
            """INSERT INTO simulation_settlements(
               event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,
               status,source,source_record_id,source_updated_at,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("event-lifecycle", "basketball_nba", commence, "Home", "Away",
             101, 98, "final", "test-results", "test-record", None,
             datetime.now(timezone.utc).isoformat()),
        )

    assert service._update_discord_alert_lifecycle() == []
    assert len(edits) == 2
    final_embed = json.loads(edits[1][0].data.decode())["embeds"][0]
    assert final_embed["title"].startswith("FINAL — GAME COMPLETED")
    assert db.pending_alert_lifecycle() == []


def test_discord_wait_response_persists_message_id_and_embed(tmp_path, monkeypatch):
    import json

    class CreatedMessageResponse(FakeResponse):
        status = 200

        def read(self):
            return b'{"id":"discord-created-456"}'

    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [sample_event()])
    service = OddsService(db)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "ALERT_MIN_EV_PERCENT", 3.0)
    monkeypatch.setattr(config, "ALERT_MIN_ARB_PERCENT", 0.5)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return CreatedMessageResponse()

    monkeypatch.setattr("app.service.urlopen", fake_urlopen)
    assert service._dispatch_alerts() >= 1
    assert all("wait=true" in request.full_url for request in requests)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT message_id,event_id,commence_time,embed_json FROM sent_alerts "
            "WHERE message_id='discord-created-456'"
        ).fetchone()
    assert row is not None
    assert row["event_id"] == "event-1"
    assert row["commence_time"] == "2030-01-01T00:00:00Z"
    assert json.loads(row["embed_json"])["fields"]


def test_discord_final_score_sync_uses_active_multi_provider_key_pool(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.providers.the_odds_api import ProviderError

    commence = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    event = sample_event()
    event["commence_time"] = commence
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [event])

    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["the_odds_api", "therundown"])
    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEYS", "CaseSensitive-Rundown-Key,Second-Key")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "DISCORD_ALERT_RESULTS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_SYNC_ENABLED", False)
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])

    service = OddsService(db)
    used_keys = []

    class FakeScoresProvider:
        name = "therundown"
        usage = {"requests_remaining": 90}

        def __init__(self, key):
            self.key = key

        def fetch_scores(self, sport, event_dates):
            used_keys.append((self.key, sport, event_dates))
            return [{
                "event_id": "event-1", "sport_key": sport,
                "commence_time": commence, "home_team": "Home", "away_team": "Away",
                "home_score": 101, "away_score": 98, "status": "final",
                "source": "therundown", "source_record_id": "event-1",
                "source_updated_at": datetime.now(timezone.utc).isoformat(),
            }]

    monkeypatch.setattr(
        service, "_new_provider",
        lambda name, key: FakeScoresProvider(key) if name == "therundown"
        else (_ for _ in ()).throw(AssertionError(name)),
    )

    assert service.discord_results_sync_enabled()
    result = service.sync_simulation_results()
    assert result["inserted"] == 1
    assert used_keys == [("CaseSensitive-Rundown-Key", "basketball_nba", [commence[:10]])]
    assert db.has_final_settlement("event-1")
    assert service._provider_key_pools["therundown"].status()["configured_keys"] == 2


def test_discord_final_score_sync_stays_off_without_active_rundown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["the_odds_api", "oddize"])
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "unused-key")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "DISCORD_ALERT_RESULTS_SYNC_ENABLED", True)
    service = OddsService(OddsDatabase(tmp_path / "odds.sqlite3"))
    assert not service.discord_results_sync_enabled()
    result = service.sync_simulation_results()
    assert result["skipped"] is True


def test_discord_final_score_sync_respects_rundown_shared_quota_cooldown(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.providers.the_odds_api import ProviderError

    commence = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    event = sample_event()
    event["commence_time"] = commence
    db = OddsDatabase(tmp_path / "odds.sqlite3")
    db.save_snapshot("basketball_nba", [event])
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["therundown"])
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "Rundown-Key-One")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEYS", "Rundown-Key-Two")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "DISCORD_ALERT_RESULTS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_SYNC_ENABLED", False)
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    service = OddsService(db)
    calls = []

    class RateLimitedScoresProvider:
        name = "therundown"
        usage = {}

        def __init__(self, key):
            self.key = key

        def fetch_scores(self, sport, event_dates):
            calls.append(self.key)
            raise ProviderError("TheRundown HTTP 429", status_code=429, retry_after=600)

    monkeypatch.setattr(service, "_new_provider", lambda name, key: RateLimitedScoresProvider(key))
    result = service.sync_simulation_results()
    assert result["ok"] is False
    assert calls == ["Rundown-Key-One"]
    assert "HTTP 429" in result["error"]
