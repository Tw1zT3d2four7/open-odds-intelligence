import json

from app.limits import MarketLimitsMonitor, OddsLinkClient, SPORT_MAP


def fixture(amount=25000, size_kind="posted_limit"):
    return [{
        "id": "nfl:fixture:2026-12-01",
        "commence_time": "2026-12-01T18:00:00Z",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [{
            "key": "circa", "title": "Circa Sports",
            "markets": [{
                "key": "spreads",
                "outcomes": [
                    {"name": "Home", "price": -110, "point": -3.5,
                     "size": {"kind": size_kind, "amount": amount}},
                    {"name": "Away", "price": -110, "point": 3.5,
                     "size": {"kind": size_kind, "amount": amount}},
                ],
            }],
        }],
    }]


def test_extract_only_explicit_posted_market_limits():
    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", fixture(), "2026-11-18T12:00:00Z"
    )
    assert len(rows) == 2
    assert rows[0][6] == "circa"
    assert rows[0][12] == 25000
    assert MarketLimitsMonitor._extract(
        "americanfootball_nfl", fixture(size_kind="resting_depth"),
        "2026-11-18T12:00:00Z"
    ) == []


def test_limit_refresh_persists_observations_and_detects_changes(tmp_path, monkeypatch):
    import app.limits as module
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(module.config, "LIMITS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(module.config, "DISCORD_WEBHOOK_URL", "")
    monitor = MarketLimitsMonitor(tmp_path / "limits.sqlite3")

    state = {"calls": 0}
    class FakeClient:
        usage = {"requests_remaining": "999"}
        def __init__(self, key):
            assert key == "CaseSensitiveKey"
        def fetch(self, sport):
            assert sport == "nfl"
            state["calls"] += 1
            return fixture(25000 if state["calls"] == 1 else 50000)

    monkeypatch.setattr(module, "OddsLinkClient", FakeClient)
    first = monitor.refresh()
    second = monitor.refresh(force=True)

    assert first["ok"] is True
    assert first["observations"] == 2
    assert second["changes"][0]["previous_limit"] == 25000
    assert second["changes"][0]["limit_amount"] == 50000
    assert second["changes"][0]["direction"] == "increased"
    current = monitor.current()
    assert len(current) == 2
    history = monitor.history(
        "nfl:fixture:2026-12-01", "circa", "spreads", "Home", -3.5
    )
    assert [row["limit_amount"] for row in history] == [50000, 25000]


def test_sport_mapping_covers_default_configured_leagues():
    assert SPORT_MAP["americanfootball_nfl"] == "nfl"
    assert SPORT_MAP["basketball_nba"] == "nba"
    assert SPORT_MAP["icehockey_nhl"] == "nhl"
    assert SPORT_MAP["baseball_mlb"] == "mlb"


def test_limit_changes_are_reconstructed_from_persisted_snapshots(tmp_path, monkeypatch):
    import app.limits as module
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(module.config, "LIMITS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(module.config, "DISCORD_WEBHOOK_URL", "")
    monitor = MarketLimitsMonitor(tmp_path / "limit-changes.sqlite3")

    state = {"calls": 0}
    class FakeClient:
        usage = {}
        def __init__(self, key):
            assert key == "CaseSensitiveKey"
        def fetch(self, sport):
            state["calls"] += 1
            return fixture([25000, 50000, 40000, 40000][state["calls"] - 1])

    monkeypatch.setattr(module, "OddsLinkClient", FakeClient)
    for _ in range(4):
        monitor.refresh(force=True)

    increases = monitor.changes(direction="increased")
    decreases = monitor.changes(direction="decreased")
    all_changes = monitor.changes(direction="all")
    assert len(increases) == 2
    assert len(decreases) == 2
    assert len(all_changes) == 4
    assert increases[0]["previous_limit"] == 25000
    assert increases[0]["limit_amount"] == 50000
    assert increases[0]["change_amount"] == 25000
    assert increases[0]["change_percent"] == 100.0
    assert decreases[0]["previous_limit"] == 50000
    assert decreases[0]["limit_amount"] == 40000
    assert len(monitor.changes(direction="increased", limit=1)) == 1


def test_limit_refresh_stops_after_provider_quota_error(tmp_path, monkeypatch):
    import app.limits as module
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(module.config, "LIMITS_SPORTS", [
        "americanfootball_nfl", "basketball_nba",
    ])
    calls = []

    class QuotaClient:
        usage = {}
        def __init__(self, key):
            assert key == "CaseSensitiveKey"
        def fetch(self, sport):
            calls.append(sport)
            raise module.LimitsError("OUT_OF_USAGE_CREDITS: quota exceeded")

    monkeypatch.setattr(module, "OddsLinkClient", QuotaClient)
    database = tmp_path / "quota-stop.sqlite3"
    monitor = MarketLimitsMonitor(database)
    result = monitor.refresh()
    assert result["ok"] is False
    assert calls == ["nfl"]
    assert "OUT_OF_USAGE_CREDITS" in result["error"]
    restarted = MarketLimitsMonitor(database)
    assert "OUT_OF_USAGE_CREDITS" in restarted.status()["last_error"]


def test_market_limits_cooldown_survives_monitor_restart(tmp_path, monkeypatch):
    import app.limits as module
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(module.config, "LIMITS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(module.config, "LIMITS_REFRESH_INTERVAL_SECONDS", 14400)
    calls = []
    keys = []

    class FakeClient:
        usage = {"requests_remaining": "999"}
        def __init__(self, key):
            keys.append(key)
        def fetch(self, sport):
            calls.append(sport)
            return fixture()

    monkeypatch.setattr(module, "OddsLinkClient", FakeClient)
    database = tmp_path / "persistent-cooldown.sqlite3"
    first_monitor = MarketLimitsMonitor(database)
    first = first_monitor.refresh()
    assert first["ok"] is True
    assert len(calls) == 1

    restarted_monitor = MarketLimitsMonitor(database)
    second = restarted_monitor.refresh()
    assert second["skipped"] is True
    assert second["reason"] == "refresh_cooldown"
    assert second["next_refresh_seconds"] > 0
    assert len(calls) == 1
    restarted_status = restarted_monitor.status()
    assert restarted_status["next_refresh_seconds"] > 0
    assert restarted_status["last_updated"] == first["updated_at"]
    assert restarted_status["requests_remaining"] == "999"

    # A new account key gets a fresh quota counter and may poll immediately.
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "RotatedCaseSensitiveKey")
    rotated_monitor = MarketLimitsMonitor(database)
    assert rotated_monitor.status()["requests_remaining"] is None
    assert rotated_monitor.status()["next_refresh_seconds"] == 0
    rotated_result = rotated_monitor.refresh()
    assert rotated_result["ok"] is True
    assert calls == ["nfl", "nfl"]
    assert keys == ["CaseSensitiveKey", "RotatedCaseSensitiveKey"]


def test_extract_skips_malformed_limit_records_and_preserves_valid_neighbors():
    events = [
        None,
        {"id": "missing-books", "bookmakers": None},
        {"id": "nfl:good", "commence_time": "2026-12-01T18:00:00Z",
         "home_team": "Home", "away_team": "Away",
         "bookmakers": [
             None,
             {"key": "bad-book", "markets": "not-a-list"},
             {"key": "circa", "title": "Circa", "markets": [
                 None,
                 {"key": "spreads", "outcomes": [
                     None,
                     {"name": "Home", "point": -3.5, "price": -110,
                      "size": {"kind": "posted_limit", "amount": 25000}},
                     {"name": "Bad Point", "point": [], "price": -110,
                      "size": {"kind": "posted_limit", "amount": 25000}},
                     {"name": "Bad Amount", "point": 3.5, "price": -110,
                      "size": {"kind": "posted_limit", "amount": True}},
                     {"name": "Bad Price", "point": 3.5, "price": [],
                      "size": {"kind": "posted_limit", "amount": 25000}},
                 ]},
             ]},
         ]},
    ]

    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
    )

    assert len(rows) == 1
    assert rows[0][2] == "nfl:good"
    assert rows[0][9] == "Home"
    assert rows[0][10] == -3.5
    assert rows[0][11] == -110
    assert rows[0][12] == 25000


def test_extract_rejects_non_list_event_collection():
    assert MarketLimitsMonitor._extract(
        "americanfootball_nfl", {"not": "a list"}, "2026-11-19T12:00:00Z"
    ) == []


def test_oddslink_client_rejects_unexpected_response_shapes(monkeypatch):
    import pytest
    import app.limits as module
    from app.limits import LimitsError

    class FakeProvider:
        usage = {}
        def __init__(self, key):
            assert key == "CaseSensitiveKey"
        def fetch_usage(self):
            return 10
        def _request(self, path, params=None):
            return {"events": "not-a-list"}

    monkeypatch.setattr(module, "OddsLinkProvider", FakeProvider)
    with pytest.raises(LimitsError, match="unexpected events response"):
        OddsLinkClient("CaseSensitiveKey").fetch("nfl")



def test_extract_rejects_invalid_american_prices():
    invalid_prices = (
        True, False, 1.5, -1.5, -99, -50, -1, 0, 1, 50, 99,
        float("nan"), float("inf"), [], {},
    )
    for invalid_price in invalid_prices:
        events = fixture()
        for outcome in events[0]["bookmakers"][0]["markets"][0]["outcomes"]:
            outcome["price"] = invalid_price
        assert MarketLimitsMonitor._extract(
            "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
        ) == []


def test_extract_accepts_american_odds_boundaries_and_missing_price():
    for valid_price in (-100, 100):
        events = fixture()
        for outcome in events[0]["bookmakers"][0]["markets"][0]["outcomes"]:
            outcome["price"] = valid_price
        rows = MarketLimitsMonitor._extract(
            "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
        )
        assert len(rows) == 2
        assert {row[11] for row in rows} == {valid_price}

    events = fixture()
    for outcome in events[0]["bookmakers"][0]["markets"][0]["outcomes"]:
        outcome["price"] = None
    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
    )
    assert len(rows) == 2
    assert {row[11] for row in rows} == {None}


