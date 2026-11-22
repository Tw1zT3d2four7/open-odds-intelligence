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


def test_provider_quota_headers_remain_visible_after_refresh_error(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    class FailingProvider:
        name = "fake"
        usage = {"requests_remaining": "0", "requests_used": "100"}

        def __init__(self, *args, **kwargs):
            pass

        def fetch_odds(self, sport):
            raise ProviderError("provider quota exhausted")

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FailingProvider)

    service = OddsService(OddsDatabase(tmp_path / "provider-error.sqlite3"))
    result = service.refresh()
    assert result["ok"] is False
    assert service.status()["provider_usage"]["requests_remaining"] == "0"


def test_player_props_cooldown_survives_service_restart(tmp_path, monkeypatch):
    calls = []

    class FakeProvider:
        name = "fake"
        usage = {}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            return []
        def fetch_player_props(self, sport, events, markets, max_events):
            calls.append(sport)
            return {
                "enabled": True, "sport": sport, "events_requested": 0,
                "events_with_props": 0, "markets": markets.split(","),
            }

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "PLAYER_PROPS_ENABLED", True)
    monkeypatch.setattr(config, "PLAYER_PROPS_SPORT", "basketball_nba")
    monkeypatch.setattr(config, "PLAYER_PROPS_MARKETS", "player_points")
    monkeypatch.setattr(config, "PLAYER_PROPS_MAX_EVENTS", 5)
    monkeypatch.setattr(config, "PLAYER_PROPS_REFRESH_INTERVAL_SECONDS", 14400)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FakeProvider)

    database_path = tmp_path / "player-props-cooldown.sqlite3"
    first_service = OddsService(OddsDatabase(database_path))
    assert first_service.refresh()["ok"] is True
    assert calls == ["basketball_nba"]

    restarted_service = OddsService(OddsDatabase(database_path))
    assert restarted_service.refresh()["ok"] is True
    assert calls == ["basketball_nba"]
    props_status = restarted_service.status()["player_props"]
    assert props_status["enabled"] is True
    assert props_status["next_refresh_seconds"] > 0


def test_provider_quota_diagnostics_persist_across_service_restart(tmp_path, monkeypatch):
    class QuotaProvider:
        name = "fake"
        usage = {"requests_remaining": "77", "requests_used": "23"}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            return []

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr("app.service.TheOddsAPIProvider", QuotaProvider)

    database_path = tmp_path / "provider-usage.sqlite3"
    first_service = OddsService(OddsDatabase(database_path))
    assert first_service.refresh()["ok"] is True

    restarted_service = OddsService(OddsDatabase(database_path))
    assert restarted_service.status()["provider_usage"] == {
        "requests_remaining": "77", "requests_used": "23",
    }

    # A rotated key must not inherit the previous account's quota counters.
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "DifferentCaseSensitiveKey")
    rotated_key_service = OddsService(OddsDatabase(database_path))
    assert rotated_key_service.status()["provider_usage"] == {}


def test_service_clv_capture_records_each_book_not_only_best_price(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    commence = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    event = {
        "id": "clv-all-books", "sport_key": "basketball_nba",
        "sport_title": "Basketball", "home_team": "Home", "away_team": "Away",
        "commence_time": commence,
        "bookmakers": [
            {"key": "a", "title": "Book A", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 120}, {"name": "Away", "price": -140}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": -110},
                    {"name": "Under", "point": 44.5, "price": -110}]},
            ]},
            {"key": "b", "title": "Book B", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 130}, {"name": "Away", "price": -150}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": 120},
                    {"name": "Under", "point": 44.5, "price": -140}]},
            ]},
            {"key": "c", "title": "Book C", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 140}, {"name": "Away", "price": -160}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 44.5, "price": 125},
                    {"name": "Under", "point": 44.5, "price": -145}]},
            ]},
        ],
    }

    class FakeProvider:
        name = "fake"
        usage = {}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            return [event]

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "PLAYER_PROPS_ENABLED", False)
    monkeypatch.setattr(config, "CLV_MIN_EV_PERCENT", -100)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FakeProvider)

    database = OddsDatabase(tmp_path / "clv-all-books.sqlite3")
    service = OddsService(database)
    assert service.refresh()["ok"] is True
    report = service.clv_report("basketball_nba")
    assert report["summary"]["total"] == 12
    assert {entry["bookmaker_key"] for entry in report["entries"]} == {"a", "b", "c"}


