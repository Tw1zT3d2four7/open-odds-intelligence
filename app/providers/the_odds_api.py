import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .base import OddsProvider
from ..math import decimal_to_american

class ProviderError(RuntimeError):
    """Provider failure with machine-readable retry and quota diagnostics."""

    def __init__(self, message: str, *, status_code=None, retry_after=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = bool(retryable)

class TheOddsAPIProvider(OddsProvider):
    name = "the_odds_api"
    base_url = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, regions="us", markets="h2h,spreads,totals", odds_format="american", bookmakers=""):
        if not api_key:
            raise ProviderError("THE_ODDS_API_KEY is not configured")
        self.api_key = api_key
        self.regions, self.markets, self.odds_format = regions, markets, odds_format
        self.bookmakers = bookmakers
        self.usage = {}

    def _normalize_price(self, value):
        """Validate provider prices before storing integer American odds."""
        if isinstance(value, bool):
            raise ValueError("Sportsbook price must be a finite non-zero number")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Sportsbook price must be a finite non-zero number") from None
        if not math.isfinite(numeric):
            raise ValueError("Sportsbook price must be a finite non-zero number")
        if self.odds_format == "decimal":
            if numeric <= 1:
                raise ValueError("Decimal odds must be greater than 1")
            return decimal_to_american(numeric)
        if numeric == 0 or not numeric.is_integer():
            raise ValueError("American odds must be a non-zero integer")
        if -100 < numeric < 100:
            raise ValueError("American odds must be +100 or greater, or -100 or lower")
        return int(numeric)

    def fetch_player_props(self, sport: str, events: list[dict], markets: str, max_events=5) -> dict:
        """Fetch optional non-featured player markets one event at a time.

        The Odds API requires the event-odds endpoint for player props. This is
        deliberately opt-in because each returned market consumes quota.
        """
        market_keys = [item.strip() for item in markets.split(",") if item.strip()]
        if not market_keys:
            return {"enabled": True, "events_requested": 0, "events_with_props": 0,
                    "markets": [], "error": "No player-prop market keys configured."}
        events_with_props = 0
        errors = []
        requests = 0
        selected_events = events[:max(1, int(max_events))]
        for event in selected_events:
            event_id = event.get("id")
            if not event_id:
                continue
            params = {"apiKey": self.api_key, "markets": ",".join(market_keys),
                      "oddsFormat": self.odds_format}
            if self.bookmakers:
                params["bookmakers"] = self.bookmakers
            else:
                params["regions"] = self.regions
            req = Request(
                f"{self.base_url}/sports/{sport}/events/{event_id}/odds/?{urlencode(params)}",
                headers={"User-Agent": "OpenOddsIntelligence/0.1"},
            )
            try:
                with urlopen(req, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.usage = {key: response.headers.get(header) for key, header in {
                        "requests_remaining": "x-requests-remaining",
                        "requests_used": "x-requests-used",
                        "requests_last": "x-requests-last",
                    }.items() if response.headers.get(header) is not None}
                requests += 1
            except HTTPError as exc:
                headers = exc.headers or {}
                self.usage.update({key: headers.get(header) for key, header in {
                    "requests_remaining": "x-requests-remaining",
                    "requests_used": "x-requests-used",
                    "requests_last": "x-requests-last",
                }.items() if headers.get(header) is not None})
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                errors.append(f"{event_id}: HTTP {exc.code}: {detail}")
                # Stop immediately on quota/auth failures; never hammer a failing key.
                if exc.code in (401, 403, 429) or "OUT_OF_USAGE_CREDITS" in detail:
                    break
                continue
            except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"{event_id}: {exc}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{event_id}: unexpected event-odds response")
                continue
            added = 0
            books = {book.get("key"): book for book in event.get("bookmakers", [])}
            for raw_book in payload.get("bookmakers", []):
                book_key = raw_book.get("key")
                if not book_key:
                    continue
                book = books.get(book_key)
                if book is None:
                    book = {"key": book_key, "title": raw_book.get("title", book_key), "markets": []}
                    event.setdefault("bookmakers", []).append(book)
                    books[book_key] = book
                existing = {market.get("key") for market in book.get("markets", [])}
                for raw_market in raw_book.get("markets", []):
                    market_key = raw_market.get("key")
                    if market_key not in market_keys or market_key in existing:
                        continue
                    outcomes = []
                    for outcome in raw_market.get("outcomes", []):
                        try:
                            price = self._normalize_price(outcome["price"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if not outcome.get("name") or price == 0:
                            continue
                        outcomes.append({
                            "name": outcome["name"],
                            "description": outcome.get("description") or "",
                            "point": outcome.get("point"),
                            "price": price,
                            "updated_at": raw_market.get("last_update"),
                        })
                    if outcomes:
                        book["markets"].append({
                            "key": market_key, "last_update": raw_market.get("last_update"),
                            "outcomes": outcomes,
                        })
                        added += len(outcomes)
            if added:
                events_with_props += 1
        return {"enabled": True, "sport": sport, "markets": market_keys,
                "events_requested": requests, "events_considered": len(selected_events), "events_with_props": events_with_props,
                "error": "; ".join(errors)[:1000] if errors else None,
                "quota": dict(self.usage)}

    def fetch_odds(self, sport: str) -> list[dict]:
        params = {"apiKey": self.api_key, "markets": self.markets,
                  "oddsFormat": self.odds_format}
        # Explicit bookmaker keys take precedence over regions in The Odds API.
        if self.bookmakers:
            params["bookmakers"] = self.bookmakers
        else:
            params["regions"] = self.regions
        query = urlencode(params)
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
            headers = exc.headers or {}
            self.usage.update({key: headers.get(header) for key, header in {
                "requests_remaining": "x-requests-remaining",
                "requests_used": "x-requests-used",
                "requests_last": "x-requests-last",
            }.items() if headers.get(header) is not None})
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"Odds provider HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retry_after=(exc.headers or {}).get("Retry-After"),
                retryable=exc.code in (408, 425, 429, 500, 502, 503, 504),
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"Odds provider request failed: {exc}", retryable=True
            ) from exc
        if not isinstance(payload, list):
            raise ProviderError("Odds provider returned an unexpected response format")

        now = datetime.now(timezone.utc).isoformat()
        events = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            books = []
            raw_books = raw.get("bookmakers")
            if not isinstance(raw_books, list):
                raw_books = []
            for b in raw_books:
                if not isinstance(b, dict):
                    continue
                raw_markets = b.get("markets")
                if not isinstance(raw_markets, list):
                    continue
                markets = []
                for m in raw_markets:
                    if not isinstance(m, dict):
                        continue
                    raw_outcomes = m.get("outcomes")
                    if not isinstance(raw_outcomes, list):
                        continue
                    outcomes = []
                    for raw_outcome in raw_outcomes:
                        if not isinstance(raw_outcome, dict) or not raw_outcome.get("name"):
                            continue
                        try:
                            price = self._normalize_price(raw_outcome["price"])
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                        outcomes.append({
                            "name": raw_outcome.get("name", ""),
                            "description": raw_outcome.get("description") or "",
                            "price": price,
                            "point": raw_outcome.get("point"),
                            "updated_at": m.get("last_update") or b.get("last_update"),
                        })
                    markets.append({"key": m.get("key", ""), "last_update": m.get("last_update"), "outcomes": outcomes})
                books.append({"key": b.get("key", ""), "title": b.get("title", b.get("key", "")),
                              "last_update": b.get("last_update"), "markets": markets})
            events.append({"id": raw.get("id", ""), "sport_key": raw.get("sport_key", sport),
                           "sport_title": raw.get("sport_title", ""),
                           "commence_time": raw.get("commence_time", ""),
                           "home_team": raw.get("home_team", ""), "away_team": raw.get("away_team", ""),
                           "source": self.name, "retrieved_at": now, "bookmakers": books})
        return events
