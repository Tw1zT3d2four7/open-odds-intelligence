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




def test_odds_api_skips_malformed_nested_bookmaker_market_and_outcome_records(monkeypatch):
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps([{
                "id": "event-malformed-nested",
                "sport_key": "americanfootball_nfl",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    None,
                    {"key": "bad-markets", "markets": None},
                    {"key": "good-book", "title": "Good Book", "markets": [
                        None,
                        {"key": "bad-outcomes", "outcomes": None},
                        {"key": "h2h", "outcomes": [
                            None,
                            "not-an-outcome",
                            {"name": "Home", "price": -110},
                            {"name": "Invalid", "price": True},
                        ]},
                    ]},
                ],
            }]).encode()

    monkeypatch.setattr(the_odds_api, "urlopen", lambda request, timeout: FakeResponse())
    events = TheOddsAPIProvider("CaseSensitiveKey").fetch_odds("americanfootball_nfl")

    assert len(events) == 1
    assert events[0]["id"] == "event-malformed-nested"
    assert len(events[0]["bookmakers"]) == 1
    assert events[0]["bookmakers"][0]["key"] == "good-book"
    assert len(events[0]["bookmakers"][0]["markets"]) == 1
    assert events[0]["bookmakers"][0]["markets"][0]["key"] == "h2h"
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"] == [
        {"name": "Home", "description": "", "price": -110,
         "point": None, "updated_at": None}
    ]


@pytest.mark.parametrize("bookmakers", [None, {}, "malformed"])
def test_odds_api_treats_non_list_bookmaker_collection_as_empty(monkeypatch, bookmakers):
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps([{"id": "event-empty-books", "bookmakers": bookmakers}]).encode()

    monkeypatch.setattr(the_odds_api, "urlopen", lambda request, timeout: FakeResponse())
    events = TheOddsAPIProvider("CaseSensitiveKey").fetch_odds("americanfootball_nfl")
    assert len(events) == 1
    assert events[0]["bookmakers"] == []

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
                    {"name": "Away Team", "is_away": True},
                    {"name": "Home Team", "is_away": False},
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
    # TheRundown uses separate NFL schedule IDs for regular season,
    # preseason and playoffs; this is distinct from affiliate IDs.
    assert "sport_ids=2%2C25%2C26" in calls[0]
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


def test_odds_api_decimal_format_is_normalized_to_american_odds(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps([{
                "id": "decimal-event", "sport_key": "basketball_nba",
                "home_team": "Home", "away_team": "Away",
                "commence_time": "2026-11-20T00:00:00Z",
                "bookmakers": [{
                    "key": "book-a", "title": "Book A", "markets": [{
                        "key": "h2h", "last_update": "2026-11-18T12:00:00Z",
                        "outcomes": [
                            {"name": "Home", "price": 1.91},
                            {"name": "Away", "price": 1.91},
                        ],
                    }],
                }],
            }]).encode()

    seen = {}
    def fake_urlopen(request, timeout):
        seen["query"] = parse_qs(urlparse(request.full_url).query)
        return FakeResponse()

    monkeypatch.setattr(the_odds_api, "urlopen", fake_urlopen)
    provider = TheOddsAPIProvider("CaseSensitiveKey", odds_format="decimal")
    events = provider.fetch_odds("basketball_nba")
    outcomes = events[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert seen["query"]["oddsFormat"] == ["decimal"]
    assert [item["price"] for item in outcomes] == [-110, -110]


def test_odds_api_decimal_player_props_are_normalized_to_american_odds(monkeypatch):
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                "id": "decimal-props",
                "bookmakers": [{
                    "key": "book-a", "title": "Book A",
                    "markets": [{
                        "key": "player_points", "outcomes": [
                            {"name": "Over", "description": "Player One", "point": 20.5, "price": 1.91},
                        ],
                    }],
                }],
            }).encode()

    monkeypatch.setattr(the_odds_api, "urlopen", lambda request, timeout: FakeResponse())
    provider = TheOddsAPIProvider("CaseSensitiveKey", odds_format="decimal")
    events = [{"id": "decimal-props", "bookmakers": []}]
    status = provider.fetch_player_props("basketball_nba", events, "player_points")
    assert status["events_with_props"] == 1
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] == -110


