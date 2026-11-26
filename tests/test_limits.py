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


def test_standalone_limits_client_rotates_to_another_oddslink_account(monkeypatch):
    import app.limits as module
    from app.providers.the_odds_api import ProviderError

    calls = []
    class FakeProvider:
        def __init__(self, key):
            self.key = key
            self.usage = {"period_resets_at": "2026-12-01T00:00:00Z"}
            calls.append(("init", key))
        def fetch_usage(self):
            if self.key == "Key-One":
                return 0
            self.usage = {"requests_remaining": 50}
            return 50
        def _seconds_until(self, value):
            return 86400
        def _request(self, path, params=None):
            calls.append(("request", self.key, path))
            return []

    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "")
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEYS", "primary|Key-One,backup|Key-Two")
    monkeypatch.setattr(module, "OddsLinkProvider", FakeProvider)

    client = module.OddsLinkClient()
    assert client.fetch("nfl") == []
    assert [item[1] for item in calls if item[0] == "init"] == ["Key-One", "Key-Two"]
    assert client.usage["requests_remaining"] == 50


def test_market_limit_provenance_is_persisted_and_scoped_for_current_and_history(tmp_path):
    monitor = MarketLimitsMonitor(path=tmp_path / "scoped-limit-provenance.sqlite3")

    def event(event_id, access_class, amount):
        return [{
            "id": event_id,
            "sport_key": "americanfootball_nfl",
            "commence_time": "2026-12-01T20:00:00Z",
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [{
                "key": "book",
                "title": "Book",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [{
                        "name": "Home",
                        "point": -3.5,
                        "price": -110,
                        "source_provider": "oddslink",
                        "source_account_group": f"{access_class}-account",
                        "source_access_class": access_class,
                        "size": {"kind": "posted_limit", "amount": amount},
                    }],
                }],
            }],
        }]

    monitor.ingest_events(
        event("free-limit", "free", 10000),
        observed_at="2026-11-25T10:00:00Z",
        key_fingerprint="free-account-fingerprint",
        source_access_class="free",
    )
    monitor.ingest_events(
        event("premium-limit", "premium", 50000),
        observed_at="2026-11-25T10:00:00Z",
        key_fingerprint="premium-account-fingerprint",
        source_access_class="premium",
    )
    monitor.ingest_events(
        event("unknown-limit", "restricted", 90000),
        observed_at="2026-11-25T10:00:00Z",
        key_fingerprint="unknown-account-fingerprint",
        source_access_class="restricted",
    )

    free_rows = monitor.current("americanfootball_nfl", 100, include_premium=False)
    paid_rows = monitor.current("americanfootball_nfl", 100, include_premium=True)
    assert {row["event_id"] for row in free_rows} == {"free-limit"}
    assert {row["event_id"] for row in paid_rows} == {"free-limit", "premium-limit"}
    assert all(row["source_provider"] == "oddslink" for row in paid_rows)
    assert all(row["source_capability"] == "posted_market_limits" for row in paid_rows)
    assert all(row["source_account_group"] for row in paid_rows)

    assert len(monitor.history(
        "free-limit", "book", "spreads", "Home", -3.5, include_premium=False
    )) == 1
    assert monitor.history(
        "premium-limit", "book", "spreads", "Home", -3.5, include_premium=False
    ) == []
    assert len(monitor.history(
        "premium-limit", "book", "spreads", "Home", -3.5, include_premium=True
    )) == 1
    assert monitor.history(
        "unknown-limit", "book", "spreads", "Home", -3.5, include_premium=True
    ) == []


def test_market_limit_change_windows_are_scoped_before_aggregation(tmp_path):
    monitor = MarketLimitsMonitor(path=tmp_path / "scoped-limit-changes.sqlite3")

    def event(amount, access_class):
        return [{
            "id": "same-event",
            "sport_key": "americanfootball_nfl",
            "commence_time": "2026-12-01T20:00:00Z",
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [{
                "key": "book",
                "title": "Book",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [{
                        "name": "Home",
                        "point": -3.5,
                        "price": -110,
                        "source_provider": "oddslink",
                        "source_account_group": f"{access_class}-group",
                        "source_access_class": access_class,
                        "size": {"kind": "posted_limit", "amount": amount},
                    }],
                }],
            }],
        }]

    for timestamp, amount, access_class in (
        ("2026-11-25T10:00:00Z", 10000, "free"),
        ("2026-11-25T10:01:00Z", 50000, "premium"),
        ("2026-11-25T10:02:00Z", 15000, "free"),
        ("2026-11-25T10:03:00Z", 75000, "premium"),
    ):
        monitor.ingest_events(
            event(amount, access_class),
            observed_at=timestamp,
            key_fingerprint=f"{access_class}-group",
            source_access_class=access_class,
        )

    free_changes = monitor.changes(
        direction="all", include_premium=False
    )
    paid_changes = monitor.changes(
        direction="all", include_premium=True
    )
    assert len(free_changes) == 1
    assert free_changes[0]["previous_limit"] == 10000
    assert free_changes[0]["limit_amount"] == 15000
    assert len(paid_changes) == 2
    assert {row["source_access_class"] for row in paid_changes} == {"free", "premium"}