def test_health_reports_per_sport_freshness_and_missing_feeds(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    database = OddsDatabase(tmp_path / "freshness.sqlite3")
    database.save_snapshot(
        "basketball_nba", [], "test",
        (now - timedelta(hours=1)).isoformat(),
    )
    database.save_snapshot(
        "icehockey_nhl", [], "test",
        (now - timedelta(hours=8)).isoformat(),
    )
    monkeypatch.setattr(config, "ODDS_SPORTS", [
        "basketball_nba", "icehockey_nhl", "baseball_mlb",
    ])
    monkeypatch.setattr(config, "ODDS_DATA_STALE_AFTER_SECONDS", 6 * 60 * 60)

    status = OddsService(database).status()
    assert status["data_freshness"] == "stale"
    by_sport = {row["sport"]: row for row in status["sport_freshness"]}
    assert by_sport["basketball_nba"]["freshness"] == "current"
    assert by_sport["icehockey_nhl"]["freshness"] == "stale"
    assert by_sport["baseball_mlb"]["freshness"] == "unavailable"
    assert by_sport["baseball_mlb"]["last_updated"] is None


def test_health_reports_incomplete_when_configured_sport_has_no_snapshot(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    database = OddsDatabase(tmp_path / "freshness-partial.sqlite3")
    database.save_snapshot(
        "basketball_nba", [], "test", datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba", "baseball_mlb"])
    monkeypatch.setattr(config, "ODDS_DATA_STALE_AFTER_SECONDS", 6 * 60 * 60)

    status = OddsService(database).status()
    assert status["data_freshness"] == "partial"



def test_provider_refresh_retries_transient_failure_with_bounded_backoff(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    calls = []
    sleeps = []

    class FlakyProvider:
        name = "fake"
        usage = {}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            calls.append(sport)
            if len(calls) == 1:
                raise ProviderError("temporary upstream failure", status_code=503, retryable=True)
            return []

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "PROVIDER_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(config, "PROVIDER_RETRY_BASE_DELAY_SECONDS", 0.5)
    monkeypatch.setattr(config, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 2.0)
    monkeypatch.setattr("app.service.time.sleep", sleeps.append)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FlakyProvider)

    service = OddsService(OddsDatabase(tmp_path / "provider-retry.sqlite3"))
    result = service.refresh()
    assert result["ok"] is True
    assert calls == ["basketball_nba", "basketball_nba"]
    assert sleeps == [0.5]


def test_provider_refresh_never_retries_quota_rate_limit(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    calls = []

    class RateLimitedProvider:
        name = "fake"
        usage = {"requests_remaining": "0"}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            calls.append(sport)
            raise ProviderError(
                "rate limit reached", status_code=429, retry_after="1",
                retryable=True,
            )

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba", "icehockey_nhl"])
    monkeypatch.setattr(config, "PROVIDER_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", RateLimitedProvider)

    service = OddsService(OddsDatabase(tmp_path / "provider-429.sqlite3"))
    result = service.refresh()
    assert result["ok"] is False
    assert calls == ["basketball_nba"]
    assert service.status()["provider_usage"]["requests_remaining"] == "0"



def test_retry_after_longer_than_budget_defers_retry(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    calls = []
    class SlowDownProvider:
        name = "fake"
        usage = {}
        def __init__(self, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            calls.append(sport)
            raise ProviderError(
                "temporary upstream slowdown", status_code=503,
                retry_after="30", retryable=True,
            )

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "CaseSensitiveKey")
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "PROVIDER_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 5.0)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", SlowDownProvider)

    service = OddsService(OddsDatabase(tmp_path / "retry-after.sqlite3"))
    result = service.refresh()
    assert result["ok"] is False
    assert calls == ["basketball_nba"]



def test_provider_refresh_rotates_to_next_key_after_quota_exhaustion(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    calls = []

    class MultiKeyProvider:
        name = "the_odds_api"

        def __init__(self, api_key, *args, **kwargs):
            self.api_key = api_key
            self.usage = {}

        def fetch_odds(self, sport):
            calls.append((self.api_key, sport))
            if self.api_key == "First-Case-Sensitive-Key":
                self.usage = {"requests_remaining": "0"}
                raise ProviderError(
                    "Odds provider HTTP 401: OUT_OF_USAGE_CREDITS",
                    status_code=401,
                )
            self.usage = {"requests_remaining": "42"}
            return []

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "")
    monkeypatch.setattr(
        config, "THE_ODDS_API_KEYS",
        "account-a|First-Case-Sensitive-Key,account-b|Second-Case-Sensitive-Key",
        raising=False,
    )
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr("app.service.TheOddsAPIProvider", MultiKeyProvider)

    service = OddsService(OddsDatabase(tmp_path / "multi-key-rotation.sqlite3"))
    result = service.refresh()

    assert result["ok"] is True
    assert calls == [
        ("First-Case-Sensitive-Key", "basketball_nba"),
        ("Second-Case-Sensitive-Key", "basketball_nba"),
    ]
    key_status = service.status()["provider_keys"]["the_odds_api"]
    assert key_status["configured_keys"] == 2
    assert key_status["available_keys"] == 1
    assert [item["status"] for item in key_status["keys"]] == [
        "quota_cooldown", "available"
    ]


def test_provider_refresh_fails_cleanly_when_every_key_is_exhausted(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    calls = []

    class ExhaustedProvider:
        name = "the_odds_api"

        def __init__(self, api_key, *args, **kwargs):
            self.api_key = api_key
            self.usage = {"requests_remaining": "0"}

        def fetch_odds(self, sport):
            calls.append(self.api_key)
            raise ProviderError(
                "quota exceeded", status_code=429, retry_after="600",
                retryable=True,
            )

    monkeypatch.setattr(config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "")
    monkeypatch.setattr(config, "THE_ODDS_API_KEYS", "account-a|QuotaKeyA,account-b|QuotaKeyB", raising=False)
    monkeypatch.setattr(config, "ODDS_SPORTS", ["basketball_nba", "icehockey_nhl"])
    monkeypatch.setattr("app.service.TheOddsAPIProvider", ExhaustedProvider)

    service = OddsService(OddsDatabase(tmp_path / "all-keys-exhausted.sqlite3"))
    result = service.refresh()

    assert result["ok"] is False
    assert len(calls) == 2
    assert "quota exceeded" in result["error"]
    assert service.status()["provider_keys"]["the_odds_api"]["available_keys"] == 0


def test_simultaneous_providers_merge_books_and_markets(tmp_path, monkeypatch):
    from app.service import OddsService
    from app.database import OddsDatabase

    event_time = "2026-11-23T20:00:00Z"
    calls = []

    class FakeRundown:
        name = "therundown"
        usage = {"datapoints_remaining": "1000"}
        def __init__(self, api_key, **kwargs):
            assert api_key == "Rundown-Key"
        def fetch_odds(self, sport):
            calls.append(("therundown", sport))
            return [{
                "id": "rundown-event", "sport_key": sport, "sport_title": "NFL",
                "commence_time": event_time, "home_team": "Home", "away_team": "Away",
                "bookmakers": [{
                    "key": "draftkings", "title": "DraftKings",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Home", "price": -110, "updated_at": event_time}
                    ]}],
                }],
            }]

    class FakeOddsAPI:
        name = "the_odds_api"
        usage = {"requests_remaining": "20"}
        def __init__(self, api_key, *args, **kwargs):
            assert api_key == "OddsAPI-Key"
        def fetch_odds(self, sport):
            calls.append(("the_odds_api", sport))
            return [{
                "id": "odds-api-event", "sport_key": sport, "sport_title": "NFL",
                "commence_time": event_time, "home_team": "Home", "away_team": "Away",
                "bookmakers": [{
                    "key": "fanduel", "title": "FanDuel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Home", "price": -105, "updated_at": event_time}
                    ]}],
                }],
            }]

    monkeypatch.setattr(config, "ODDS_PROVIDER", "therundown")
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True, raising=False)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["therundown", "the_odds_api"], raising=False)
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "Rundown-Key")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEYS", "", raising=False)
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "OddsAPI-Key")
    monkeypatch.setattr(config, "THE_ODDS_API_KEYS", "", raising=False)
    monkeypatch.setattr(config, "ODDS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "PLAYER_PROPS_ENABLED", False)
    monkeypatch.setattr("app.service.TheRundownProvider", FakeRundown)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", FakeOddsAPI)

    database = OddsDatabase(tmp_path / "multi-provider.sqlite3")
    service = OddsService(database)
    result = service.refresh()

    assert result["ok"] is True
    assert result["partial"] is False
    assert calls == [
        ("therundown", "americanfootball_nfl"),
        ("the_odds_api", "americanfootball_nfl"),
    ]
    stored = database.latest_events("americanfootball_nfl")
    assert len(stored) == 1
    assert {book["key"] for book in stored[0]["bookmakers"]} == {
        "draftkings", "fanduel"
    }
    assert stored[0]["id"] == "rundown-event"
    sources = {
        outcome.get("source_provider")
        for book in stored[0]["bookmakers"]
        for market in book["markets"]
        for outcome in market["outcomes"]
    }
    assert sources == {"therundown", "the_odds_api"}
    assert service.status()["providers"] == ["therundown", "the_odds_api"]


def test_multi_provider_refresh_keeps_successful_source_when_other_fails(tmp_path, monkeypatch):
    from app.providers.the_odds_api import ProviderError

    class GoodRundown:
        name = "therundown"
        usage = {}
        def __init__(self, api_key, **kwargs):
            pass
        def fetch_odds(self, sport):
            return [{
                "id": "rundown-only", "sport_key": sport, "sport_title": "NFL",
                "commence_time": "2026-11-23T20:00:00Z",
                "home_team": "Home", "away_team": "Away",
                "bookmakers": [{
                    "key": "draftkings", "title": "DraftKings",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Home", "price": -110}
                    ]}],
                }],
            }]

    class DownOddsAPI:
        name = "the_odds_api"
        usage = {}
        def __init__(self, api_key, *args, **kwargs):
            pass
        def fetch_odds(self, sport):
            raise ProviderError("temporary outage", status_code=503, retryable=False)

    monkeypatch.setattr(config, "ODDS_PROVIDER", "therundown")
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True, raising=False)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["therundown", "the_odds_api"], raising=False)
    monkeypatch.setattr(config, "THERUNDOWN_API_KEY", "Rundown-Key")
    monkeypatch.setattr(config, "THERUNDOWN_API_KEYS", "", raising=False)
    monkeypatch.setattr(config, "THE_ODDS_API_KEY", "OddsAPI-Key")
    monkeypatch.setattr(config, "THE_ODDS_API_KEYS", "", raising=False)
    monkeypatch.setattr(config, "ODDS_SPORTS", ["americanfootball_nfl"])
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "PROVIDER_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("app.service.TheRundownProvider", GoodRundown)
    monkeypatch.setattr("app.service.TheOddsAPIProvider", DownOddsAPI)

    database = OddsDatabase(tmp_path / "multi-provider-partial.sqlite3")
    service = OddsService(database)
    result = service.refresh()

    assert result["ok"] is True
    assert result["partial"] is True
    assert result["provider_results"]["americanfootball_nfl"]["providers_succeeded"] == [
        "therundown"
    ]
    assert "the_odds_api" in result["provider_errors"][0]
    assert len(database.latest_events("americanfootball_nfl")) == 0