def test_extract_rejects_non_scalar_event_ids():
    events = fixture()
    events[0]["id"] = {"unexpected": "object"}
    assert MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
    ) == []


def test_extract_deduplicates_identical_h2h_observations_with_null_points():
    events = fixture()
    outcomes = [
        {"name": "Home", "price": 110, "size": {"kind": "posted_limit", "amount": 25000}},
        {"name": "Away", "price": -110, "size": {"kind": "posted_limit", "amount": 25000}},
    ]
    outcomes.append(dict(outcomes[0]))
    events[0]["bookmakers"][0]["markets"].append({
        "key": "h2h", "outcomes": outcomes,
    })

    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
    )
    h2h_rows = [row for row in rows if row[8] == "h2h"]
    assert len(h2h_rows) == 2
    assert {row[9] for row in h2h_rows} == {"Home", "Away"}


def test_extract_drops_conflicting_duplicate_h2h_identity_with_null_point():
    events = fixture()
    outcomes = [
        {"name": "Home", "price": 110, "size": {"kind": "posted_limit", "amount": 25000}},
        {"name": "Away", "price": -110, "size": {"kind": "posted_limit", "amount": 25000}},
        {"name": "Home", "price": 110, "size": {"kind": "posted_limit", "amount": 50000}},
    ]
    events[0]["bookmakers"][0]["markets"].append({
        "key": "h2h", "outcomes": outcomes,
    })

    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z"
    )
    h2h_rows = [row for row in rows if row[8] == "h2h"]
    assert len(h2h_rows) == 1
    assert h2h_rows[0][9] == "Away"


