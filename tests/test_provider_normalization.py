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
