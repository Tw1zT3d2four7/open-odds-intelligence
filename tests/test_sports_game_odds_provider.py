import json

import pytest

from app.providers import sports_game_odds
from app.providers.sports_game_odds import SportsGameOddsProvider
from app.providers.the_odds_api import ProviderError


def test_sgo_requires_api_key():
    with pytest.raises(ProviderError):
        SportsGameOddsProvider("")


def test_sgo_normalizes_main_markets_and_player_props():
    provider = SportsGameOddsProvider("CaseSensitiveSGOKey")
    event = provider._normalize_event({
        "eventID": "sgo-1",
        "leagueID": "NBA",
        "status": {"startsAt": "2026-11-23T20:00:00Z"},
        "teams": {
            "home": {"names": {"long": "Boston Celtics"}},
            "away": {"names": {"long": "New York Knicks"}},
        },
        "players": [{"playerID": "J_TATUM", "name": "Jayson Tatum"}],
        "odds": {
            "points-home-game-ml-home": {
                "betTypeID": "ml", "statEntityID": "home", "sideID": "home",
                "byBookmaker": {
                    "draftkings": {"odds": "-120", "available": True,
                                   "lastUpdatedAt": "2026-11-23T19:00:00Z"}
                },
            },
            "points-home-game-sp-home": {
                "betTypeID": "sp", "statEntityID": "home", "sideID": "home",
                "byBookmaker": {
                    "fanduel": {"odds": "-110", "spread": "-4.5", "available": True,
                               "altLines": [{"odds": "-105", "spread": "-5.5",
                                             "available": True}]}
                },
            },
            "points-all-game-ou-over": {
                "betTypeID": "ou", "statEntityID": "all", "sideID": "over",
                "byBookmaker": {
                    "caesars": {"odds": "-105", "overUnder": "224.5", "available": True}
                },
            },
            "points-J_TATUM-game-ou-over": {
                "betTypeID": "ou", "statEntityID": "J_TATUM", "playerID": "J_TATUM",
                "sideID": "over",
                "byBookmaker": {
                    "draftkings": {"odds": "+105", "overUnder": "28.5", "available": True}
                },
            },
        },
    }, "basketball_nba")

    books = {book["key"]: book for book in event["bookmakers"]}
    assert event["id"] == "sgo-1"
    assert event["home_team"] == "Boston Celtics"
    assert event["away_team"] == "New York Knicks"
    assert books["draftkings"]["markets"][0]["key"] == "h2h"
    spread = books["fanduel"]["markets"][0]["outcomes"]
    assert {item["point"] for item in spread} == {-4.5, -5.5}
    props = next(
        market for market in books["draftkings"]["markets"]
        if market["key"] == "player_points"
    )
    assert props["outcomes"][0]["description"] == "Jayson Tatum"
    assert props["outcomes"][0]["point"] == 28.5
    assert props["outcomes"][0]["price"] == 105


def test_sgo_fetch_uses_case_sensitive_header_and_free_league_filter(monkeypatch):
    class FakeResponse:
        headers = {"x-ratelimit-remaining": "9"}
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    seen = {"urls": []}
    def fake_urlopen(request, timeout):
        seen["urls"].append(request.full_url)
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        if "/account/usage" in request.full_url:
            return FakeResponse({"success": True, "data": {
                "tier": "amateur",
                "rateLimits": {"per-month": {
                    "max-entities": "2500", "current-entities": "100"
                }},
            }})
        return FakeResponse({"success": True, "data": []})

    monkeypatch.setattr(sports_game_odds, "urlopen", fake_urlopen)
    monkeypatch.setattr(SportsGameOddsProvider, "_pace", lambda self: None)
    provider = SportsGameOddsProvider("Case-Sensitive-SGO")
    assert provider.fetch_odds("basketball_nba") == []
    events_url = next(url for url in seen["urls"] if "/events/" in url)
    assert "leagueID=NBA" in events_url
    assert "oddsAvailable=true" in events_url
    assert "limit=100" in events_url
    assert all("apiKey=" not in url for url in seen["urls"])
    assert seen["headers"]["X-api-key"] == "Case-Sensitive-SGO"
    assert provider.usage["tier"] == "amateur"
    assert provider.usage["monthly_objects_remaining"] == 2400


def test_sgo_rejects_leagues_outside_published_free_tier():
    provider = SportsGameOddsProvider("Key")
    with pytest.raises(ProviderError, match="does not include"):
        provider.fetch_odds("soccer_epl")


def test_sgo_blocks_event_request_when_free_monthly_object_allowance_is_exhausted(monkeypatch):
    calls = []
    class FakeResponse:
        headers = {}
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return FakeResponse({"success": True, "data": {
            "tier": "amateur",
            "rateLimits": {"per-month": {
                "max-entities": "2500", "current-entities": "2500"
            }},
        }})

    monkeypatch.setattr(sports_game_odds, "urlopen", fake_urlopen)
    monkeypatch.setattr(SportsGameOddsProvider, "_pace", lambda self: None)
    provider = SportsGameOddsProvider("Key")
    with pytest.raises(ProviderError, match="allowance is exhausted") as caught:
        provider.fetch_odds("basketball_nba")
    assert caught.value.status_code == 429
    assert len(calls) == 1
    assert "/account/usage" in calls[0]
