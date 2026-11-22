import json

import pytest

from app.providers import oddize
from app.providers.oddize import OddizeProvider
from app.providers.the_odds_api import ProviderError


def test_oddize_requires_api_key():
    with pytest.raises(ProviderError):
        OddizeProvider("")


def test_oddize_normalizes_twenty_book_feed_and_market_types():
    provider = OddizeProvider("Case-Sensitive-Oddize-Key")
    event = provider._normalize_event({
        "event_id": "202611180ORL",
        "team1": "PHI",
        "team2": "ORL",
        "start_date": "2026-11-18T19:00:00-05:00",
        "odds": [
            {"book": "b365", "book_name": "Bet365", "team": "ORL",
             "line": 1.5, "american_odds": "-119", "offer_type": "s",
             "timestamp": "2026-11-18T21:50:58Z"},
            {"book": "blys", "book_name": "Bally's", "team": "Over",
             "line": 221.5, "american_odds": "-110", "offer_type": "t"},
            {"book": "pin", "book_name": "Pinnacle", "team": "PHI",
             "line": None, "american_odds": "+105", "offer_type": "ml"},
            {"book": "dk", "book_name": "DraftKings", "team": "ORL",
             "line": None, "american_odds": "-115", "offer_type": "ml"},
        ],
    }, "basketball_nba")

    books = {book["key"]: book for book in event["bookmakers"]}
    assert event["home_team"] == "ORL"
    assert event["away_team"] == "PHI"
    assert set(books) == {"bet365", "ballys", "pinnacle", "draftkings"}
    assert books["bet365"]["markets"][0]["key"] == "spreads"
    assert books["ballys"]["markets"][0]["key"] == "totals"
    assert books["pinnacle"]["markets"][0]["key"] == "h2h"
    assert books["pinnacle"]["markets"][0]["outcomes"][0]["price"] == 105


def test_oddize_usage_preflight_and_current_odds_request(monkeypatch):
    calls = []
    class FakeResponse:
        headers = {"X-Credits-Remaining": "1498", "X-Credits-Cost": "1"}
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, dict(request.header_items())))
        if "/account/usage" in request.full_url:
            return FakeResponse({
                "plan_id": "api_developer", "credits_limit": 1500,
                "credits_used": 1, "credits_remaining": 1499,
                "requests_made": 2, "rate_limit_rpm": 30,
                "resets_in_seconds": 1000,
            })
        return FakeResponse({"sport": "nba", "events": []})

    monkeypatch.setattr(oddize, "urlopen", fake_urlopen)
    provider = OddizeProvider("Case-Sensitive-Oddize-Key")
    assert provider.fetch_odds("basketball_nba") == []
    assert len(calls) == 2
    assert "/account/usage" in calls[0][0]
    assert "/odds/latest" in calls[1][0]
    assert calls[1][1]["X-api-key"] == "Case-Sensitive-Oddize-Key"
    assert "offer_types=s" in calls[1][0]
    assert provider.usage["credits_remaining"] == 1498


def test_oddize_blocks_credit_spending_when_free_allowance_is_empty(monkeypatch):
    calls = []
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                "plan_id": "api_developer", "credits_limit": 1500,
                "credits_used": 1500, "credits_remaining": 0,
                "resets_in_seconds": 86400,
            }).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(oddize, "urlopen", fake_urlopen)
    provider = OddizeProvider("Key")
    with pytest.raises(ProviderError, match="0 credits") as caught:
        provider.fetch_odds("basketball_nba")
    assert caught.value.status_code == 402
    assert len(calls) == 1
    assert "/account/usage" in calls[0]


def test_oddize_rotates_through_prop_types_and_attaches_player_lines(monkeypatch):
    provider = OddizeProvider("Key")
    provider._sports_metadata = [{
        "sport": "nba",
        "prop_types": [{"prop_type": "player_pts"}, {"prop_type": "player_reb"}],
    }]
    calls = []
    def fake_request(path, params=None):
        calls.append((path, params))
        if path == "/props/nba":
            return {"events": [{
                "event_id": "nba-event",
                "team1": "BOS",
                "team2": "NYK",
                "start_date": "2026-11-23T20:00:00Z",
                "players": [{
                    "player_name": "Jayson Tatum",
                    "main_line": 28.5,
                    "odds": [{
                        "book": "dk", "book_name": "DraftKings",
                        "team": "Over", "line": 28.5,
                        "american_odds": "-110",
                        "timestamp": "2026-11-23T19:00:00Z",
                    }],
                }],
            }]}
        raise AssertionError(path)
    provider._request = fake_request
    usage_calls = []
    provider._require_credits = lambda amount: usage_calls.append(amount)
    events = [{
        "id": "nba-event", "sport_key": "basketball_nba",
        "home_team": "NYK", "away_team": "BOS", "bookmakers": [],
    }]

    result = provider.fetch_props("basketball_nba", events, max_prop_types=1, cursor=1)

    assert result["prop_types_requested"] == ["player_reb"]
    assert result["next_cursor"] == 0
    assert usage_calls == [2]
    outcome = events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome["description"] == "Jayson Tatum"
    assert outcome["point"] == 28.5
    assert outcome["source_provider"] == "oddize"


def test_oddize_fetches_and_normalizes_alternate_spreads_and_totals():
    provider = OddizeProvider("Key")
    credits = []
    provider._require_credits = lambda amount: credits.append(amount)
    def fake_request(path, params=None):
        if "alt_s" in params.values():
            return {"lines": [{
                "line": -5.5,
                "prices": [{
                    "book": "dk", "book_name": "DraftKings", "team": "BOS",
                    "line": -5.5, "american_odds": "-110",
                    "timestamp": "2026-11-23T19:00:00Z",
                }],
            }]}
        return {"lines": [{
            "line": 224.5,
            "prices": [{
                "book": "fd", "book_name": "FanDuel", "team": "Over",
                "line": 224.5, "american_odds": "-105",
            }],
        }]}
    provider._request = fake_request
    events = [{
        "id": "nba-event", "bookmakers": [],
    }]

    result = provider.fetch_alternate_lines("basketball_nba", events, max_events=1)

    assert result["alternate_quotes_added"] == 2
    assert credits == [2, 2]
    books = {book["key"]: book for book in events[0]["bookmakers"]}
    assert books["draftkings"]["markets"][0]["key"] == "spreads"
    assert books["fanduel"]["markets"][0]["key"] == "totals"
