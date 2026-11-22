import json

import pytest

from app.providers import oddslink
from app.providers.oddslink import OddsLinkProvider
from app.providers.the_odds_api import ProviderError


def test_oddslink_requires_api_key():
    with pytest.raises(ProviderError):
        OddsLinkProvider("")


def test_oddslink_normalizes_all_books_and_period_market_lines():
    provider = OddsLinkProvider("Case-Sensitive-OddsLink-Key")
    event = provider._normalize_event({
        "id": "nfl:chiefs-raiders:2026-11-23",
        "sport_key": "nfl",
        "sport_title": "NFL",
        "commence_time": "2026-11-23T20:00:00.000Z",
        "home_team": "Kansas City Chiefs",
        "away_team": "Las Vegas Raiders",
        "bookmakers": [{
            "key": "draftkings", "title": "DraftKings",
            "last_update": "2026-11-23T18:00:00.000Z",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Las Vegas Raiders", "price": 145},
                    {"name": "Kansas City Chiefs", "price": -165},
                ]},
                {"key": "totals", "period": "1h", "last_update": "2026-11-23T17:50:00.000Z",
                 "outcomes": [
                    {"name": "Over", "price": -110, "point": 21.5, "main": True},
                    {"name": "Under", "price": -110, "point": 21.5, "main": True},
                 ]},
            ],
        }, {
            "key": "betmgm", "title": "BetMGM",
            "markets": [{"key": "spreads", "outcomes": [
                {"name": "Kansas City Chiefs", "price": -110, "point": -3.5}
            ]}],
        }],
    }, "americanfootball_nfl")

    assert event["id"] == "nfl:chiefs-raiders:2026-11-23"
    assert event["sport_key"] == "americanfootball_nfl"
    assert len(event["bookmakers"]) == 2
    markets = {
        market["key"]: market
        for book in event["bookmakers"]
        for market in book["markets"]
    }
    assert "totals_1h" in markets
    assert markets["totals_1h"]["outcomes"][0]["updated_at"] == "2026-11-23T17:50:00.000Z"
    assert markets["h2h"]["outcomes"][0]["source_provider"] == "oddslink"


def test_oddslink_checks_free_account_allowance_before_priced_request(monkeypatch):
    calls = []
    class FakeResponse:
        headers = {"x-requests-used": "21", "x-requests-remaining": "979",
                   "x-requests-last": "1", "X-RateLimit-Limit": "2"}
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, dict(request.header_items())))
        if "/account/usage" in request.full_url:
            return FakeResponse({
                "plan": "free", "requests_allowance": 1000,
                "requests_used_this_period": 20,
                "period_resets_at": "2026-12-01T00:00:00.000Z",
                "speed_limit_per_second": 2,
                "requests_last_second": 0,
            })
        return FakeResponse([])

    monkeypatch.setattr(oddslink, "urlopen", fake_urlopen)
    provider = OddsLinkProvider("Case-Sensitive-OddsLink-Key")
    assert provider.fetch_odds("americanfootball_nfl") == []
    assert len(calls) == 2
    assert "/account/usage" in calls[0][0]
    assert "/sports/nfl/odds" in calls[1][0]
    assert calls[1][1]["X-api-key"] == "Case-Sensitive-OddsLink-Key"
    assert "periods=all" in calls[1][0]
    assert provider.usage["requests_remaining"] == 979


def test_oddslink_blocks_spending_after_free_allowance_is_spent(monkeypatch):
    calls = []
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                "plan": "free", "requests_allowance": 1000,
                "requests_used_this_period": 1000,
                "period_resets_at": "2026-12-01T00:00:00.000Z",
                "speed_limit_per_second": 2,
                "requests_last_second": 0,
            }).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(oddslink, "urlopen", fake_urlopen)
    provider = OddsLinkProvider("Key")
    with pytest.raises(ProviderError, match="allowance is exhausted") as caught:
        provider.fetch_odds("americanfootball_nfl")
    assert caught.value.status_code == 429
    assert len(calls) == 1
    assert "/account/usage" in calls[0]


def test_oddslink_filters_tennis_by_tournament_type():
    provider = OddsLinkProvider("Key")
    raw = {
        "id": "tennis:one", "commence_time": "2026-11-23T12:00:00Z",
        "home_team": "Player A", "away_team": "Player B",
        "tournament": {"key": "tennis_atp_hangzhou"},
        "bookmakers": [],
    }
    assert provider._normalize_event(raw, "tennis_atp")["id"] == "tennis:one"
    assert OddsLinkProvider.SPORTS["tennis_wta"][1]["tournament_prefix"] == "tennis_wta_"