def test_market_limit_discord_alert_uses_readable_embed_and_deduplicates(tmp_path, monkeypatch):
    import app.limits as module

    monkeypatch.setattr(module.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monitor = MarketLimitsMonitor(tmp_path / "limit-alert-embed.sqlite3")
    sent = []

    class FakeResponse:
        status = 204
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        sent.append((json.loads(request.data.decode()), timeout))
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    change = {
        "event_id": "limit-event", "bookmaker_key": "circa",
        "bookmaker": "Circa Sports", "matchup": "Away @ Home",
        "market": "spreads", "outcome": "Home", "point": -3.5,
        "previous_limit": 10000, "limit_amount": 25000,
        "change_amount": 15000, "change_percent": 150.0,
        "direction": "increased", "observed_at": "2026-11-22T14:00:00Z",
        "commence_time": "2026-11-23T01:00:00Z",
    }

    monitor._send_alerts([change])
    assert len(sent) == 1
    payload, timeout = sent[0]
    assert timeout == 8
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "MARKET LIMIT INCREASED"
    assert embed["description"] == "**Away @ Home**"
    assert {"Sportsbook", "Market", "Selection", "Previous posted limit",
            "New posted limit", "Change", "Game time"} <= {
        field["name"] for field in embed["fields"]
    }
    assert any("Circa Sports" in field["value"] for field in embed["fields"])
    assert any("<t:" in field["value"] for field in embed["fields"])

    monitor._send_alerts([change])
    assert len(sent) == 1


def test_ingest_events_records_oddslink_posted_limits_without_api_call(tmp_path):
    monitor = MarketLimitsMonitor(path=tmp_path / "ingest-limits.sqlite3")
    result = monitor.ingest_events([{
        "id": "oddslink-event",
        "sport_key": "americanfootball_nfl",
        "commence_time": "2026-11-23T20:00:00Z",
        "home_team": "Kansas City Chiefs",
        "away_team": "Las Vegas Raiders",
        "bookmakers": [{
            "key": "draftkings", "title": "DraftKings",
            "markets": [{
                "key": "spreads", "outcomes": [{
                    "name": "Kansas City Chiefs", "point": -3.5, "price": -110,
                    "size": {"kind": "posted_limit", "amount": 25000},
                }],
            }],
        }],
    }], observed_at="2026-11-23T18:00:00+00:00",
       usage={"requests_remaining": 900})

    assert result["ok"] is True
    assert result["source"] == "stored_oddslink_snapshot"
    assert result["observations"] == 1
    assert result["usage"]["requests_remaining"] == 900
    assert monitor.status()["last_updated"] == "2026-11-23T18:00:00+00:00"
