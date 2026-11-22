"""SportsGameOdds v2 adapter, scoped to its permanently free Amateur tier."""
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .the_odds_api import ProviderError


class SportsGameOddsProvider:
    name = "sports_game_odds"
    base_url = "https://api.sportsgameodds.com/v2"
    # The provider's published Amateur tier covers these eight leagues.
    SPORTS = {
        "americanfootball_nfl": "NFL",
        "americanfootball_ncaaf": "NCAAF",
        "basketball_nba": "NBA",
        "basketball_ncaab": "NCAAB",
        "baseball_mlb": "MLB",
        "icehockey_nhl": "NHL",
        "soccer_mls": "MLS",
        "soccer_uefa_champs_league": "UCL",
    }
    BOOKS = {
        "draftkings": "DraftKings",
        "fanduel": "FanDuel",
        "betmgm": "BetMGM",
        "caesars": "Caesars",
        "espnbet": "ESPN BET",
        "bovada": "Bovada",
        "unibet": "Unibet",
        "pointsbet": "PointsBet",
        "williamhill": "William Hill",
    }
    # Amateur is documented at 10 requests/minute. Share pacing across
    # adapter instances so refreshing several leagues cannot exceed it.
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    MIN_REQUEST_INTERVAL_SECONDS = 6.1

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("SPORTSGAMEODDS_API_KEY is not configured")
        self.api_key = api_key
        self.max_events = max(1, min(100, int(os.getenv("SPORTSGAMEODDS_MAX_EVENTS", "25"))))
        self.usage = {}

    @staticmethod
    def _price(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(str(value).replace("+", ""))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or not number.is_integer():
            return None
        price = int(number)
        if price == 0 or -100 < price < 100:
            return None
        return price

    @staticmethod
    def _point(value):
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _team_name(team):
        if not isinstance(team, dict):
            return ""
        names = team.get("names") or {}
        if isinstance(names, dict):
            return str(names.get("long") or names.get("medium") or names.get("short") or "").strip()
        return str(team.get("name") or "").strip()

    @classmethod
    def _pace(cls):
        with cls._request_lock:
            wait = cls.MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - cls._last_request_at)
            if wait > 0:
                time.sleep(wait)
            cls._last_request_at = time.monotonic()

    def _request(self, url):
        self._pace()
        request = Request(url, headers={
            "X-API-Key": self.api_key,
            "User-Agent": "OpenOddsIntelligence/0.6",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.usage.update({
                    key: response.headers.get(header)
                    for key, header in {
                        "requests_remaining": "x-ratelimit-remaining",
                        "requests_limit": "x-ratelimit-limit",
                        "objects_remaining": "x-objects-remaining",
                        "objects_used": "x-objects-used",
                    }.items() if response.headers.get(header) is not None
                })
                return payload
        except HTTPError as exc:
            headers = exc.headers or {}
            self.usage.update({
                key: headers.get(header)
                for key, header in {
                    "requests_remaining": "x-ratelimit-remaining",
                    "requests_limit": "x-ratelimit-limit",
                    "objects_remaining": "x-objects-remaining",
                    "objects_used": "x-objects-used",
                }.items() if headers.get(header) is not None
            })
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"SportsGameOdds HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retry_after=headers.get("Retry-After"),
                retryable=exc.code in (408, 425, 429, 500, 502, 503, 504),
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                f"SportsGameOdds request failed: {exc}", retryable=True
            ) from exc

    def fetch_usage(self):
        """Read authoritative tier and rolling 30-day object allowance (free endpoint)."""
        payload = self._request(f"{self.base_url}/account/usage")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ProviderError("SportsGameOdds returned an unexpected account-usage response")
        data = payload["data"]
        limits = data.get("rateLimits") or {}
        monthly = limits.get("per-month") or {}
        try:
            maximum = int(monthly.get("max-entities"))
            used = int(monthly.get("current-entities", 0))
        except (TypeError, ValueError, OverflowError):
            raise ProviderError(
                "SportsGameOdds did not return a usable monthly object allowance; "
                "refresh blocked to protect the free-tier quota"
            ) from None
        remaining = max(0, maximum - used)
        self.usage.update({
            "tier": str(data.get("tier") or "unknown"),
            "monthly_objects_limit": maximum,
            "monthly_objects_used": used,
            "monthly_objects_remaining": remaining,
            "monthly_reset": monthly.get("resetsAt") or monthly.get("resetAt"),
        })
        return remaining

    def _normalize_event(self, raw, sport):
        teams = raw.get("teams") or {}
        home = self._team_name(teams.get("home"))
        away = self._team_name(teams.get("away"))
        status = raw.get("status") or {}
        commence = status.get("startsAt") or raw.get("startTime") or raw.get("commenceTime") or ""
        players = {}
        for player in raw.get("players") or []:
            if not isinstance(player, dict):
                continue
            player_id = str(player.get("playerID") or player.get("id") or "")
            name = player.get("name") or player.get("fullName") or player.get("nickname") or ""
            if player_id and name:
                players[player_id] = str(name)

        books = {}
        odds = raw.get("odds") or {}
        if not isinstance(odds, dict):
            odds = {}
        for odd_id, odd in odds.items():
            if not isinstance(odd, dict):
                continue
            bet_type = str(odd.get("betTypeID") or "")
            period = str(odd.get("periodID") or "game")
            entity = str(odd.get("statEntityID") or "")
            side = str(odd.get("sideID") or "").lower()
            stat = str(odd.get("statID") or "")
            player_id = str(odd.get("playerID") or entity)
            if bet_type in ("ml", "ml3way"):
                market_key = "h2h" if bet_type == "ml" else "h2h_3way"
                outcome_name = home if side == "home" else away if side == "away" else (
                    "Draw" if side == "draw" else side.title()
                )
                description = ""
                point_field = None
            elif bet_type == "sp":
                market_key = "spreads"
                outcome_name = home if side == "home" else away if side == "away" else side.title()
                description = ""
                point_field = "spread"
            elif bet_type == "ou" and entity == "all" and stat == "points":
                market_key = "totals"
                outcome_name = side.title()
                description = ""
                point_field = "overUnder"
            elif bet_type == "ou" and entity in ("home", "away") and stat == "points":
                market_key = "team_totals"
                outcome_name = side.title()
                description = home if entity == "home" else away
                point_field = "overUnder"
            elif bet_type in ("ou", "yn", "prop") and entity not in ("home", "away", "all", ""):
                market_key = "player_" + stat
                outcome_name = side.title() or str(odd.get("marketName") or "Selection")
                description = (
                    players.get(player_id)
                    or odd.get("playerName")
                    or ((odd.get("player") or {}).get("name", "")
                        if isinstance(odd.get("player"), dict) else "")
                    or player_id
                )
                point_field = "overUnder" if bet_type == "ou" else None
            else:
                continue
            if not outcome_name:
                continue
            if period != "game":
                market_key = f"{market_key}_{period}"
            by_book = odd.get("byBookmaker") or {}
            if not isinstance(by_book, dict):
                continue
            for book_key, quote in by_book.items():
                if not isinstance(quote, dict) or quote.get("available") is False:
                    continue
                price = self._price(quote.get("odds"))
                if price is None:
                    continue
                point = self._point(quote.get(point_field)) if point_field else None
                book = books.setdefault(book_key, {
                    "key": str(book_key),
                    "title": self.BOOKS.get(str(book_key).lower(), str(book_key)),
                    "markets": {},
                })
                market = book["markets"].setdefault(market_key, {
                    "key": market_key, "outcomes": [],
                })
                market["outcomes"].append({
                    "name": outcome_name,
                    "description": description,
                    "point": point,
                    "price": price,
                    "updated_at": quote.get("lastUpdatedAt"),
                    "source_provider": self.name,
                })
                for alternate in quote.get("altLines") or []:
                    if not isinstance(alternate, dict) or alternate.get("available") is False:
                        continue
                    alternate_price = self._price(alternate.get("odds"))
                    if alternate_price is None:
                        continue
                    alternate_point = self._point(
                        alternate.get(point_field) if point_field else None
                    )
                    if alternate_point is None:
                        continue
                    market["outcomes"].append({
                        "name": outcome_name,
                        "description": description,
                        "point": alternate_point,
                        "price": alternate_price,
                        "updated_at": alternate.get("lastUpdatedAt") or quote.get("lastUpdatedAt"),
                        "source_provider": self.name,
                    })

        bookmakers = []
        for book in books.values():
            bookmakers.append({
                "key": book["key"], "title": book["title"],
                "markets": list(book["markets"].values()),
            })
        return {
            "id": str(raw.get("eventID") or raw.get("id") or ""),
            "sport_key": sport,
            "sport_title": raw.get("leagueName") or raw.get("leagueID") or self.SPORTS[sport],
            "commence_time": commence,
            "home_team": home,
            "away_team": away,
            "source": self.name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "bookmakers": bookmakers,
        }

    def fetch_odds(self, sport: str) -> list[dict]:
        league = self.SPORTS.get(sport)
        if not league:
            raise ProviderError(
                f"SportsGameOdds Free does not include the configured league '{sport}'"
            )
        monthly_remaining = self.fetch_usage()
        if monthly_remaining <= 0:
            raise ProviderError(
                "SportsGameOdds rolling 30-day free object allowance is exhausted",
                status_code=429, retry_after="86400", retryable=True,
            )
        params = urlencode({
            "leagueID": league,
            "oddsAvailable": "true",
            "includeAltLines": "true",
            "limit": str(min(self.max_events, monthly_remaining)),
        })
        payload = self._request(f"{self.base_url}/events/?{params}")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("SportsGameOdds returned an unexpected events response")
        return [
            self._normalize_event(event, sport)
            for event in payload["data"]
            if isinstance(event, dict)
            and (event.get("eventID") or event.get("id"))
        ]
