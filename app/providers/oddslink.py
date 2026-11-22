"""OddsLink free-tier adapter: every listed book, pre-match markets, no paid props."""
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .the_odds_api import ProviderError


class OddsLinkProvider:
    name = "oddslink"
    base_url = "https://oddslink.app/v1"
    SPORTS = {
        "americanfootball_nfl": ("nfl", {}),
        "americanfootball_ncaaf": ("ncaaf", {}),
        "basketball_nba": ("nba", {}),
        "basketball_ncaab": ("ncaab", {}),
        "baseball_mlb": ("mlb", {}),
        "icehockey_nhl": ("nhl", {}),
        "soccer_epl": ("soccer", {"league": "epl"}),
        "soccer_mls": ("soccer", {"league": "mls"}),
        "tennis_atp": ("tennis", {"tournament_prefix": "tennis_atp_"}),
        "tennis_wta": ("tennis", {"tournament_prefix": "tennis_wta_"}),
    }
    MARKETS = "h2h,h2h_3_way,spreads,totals,team_totals"
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    MIN_REQUEST_INTERVAL_SECONDS = 0.55  # 2 requests/second on the free tier.
    _tennis_cache_lock = threading.Lock()
    _tennis_cache_at = 0.0
    _tennis_cache_payload = None
    TENNIS_CACHE_SECONDS = 10.0

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("ODDSLINK_API_KEY is not configured")
        self.api_key = api_key
        self.usage = {}

    @staticmethod
    def _seconds_until(value):
        if not value:
            return None
        try:
            reset = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if reset.tzinfo is None:
                return None
            return max(1, int((reset - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _int_header(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return value

    @classmethod
    def _pace(cls):
        with cls._request_lock:
            wait = cls.MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - cls._last_request_at)
            if wait > 0:
                time.sleep(wait)
            cls._last_request_at = time.monotonic()

    def _request(self, path, params=None):
        self._pace()
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "OpenOddsIntelligence/0.6",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                headers = response.headers
                self.usage.update({
                    "requests_used": self._int_header(headers.get("x-requests-used")),
                    "requests_remaining": self._int_header(headers.get("x-requests-remaining")),
                    "requests_last": self._int_header(headers.get("x-requests-last")),
                    "rate_limit_per_second": self._int_header(headers.get("X-RateLimit-Limit")),
                    "rate_limit_remaining": self._int_header(headers.get("X-RateLimit-Remaining")),
                })
                return payload
        except HTTPError as exc:
            headers = exc.headers or {}
            self.usage.update({
                "requests_used": self._int_header(headers.get("x-requests-used")),
                "requests_remaining": self._int_header(headers.get("x-requests-remaining")),
                "requests_last": self._int_header(headers.get("x-requests-last")),
                "rate_limit_per_second": self._int_header(headers.get("X-RateLimit-Limit")),
                "rate_limit_remaining": self._int_header(headers.get("X-RateLimit-Remaining")),
            })
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"OddsLink HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retry_after=headers.get("Retry-After"),
                retryable=exc.code in (408, 425, 429, 500, 502, 503, 504),
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"OddsLink request failed: {exc}", retryable=True) from exc

    def fetch_usage(self):
        payload = self._request("/account/usage")
        if not isinstance(payload, dict):
            raise ProviderError("OddsLink returned an invalid account-usage response")
        try:
            allowance = payload.get("requests_allowance")
            used = int(payload.get("requests_used_this_period", 0))
            remaining = None if allowance is None else max(0, int(allowance) - used)
        except (TypeError, ValueError, OverflowError):
            raise ProviderError("OddsLink usage response has invalid allowance counters") from None
        self.usage.update({
            "plan": payload.get("plan"),
            "requests_allowance": allowance,
            "requests_used": used,
            "requests_remaining": remaining,
            "period_resets_at": payload.get("period_resets_at"),
            "speed_limit_per_second": payload.get("speed_limit_per_second"),
            "requests_last_second": payload.get("requests_last_second"),
        })
        return remaining

    def _normalize_event(self, raw, sport):
        bookmakers = []
        for raw_book in raw.get("bookmakers") or []:
            if not isinstance(raw_book, dict) or not raw_book.get("key"):
                continue
            markets = []
            for raw_market in raw_book.get("markets") or []:
                if not isinstance(raw_market, dict) or not raw_market.get("key"):
                    continue
                market_key = str(raw_market["key"])
                period = raw_market.get("period")
                if period and str(period).lower() not in ("full", "full_game", "full-game", "game"):
                    market_key = f"{market_key}_{str(period).lower()}"
                outcomes = []
                for raw_outcome in raw_market.get("outcomes") or []:
                    if not isinstance(raw_outcome, dict):
                        continue
                    price = raw_outcome.get("price")
                    if isinstance(price, bool) or not isinstance(price, (int, float)):
                        continue
                    outcome = dict(raw_outcome)
                    outcome["updated_at"] = (
                        raw_outcome.get("last_update")
                        or raw_market.get("last_update")
                        or raw_book.get("last_update")
                    )
                    outcome["source_provider"] = self.name
                    outcomes.append(outcome)
                if outcomes:
                    markets.append({"key": market_key, "outcomes": outcomes})
            if markets:
                bookmakers.append({
                    "key": str(raw_book["key"]),
                    "title": str(raw_book.get("title") or raw_book["key"]),
                    "markets": markets,
                })
        participants = raw.get("participants") or raw.get("competitors") or []
        participant_names = []
        for participant in participants:
            if isinstance(participant, dict):
                name = (
                    participant.get("name") or participant.get("full_name")
                    or participant.get("competitor_name") or participant.get("title")
                )
            else:
                name = participant
            if name:
                participant_names.append(str(name))
        home_team = raw.get("home_team") or (participant_names[0] if participant_names else "")
        away_team = raw.get("away_team") or (participant_names[1] if len(participant_names) > 1 else "")
        return {
            "id": str(raw.get("id") or ""),
            "sport_key": sport,
            "sport_title": str(raw.get("sport_title") or sport),
            "commence_time": raw.get("commence_time") or raw.get("start_time"),
            "home_team": str(home_team),
            "away_team": str(away_team),
            "source": self.name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "bookmakers": bookmakers,
        }

    def fetch_odds(self, sport):
        mapping = self.SPORTS.get(sport)
        if not mapping:
            raise ProviderError(f"OddsLink Free does not cover league '{sport}'")
        api_sport, filters = mapping
        params = {
            "markets": self.MARKETS,
            "periods": "all",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        if "league" in filters:
            params["league"] = filters["league"]

        if "tournament_prefix" in filters:
            # The aggregate tennis endpoint returns ATP and WTA in one priced
            # request. Cache briefly so the two app leagues share that call.
            with self._tennis_cache_lock:
                now = time.monotonic()
                if (self._tennis_cache_payload is not None
                        and now - self._tennis_cache_at <= self.TENNIS_CACHE_SECONDS):
                    payload = self._tennis_cache_payload
                else:
                    remaining = self.fetch_usage()
                    if remaining is not None and remaining <= 0:
                        raise ProviderError(
                            "OddsLink free monthly request allowance is exhausted",
                            status_code=429,
                            retry_after=str(
                                self._seconds_until(self.usage.get("period_resets_at")) or 86400
                            ),
                            retryable=False,
                        )
                    payload = self._request(f"/sports/{api_sport}/odds", params)
                    type(self)._tennis_cache_payload = payload
                    type(self)._tennis_cache_at = time.monotonic()
        else:
            remaining = self.fetch_usage()
            if remaining is not None and remaining <= 0:
                raise ProviderError(
                    "OddsLink free monthly request allowance is exhausted",
                    status_code=429,
                    retry_after=str(
                        self._seconds_until(self.usage.get("period_resets_at")) or 86400
                    ),
                    retryable=False,
                )
            payload = self._request(f"/sports/{api_sport}/odds", params)

        if not isinstance(payload, list):
            raise ProviderError("OddsLink returned an unexpected fixture/odds response")
        events = []
        for raw in payload:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            if "tournament_prefix" in filters:
                tournament = raw.get("tournament") or raw.get("competition") or {}
                tournament_key = str(tournament.get("key") or "").lower()
                prefix = filters["tournament_prefix"].lower()
                alternate_prefix = prefix.replace("tennis_", "", 1)
                if not tournament_key.startswith((prefix, alternate_prefix)):
                    continue
            events.append(self._normalize_event(raw, sport))
        return events