def test_limit_extract_uses_quote_account_group_and_access_class():
    events = fixture()
    for outcome in events[0]["bookmakers"][0]["markets"][0]["outcomes"]:
        outcome["source_provider"] = "oddslink"
        outcome["source_account_group"] = "free-account"
        outcome["source_access_class"] = "free"

    rows = MarketLimitsMonitor._extract(
        "americanfootball_nfl", events, "2026-11-19T12:00:00Z",
        source_account_group="legacy-fingerprint",
        source_access_class="premium",
    )

    assert len(rows) == 2
    assert {row[14] for row in rows} == {"free-account"}
    assert {row[15] for row in rows} == {"free"}


def test_standalone_oddslink_limit_client_leases_only_free_account_groups(monkeypatch):
    import app.limits as module

    monkeypatch.setattr(
        module.config, "ODDSLINK_API_KEYS",
        "free-account|FreeCaseKey,premium-account|PremiumCaseKey",
    )
    monkeypatch.setattr(module.config, "ODDSLINK_API_KEY", "")
    monkeypatch.setattr(module.config, "ODDS_PREMIUM_PROVIDER_POLLING_ENABLED", False)
    monkeypatch.setattr(
        module.config, "ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES",
        {"oddslink": {"free-account": "free", "premium-account": "premium"}},
    )
    calls = []

    class FakeProvider:
        usage = {"requests_remaining": "99"}

        def __init__(self, key):
            calls.append(key)

        def fetch_usage(self):
            return 99

        def _request(self, path, params=None):
            return {"events": fixture()}

    monkeypatch.setattr(module, "OddsLinkProvider", FakeProvider)
    client = module.OddsLinkClient()
    events = client.fetch("nfl")

    assert calls == ["FreeCaseKey"]
    assert client.last_account_group == "free-account"
    assert client.last_access_class == "free"
    outcomes = events[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert {row["source_account_group"] for row in outcomes} == {"free-account"}
    assert {row["source_access_class"] for row in outcomes} == {"free"}



def test_market_limit_observations_from_same_tier_groups_coexist(tmp_path):
    monitor = MarketLimitsMonitor(tmp_path / "limit-account-groups.sqlite3")
    first = fixture()
    second = fixture()
    for events, group in ((first, "free-a"), (second, "free-b")):
        for event in events:
            event["sport_key"] = "americanfootball_nfl"
            for book in event.get("bookmakers", []):
                for market in book.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        outcome["source_provider"] = "oddslink"
                        outcome["source_account_group"] = group
                        outcome["source_access_class"] = "free"
        result = monitor.ingest_events(
            events, observed_at="2026-11-26T01:00:00Z",
            source_provider="oddslink", source_access_class="free",
        )
        assert result["ok"] is True

    with monitor._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT source_account_group FROM market_limit_quotes ORDER BY source_account_group"
        ).fetchall()
    assert [row["source_account_group"] for row in rows] == ["free-a", "free-b"]


