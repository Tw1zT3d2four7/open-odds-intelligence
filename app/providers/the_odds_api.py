import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .base import OddsProvider

class ProviderError(RuntimeError):
    pass

class TheOddsAPIProvider(OddsProvider):
    name = "the_odds_api"
    base_url = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, regions="us", markets="h2h,spreads,totals", odds_format="american"):
        if not api_key:
            raise ProviderError("THE_ODDS_API_KEY is not configured")
        self.api_key = api_key
        self.regions, self.markets, self.odds_format = regions, markets, odds_format
        self.usage = {}

    def fetch_odds(self, sport: str) -> list[dict]:
        query = urlencode({"apiKey": self.api_key, "regions": self.regions,
                           "markets": self.markets, "oddsFormat": self.odds_format})
        req = Request(f"{self.base_url}/sports/{sport}/odds/?{query}",
                      headers={"User-Agent": "OpenOddsIntelligence/0.1"})
        try:
            with urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.usage = {key: response.headers.get(header) for key, header in {
                    "requests_remaining": "x-requests-remaining",
                    "requests_used": "x-requests-used",
                    "requests_last": "x-requests-last",
                }.items() if response.headers.get(header) is not None}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(f"Odds provider HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Odds provider request failed: {exc}") from exc
        if not isinstance(payload, list):
            raise ProviderError("Odds provider returned an unexpected response format")

        now = datetime.now(timezone.utc).isoformat()
        events = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            books = []
            for b in raw.get("bookmakers", []):
                markets = []
                for m in b.get("markets", []):
                    outcomes = [{"name": o.get("name", ""), "price": int(o["price"]),
                                 "point": o.get("point")} for o in m.get("outcomes", [])
                                if isinstance(o, dict) and isinstance(o.get("price"), (int, float))]
                    markets.append({"key": m.get("key", ""), "outcomes": outcomes})
                books.append({"key": b.get("key", ""), "title": b.get("title", b.get("key", "")),
                              "last_update": b.get("last_update"), "markets": markets})
            events.append({"id": raw.get("id", ""), "sport_key": raw.get("sport_key", sport),
                           "sport_title": raw.get("sport_title", ""),
                           "commence_time": raw.get("commence_time", ""),
                           "home_team": raw.get("home_team", ""), "away_team": raw.get("away_team", ""),
                           "source": self.name, "retrieved_at": now, "bookmakers": books})
        return events
