import json

import pytest

from app.providers import the_odds_api
from app.providers.the_odds_api import TheOddsAPIProvider, ProviderError


def test_provider_requires_key():
    with pytest.raises(ProviderError):
        TheOddsAPIProvider("")


def test_provider_preserves_key_and_captures_quota_headers(monkeypatch):
    class FakeResponse:
        headers = {
            "x-requests-remaining": "97",
            "x-requests-used": "3",
            "x-requests-last": "1",
        }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([{
                "id": "event-1", "sport_key": "americanfootball_nfl",
                "home_team": "Home", "away_team": "Away", "bookmakers": []
            }]).encode()

    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(the_odds_api, "urlopen", fake_urlopen)
    provider = TheOddsAPIProvider("AbC-Case-Sensitive-Key")
    events = provider.fetch_odds("americanfootball_nfl")

    assert "apiKey=AbC-Case-Sensitive-Key" in seen["url"]
    assert seen["timeout"] == 15
    assert events[0]["id"] == "event-1"
    assert provider.usage == {
        "requests_remaining": "97",
        "requests_used": "3",
        "requests_last": "1",
    }


def test_provider_uses_explicit_bookmakers_instead_of_regions(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b"[]"

    seen = {}
    def fake_urlopen(request, timeout):
        seen["query"] = parse_qs(urlparse(request.full_url).query)
        return FakeResponse()

    monkeypatch.setattr(the_odds_api, "urlopen", fake_urlopen)
    provider = TheOddsAPIProvider("CaseSensitiveKey", regions="us",
                                  bookmakers="draftkings,fanduel,betmgm")
    provider.fetch_odds("americanfootball_nfl")
    assert seen["query"]["bookmakers"] == ["draftkings,fanduel,betmgm"]
    assert "regions" not in seen["query"]


def test_provider_supports_multiple_regions_when_bookmakers_not_set(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b"[]"

    seen = {}
    def fake_urlopen(request, timeout):
        seen["query"] = parse_qs(urlparse(request.full_url).query)
        return FakeResponse()

    monkeypatch.setattr(the_odds_api, "urlopen", fake_urlopen)
    TheOddsAPIProvider("CaseSensitiveKey", regions="us,us2").fetch_odds(
        "americanfootball_nfl"
    )
    assert seen["query"]["regions"] == ["us,us2"]
    assert "bookmakers" not in seen["query"]


def test_therundown_normalizes_v2_events(monkeypatch):
    from app.providers import therundown
    from app.providers.therundown import TheRundownProvider
    import json
    from datetime import datetime, timezone

    class FakeResponse:
        def __init__(self, payload, headers=None):
            self.payload = payload
            self.headers = headers or {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    calls = []
    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "/sports/dates?" in request.full_url:
            return FakeResponse({"2": {"dates": [datetime.now(timezone.utc).timestamp() + 86400]}})
        return FakeResponse({"events": [{
                "event_id": "evt-1", "sport_id": 2,
                "event_date": "2026-11-20T18:00:00Z",
                "teams": [
                    {"name": "Away Team", "is_home": False},
                    {"name": "Home Team", "is_home": True},
                ],
                "markets": [{
                    "market_id": 1, "period_id": 0, "name": "moneyline",
                    "participants": [
                        {"id": 10, "name": "Away Team", "lines": [{"prices": {
                            "19": {"price": 105, "updated_at": "2026-11-17T12:00:00Z"}
                        }}]},
                        {"id": 11, "name": "Home Team", "lines": [{"prices": {
                            "19": {"price": -115, "updated_at": "2026-11-17T12:00:00Z"}
                        }}]},
                    ],
                }],
            }]}, {"X-Datapoints": "123", "X-Datapoints-Used": "120", "X-Datapoints-Remaining": "19880", "X-Data-Delay-Seconds": "300"})

    seen = {}
    def capture_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        return fake_urlopen(request, timeout)
    monkeypatch.setattr(therundown, "urlopen", capture_urlopen)
    provider = TheRundownProvider("CaseSensitiveRundownKey")
    events = provider.fetch_odds("americanfootball_nfl")
    assert events[0]["id"] == "evt-1"
    assert events[0]["home_team"] == "Home Team"
    assert events[0]["away_team"] == "Away Team"
    assert events[0]["bookmakers"][0]["key"] == "draftkings"
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] == 105
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] == "2026-11-17T12:00:00Z"
    assert provider.usage["datapoints_remaining"] == "19880"
    assert provider.usage["datapoints_total"] == "123"
    assert provider.usage["data_delay_seconds"] == "300"
    assert seen["headers"]["X-therundown-key"] == "CaseSensitiveRundownKey"
    assert "affiliate_ids=19%2C22%2C23" in seen["url"]
    assert len(calls) == 2


def test_therundown_rejects_unknown_sports():
    from app.providers.therundown import TheRundownProvider, ProviderError
    with pytest.raises(ProviderError):
        TheRundownProvider("key").fetch_odds("unknown_league")



def test_the_odds_api_fetches_player_props_per_event_and_preserves_player_name(monkeypatch):
    from urllib.parse import urlparse, parse_qs

    class FakeResponse:
        headers = {"x-requests-remaining": "88", "x-requests-used": "12", "x-requests-last": "2"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                "id": "event-1",
                "bookmakers": [{
                    "key": "draftkings", "title": "DraftKings",
                    "markets": [{
                        "key": "player_points", "last_update": "2026-11-18T12:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Player One", "point": 24.5, "price": -110},
                            {"name": "Under", "description": "Player One", "point": 24.5, "price": -110},
                        ],
                    }],
                }],
            }).encode()

    seen = {}
    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(the_odds_api, "urlopen", fake_urlopen)
    provider = TheOddsAPIProvider("CaseSensitiveKey")
    events = [{"id": "event-1", "bookmakers": []}]
    status = provider.fetch_player_props("basketball_nba", events, "player_points")
    assert status["events_requested"] == 1
    assert status["events_with_props"] == 1
    assert parse_qs(urlparse(seen["url"]).query)["markets"] == ["player_points"]
    assert "CaseSensitiveKey" in seen["url"]
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["description"] == "Player One"
    assert provider.usage["requests_remaining"] == "88"
