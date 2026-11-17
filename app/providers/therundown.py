"""TheRundown V2 adapter, deliberately scoped to its free-plan odds feed."""
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .the_odds_api import ProviderError


class TheRundownProvider:
    name = "therundown"
    base_url = "https://therundown.io/api/v2"
    # IDs documented by TheRundown for its three Free-plan sportsbooks.
    BOOKS = {
        "19": ("draftkings", "DraftKings"),
        "22": ("betmgm", "BetMGM"),
        "23": ("fanduel", "FanDuel"),
    }
    SPORTS = {
        "americanfootball_nfl": (2, "NFL"),
        "baseball_mlb": (3, "MLB"),
        "basketball_nba": (4, "NBA"),
        "basketball_ncaab": (5, "NCAAB"),
        "icehockey_nhl": (6, "NHL"),
        "mma_mixed_martial_arts": (7, "UFC/MMA"),
        "basketball_wnba": (8, "WNBA"),
        "soccer_mls": (10, "MLS"),
        "soccer_epl": (11, "EPL"),
        "americanfootball_ncaaf": (1, "NCAAF"),
    }
    MARKETS = {1: "h2h", 2: "spreads", 3: "totals"}

    def __init__(self, api_key: str, utc_offset_minutes=0):
        if not api_key:
            raise ProviderError("THERUNDOWN_API_KEY is not configured")
        self.api_key = api_key
        self.utc_offset_minutes = int(utc_offset_minutes)
        self.usage = {}
        self._last_request_at = 0.0

    def _request(self, url):
        # Free tier is limited to one request per second.
        wait = 1.05 - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={
            "X-TheRundown-Key": self.api_key,
            "User-Agent": "OpenOddsIntelligence/0.5",
            "Accept": "application/json",
        })
        self._last_request_at = time.monotonic()
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                headers = response.headers
                # X-Datapoints is the authoritative billed total for this response.
                new_usage = {
                    "datapoints_total": headers.get("X-Datapoints"),
                    "datapoints_used": headers.get("X-Datapoints-Used"),
                    "datapoints_remaining": headers.get("X-Datapoints-Remaining"),
                    "datapoints_limit": headers.get("X-Datapoints-Limit"),
                    "datapoints_period": headers.get("X-Datapoints-Period"),
                    "data_delay_seconds": headers.get("X-Data-Delay-Seconds"),
                }
                self.usage.update({k: v for k, v in new_usage.items() if v is not None})
                return payload
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            suffix = f" (Retry-After: {retry_after}s)" if retry_after else ""
            raise ProviderError(f"TheRundown HTTP {exc.code}{suffix}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"TheRundown request failed: {exc}") from exc

    @staticmethod
    def _as_float(value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_time(event):
        value = event.get("event_date") or event.get("start_date") or event.get("commence_time")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        return value or ""

    def fetch_odds(self, sport: str) -> list[dict]:
        if sport not in self.SPORTS:
            raise ProviderError(f"TheRundown does not currently map sport '{sport}'")
        sport_id, sport_title = self.SPORTS[sport]
        event_sport_id, date = self._next_event_slate(sport_id)
        if not date:
            return []
        params = urlencode({
            "market_ids": "1,2,3",
            "affiliate_ids": "19,22,23",
            "main_line": "true",
            "hide_closed": "true",
            "offset": str(self.utc_offset_minutes),
        })
        payload = self._request(
            f"{self.base_url}/sports/{event_sport_id}/events/{date}?{params}"
        )
        raw_events = payload.get("events", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_events, list):
            raise ProviderError("TheRundown returned an unexpected events response")
        return [self._normalize_event(raw, sport, sport_title) for raw in raw_events
                if isinstance(raw, dict)]

    def _next_event_slate(self, sport_id):
        # Schedule/date discovery is reference data; choose the next actual slate.
        date_sports = [sport_id]
        if sport_id == 2:
            date_sports = [2, 25, 26]
        params = urlencode({
            "sport_ids": ",".join(str(value) for value in date_sports),
            "format": "epoch",
            "exclude_canceled": "true",
        })
        payload = self._request(f"{self.base_url}/sports/dates?{params}")
        today = datetime.now(timezone.utc).date()
        candidates = []
        if isinstance(payload, dict):
            for returned_sport, body in payload.items():
                if not isinstance(body, dict):
                    continue
                try:
                    returned_id = int(returned_sport)
                except (TypeError, ValueError):
                    continue
                for stamp in body.get("dates") or []:
                    try:
                        epoch = float(stamp)
                    except (TypeError, ValueError):
                        continue
                    event_date = datetime.fromtimestamp(epoch, timezone.utc).date()
                    if event_date >= today:
                        candidates.append((epoch, returned_id))
        if not candidates:
            return sport_id, None
        epoch, selected_sport_id = min(candidates)
        return selected_sport_id, datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()

    def _normalize_event(self, raw, sport, sport_title):
        event_id = raw.get("event_id") or raw.get("id") or ""
        teams = raw.get("teams") or []
        home = raw.get("home_team") or raw.get("home") or ""
        away = raw.get("away_team") or raw.get("away") or ""
        if isinstance(teams, list):
            for team in teams:
                if not isinstance(team, dict):
                    continue
                name = team.get("name") or team.get("team_name") or ""
                home_flag = team.get("is_home")
                side = str(team.get("home_away") or team.get("side") or "").lower()
                if home_flag is True or side in ("home", "h"):
                    home = name
                elif home_flag is False or side in ("away", "a"):
                    away = name
        books = {}
        for market in raw.get("markets") or []:
            if not isinstance(market, dict):
                continue
            market_key = self.MARKETS.get(market.get("market_id"))
            if not market_key:
                continue
            for participant in market.get("participants") or []:
                if not isinstance(participant, dict):
                    continue
                outcome_name = participant.get("name") or ""
                for line in participant.get("lines") or []:
                    if not isinstance(line, dict):
                        continue
                    point = self._as_float(line.get("value"))
                    for book_id, quote in (line.get("prices") or {}).items():
                        if not isinstance(quote, dict) or quote.get("closed_at"):
                            continue
                        price_value = self._as_float(quote.get("price"))
                        if price_value is None or price_value == 0 or price_value == 0.0001:
                            continue
                        price = int(price_value)
                        key, title = self.BOOKS.get(str(book_id), (f"affiliate_{book_id}", f"Affiliate {book_id}"))
                        book = books.setdefault(key, {"key": key, "title": title, "markets": {}})
                        market_obj = book["markets"].setdefault(market_key, {"key": market_key, "outcomes": []})
                        market_obj["outcomes"].append({
                            "name": outcome_name,
                            "price": price,
                            "point": point,
                            "updated_at": quote.get("updated_at") or quote.get("last_update") or
                                          quote.get("updated") or line.get("updated_at"),
                        })
        bookmakers = []
        for book in books.values():
            bookmakers.append({
                "key": book["key"], "title": book["title"],
                "markets": list(book["markets"].values()),
            })
        return {
            "id": str(event_id),
            "sport_key": sport,
            "sport_title": sport_title,
            "commence_time": self._event_time(raw),
            "home_team": home,
            "away_team": away,
            "source": self.name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "bookmakers": bookmakers,
        }
