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
    second = monitor.refresh()

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
        monitor.refresh()

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
    monitor = MarketLimitsMonitor(tmp_path / "quota-stop.sqlite3")
    result = monitor.refresh()
    assert result["ok"] is False
    assert calls == ["nfl"]
    assert "OUT_OF_USAGE_CREDITS" in result["error"]