def test_market_limit_legacy_unique_index_migrates_with_restricted_rows(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-limit-identity.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.execute("""
            CREATE TABLE market_limit_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL, sport TEXT NOT NULL, event_id TEXT NOT NULL,
                commence_time TEXT NOT NULL DEFAULT '', home_team TEXT NOT NULL DEFAULT '',
                away_team TEXT NOT NULL DEFAULT '', bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL DEFAULT '', market_key TEXT NOT NULL,
                outcome_name TEXT NOT NULL, point REAL, price REAL,
                limit_amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'bookmaker_currency',
                UNIQUE(observed_at,event_id,bookmaker_key,market_key,outcome_name,point)
            )
        """)
        raw.execute("""
            INSERT INTO market_limit_quotes(
                observed_at,sport,event_id,bookmaker_key,bookmaker_title,market_key,
                outcome_name,point,price,limit_amount
            ) VALUES('2026-11-26T01:00:00Z','nfl','legacy-event','book','Book',
                     'spreads','Home',2.5,-110,1000)
        """)

    monitor = MarketLimitsMonitor(path)
    with monitor._connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(market_limit_quotes)")}
        indexes = db.execute("PRAGMA index_list(market_limit_quotes)").fetchall()
        unique_columns = [
            [item["name"] for item in db.execute(
                f"PRAGMA index_info('{index['name']}')"
            ).fetchall()]
            for index in indexes if index["unique"]
        ]
        legacy = db.execute(
            "SELECT source_access_class,source_account_group FROM market_limit_quotes WHERE event_id='legacy-event'"
        ).fetchone()
        for group, access_class in (("free-a", "free"), ("premium-a", "premium")):
            db.execute("""
                INSERT INTO market_limit_quotes(
                    observed_at,sport,event_id,bookmaker_key,bookmaker_title,market_key,
                    outcome_name,point,price,limit_amount,source_provider,
                    source_account_group,source_access_class,source_capability
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                "2026-11-26T01:00:00Z", "nfl", "legacy-event", "book", "Book",
                "spreads", "Home", 2.5, -115, 1000, "oddslink", group,
                access_class, "posted_market_limits",
            ))
        count = db.execute(
            "SELECT COUNT(*) FROM market_limit_quotes WHERE event_id='legacy-event'"
        ).fetchone()[0]

    assert "source_account_group" in columns
    assert "source_access_class" in columns
    assert ["observed_at", "event_id", "bookmaker_key", "market_key",
            "outcome_name", "point", "source_provider", "source_account_group",
            "source_access_class", "source_capability"] in unique_columns
    assert legacy["source_access_class"] == "restricted"
    assert legacy["source_account_group"] == ""
    assert count == 3



def test_market_limit_migration_restricts_old_provider_level_tier_labels(tmp_path):
    import sqlite3

    path = tmp_path / "old-provider-tier-limits.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.execute("""
            CREATE TABLE market_limit_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL, sport TEXT NOT NULL, event_id TEXT NOT NULL,
                commence_time TEXT NOT NULL DEFAULT '', home_team TEXT NOT NULL DEFAULT '',
                away_team TEXT NOT NULL DEFAULT '', bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL DEFAULT '', market_key TEXT NOT NULL,
                outcome_name TEXT NOT NULL, point REAL, price REAL,
                limit_amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'bookmaker_currency',
                source_provider TEXT NOT NULL DEFAULT '',
                source_account_group TEXT NOT NULL DEFAULT '',
                source_access_class TEXT NOT NULL DEFAULT 'restricted',
                source_capability TEXT NOT NULL DEFAULT 'posted_market_limits',
                UNIQUE(observed_at,event_id,bookmaker_key,market_key,outcome_name,point)
            )
        """)
        raw.execute("""
            INSERT INTO market_limit_quotes(
                observed_at,sport,event_id,bookmaker_key,bookmaker_title,market_key,
                outcome_name,point,price,limit_amount,source_provider,
                source_account_group,source_access_class
            ) VALUES('2026-11-26T01:00:00Z','nfl','legacy-provider-tier','book','Book',
                     'spreads','Home',2.5,-110,1000,'oddslink','old-keyset-fingerprint','free')
        """)

    monitor = MarketLimitsMonitor(path)
    with monitor._connect() as db:
        row = db.execute(
            "SELECT source_account_group,source_access_class FROM market_limit_quotes WHERE event_id='legacy-provider-tier'"
        ).fetchone()
    assert row["source_account_group"] == "old-keyset-fingerprint"
    assert row["source_access_class"] == "restricted"



def test_market_limit_tier_follows_size_source_not_odds_source(tmp_path):
    monitor = MarketLimitsMonitor(tmp_path / "size-source-tier.sqlite3")
    events = fixture()
    outcome = events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    outcome.update({
        "source_provider": "the_odds_api",
        "source_account_group": "free-account",
        "source_access_class": "free",
        "size_source_provider": "oddize",
        "size_source_account_group": "premium-account",
        "size_source_access_class": "premium",
    })

    rows = monitor._extract(
        "americanfootball_nfl", events,
        "2026-11-26T01:00:00Z",
        source_provider="the_odds_api",
        source_access_class="free",
    )
    selected = next(row for row in rows if row[9] == outcome["name"])
    assert selected[13] == "oddize"
    assert selected[14] == "premium-account"
    assert selected[15] == "premium"


def test_market_limit_size_from_unknown_provider_group_is_restricted(tmp_path):
    monitor = MarketLimitsMonitor(tmp_path / "size-source-unknown.sqlite3")
    events = fixture()
    outcome = events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    outcome.update({
        "source_provider": "the_odds_api",
        "source_account_group": "free-account",
        "source_access_class": "free",
        "size_source_provider": "oddize",
        "size_source_account_group": "",
        "size_source_access_class": "premium",
    })

    rows = monitor._extract(
        "americanfootball_nfl", events,
        "2026-11-26T01:00:00Z",
        source_provider="the_odds_api",
        source_access_class="free",
    )
    selected = next(row for row in rows if row[9] == outcome["name"])
    assert selected[13] == "oddize"
    assert selected[14] == ""
    assert selected[15] == "restricted"