def test_therundown_accepts_canonical_odds_api_mls_sport_key():
    from app.providers.therundown import TheRundownProvider
    assert TheRundownProvider.SPORTS["soccer_usa_mls"] == (10, "MLS")


@pytest.mark.parametrize("sport_id,expected", [
    (2, "2,25,26"), (3, "3,31"), (4, "4,23,24"), (6, "6,27,28"),
    (1, "1"), (5, "5"), (8, "8"), (10, "10"), (11, "11"),
])
def test_therundown_schedule_discovery_includes_supported_season_variants(
    sport_id, expected
):
    from urllib.parse import parse_qs, urlparse
    from app.providers.therundown import TheRundownProvider

    provider = TheRundownProvider("CaseSensitiveKey")
    seen = {}
    def fake_request(url):
        seen["query"] = parse_qs(urlparse(url).query)
        return {}
    provider._request = fake_request
    provider._next_event_slate(sport_id)
    assert seen["query"]["sport_ids"] == [expected]



@pytest.mark.parametrize("status_code,retryable", [
    (401, False), (403, False), (429, True), (503, True),
])
def test_odds_api_http_errors_expose_safe_retry_classification(
    monkeypatch, status_code, retryable
):
    from urllib.error import HTTPError
    from email.message import Message

    headers = Message()
    headers["Retry-After"] = "30"
    headers["x-requests-remaining"] = "0"

    def fail_request(request, timeout):
        raise HTTPError(request.full_url, status_code, "provider error", headers,
                        __import__("io").BytesIO(b'{"message":"quota or access failure"}'))

    monkeypatch.setattr(the_odds_api, "urlopen", fail_request)
    provider = TheOddsAPIProvider("CaseSensitiveKey")
    with pytest.raises(ProviderError) as caught:
        provider.fetch_odds("americanfootball_nfl")

    error = caught.value
    assert error.status_code == status_code
    assert error.retryable is retryable
    assert error.retry_after == "30"
    assert provider.usage["requests_remaining"] == "0"


def test_odds_api_transport_errors_are_classified_as_retryable(monkeypatch):
    from urllib.error import URLError

    monkeypatch.setattr(
        the_odds_api, "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(URLError("temporary DNS failure")),
    )
    provider = TheOddsAPIProvider("CaseSensitiveKey")
    with pytest.raises(ProviderError) as caught:
        provider.fetch_odds("americanfootball_nfl")

    assert caught.value.status_code is None
    assert caught.value.retry_after is None
    assert caught.value.retryable is True


def test_therundown_tennis_normalizes_players_from_moneyline_participants():
    from app.providers.therundown import TheRundownProvider

    provider = TheRundownProvider("case-Sensitive-Key")
    event = provider._normalize_event({
        "event_id": "tennis-match-1",
        "event_date": "2026-11-20T18:00:00Z",
        "markets": [{
            "market_id": 1,
            "participants": [
                {"name": "Player A", "lines": [{"value": None, "prices": {"19": {"price": -110}}}]},
                {"name": "Player B", "lines": [{"value": None, "prices": {"19": {"price": 100}}}]},
            ],
        }],
    }, "tennis_atp", "ATP Tennis")

    assert event["home_team"] == "Player A"
    assert event["away_team"] == "Player B"
    assert event["sport_key"] == "tennis_atp"
    assert len(event["bookmakers"]) == 1
    assert event["bookmakers"][0]["markets"][0]["key"] == "h2h"


@pytest.mark.parametrize("value", [True, False, 0, "0", 100.5, -110.25, float("nan"), float("inf"), None, "not-a-price"])
def test_odds_api_rejects_invalid_american_prices(value):
    provider = TheOddsAPIProvider("CaseSensitiveKey")
    with pytest.raises(ValueError):
        provider._normalize_price(value)


@pytest.mark.parametrize("value", [True, False, 0, "0", 1, 1.0, float("nan"), float("inf"), None, "not-a-price"])
def test_odds_api_rejects_invalid_decimal_prices(value):
    provider = TheOddsAPIProvider("CaseSensitiveKey", odds_format="decimal")
    with pytest.raises(ValueError):
        provider._normalize_price(value)


def test_odds_api_does_not_truncate_fractional_american_prices(monkeypatch):
    class FakeResponse:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps([{
                "id": "fractional-price-event", "sport_key": "basketball_nba",
                "home_team": "Home", "away_team": "Away",
                "bookmakers": [{"key": "book-a", "markets": [{
                    "key": "h2h", "outcomes": [
                        {"name": "Home", "price": -110.5},
                        {"name": "Away", "price": 105},
                    ],
                }]}],
            }]).encode()

    monkeypatch.setattr(the_odds_api, "urlopen", lambda request, timeout: FakeResponse())
    events = TheOddsAPIProvider("CaseSensitiveKey").fetch_odds("basketball_nba")
    outcomes = events[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert [item["price"] for item in outcomes] == [105]


@pytest.mark.parametrize("value,expected", [
    (-110, -110), ("105", 105), ("-110.0", -110),
    (True, None), (False, None), (0, None), ("0", None),
    (110.5, None), (-110.25, None), (float("nan"), None),
    (float("inf"), None), (None, None), ("bad", None), (0.0001, None),
])
def test_therundown_american_price_validation(value, expected):
    from app.providers.therundown import TheRundownProvider

    assert TheRundownProvider._american_price(value) == expected


def test_therundown_drops_invalid_prices_without_aborting_event_normalization():
    from app.providers.therundown import TheRundownProvider

    provider = TheRundownProvider("CaseSensitiveKey")
    event = provider._normalize_event({
        "event_id": "price-integrity-event",
        "teams": [
            {"name": "Away", "is_away": True},
            {"name": "Home", "is_home": True},
        ],
        "markets": [{
            "market_id": 1,
            "participants": [
                {"name": "Home", "lines": [{"prices": {
                    "19": {"price": True}, "22": {"price": 110.5},
                    "23": {"price": -110},
                }}]},
                {"name": "Away", "lines": [{"prices": {
                    "19": {"price": float("inf")}, "22": {"price": 105},
                    "23": {"price": 0},
                }}]},
            ],
        }],
    }, "americanfootball_nfl", "NFL")

    books = {book["key"]: book for book in event["bookmakers"]}
    assert set(books) == {"fanduel", "betmgm"}
    assert books["fanduel"]["markets"][0]["outcomes"][0]["price"] == -110
    assert books["betmgm"]["markets"][0]["outcomes"][0]["price"] == 105


@pytest.mark.parametrize("value", [-99, -50, -1, 1, 50, 99])
def test_odds_api_rejects_american_prices_inside_invalid_range(value):
    provider = TheOddsAPIProvider("CaseSensitiveKey")
    with pytest.raises(ValueError, match="\+100 or greater"):
        provider._normalize_price(value)


@pytest.mark.parametrize("value", [-99, -50, -1, 1, 50, 99])
def test_therundown_skips_american_prices_inside_invalid_range(value):
    from app.providers.therundown import TheRundownProvider
    assert TheRundownProvider._american_price(value) is None


@pytest.mark.parametrize("value", [-100, 100, -101, 101])
def test_providers_accept_valid_american_price_boundaries(value):
    from app.providers.therundown import TheRundownProvider
    assert TheOddsAPIProvider("CaseSensitiveKey")._normalize_price(value) == value
    assert TheRundownProvider._american_price(value) == value
