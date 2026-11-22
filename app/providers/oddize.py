"""Oddize Developer-tier adapter with credit preflight and rotating prop coverage."""
import json
import math
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .the_odds_api import ProviderError


class OddizeProvider:
    name = "oddize"
    base_url = "https://oddize.com/api/v1"
    SPORTS = {
        "baseball_mlb": "mlb",
        "basketball_nba": "nba",
        "americanfootball_nfl": "nfl",
        "basketball_ncaab": "ncaabm",
        "americanfootball_ncaaf": "ncaaf",
        "icehockey_nhl": "nhl",
    }
    BOOKS = {
        "b365": "bet365", "blys": "ballys", "mgm": "betmgm",
        "boag": "betonlineag", "briv": "betrivers", "bov": "bovada",
        "czr": "caesars", "circa": "circa", "dk": "draftkings",
        "espn": "espnbet", "fd": "fanduel", "gnug": "goldennugget",
        "hrb": "hardrock", "novig": "novig", "parx": "parx",
        "pin": "pinnacle", "pb": "pointsbet", "poly": "polymarket",
        "phtx": "prophetx", "smkt": "smarkets",
    }
    PROP_MARKETS = {
        "player_pts": "player_points",
        "player_reb": "player_rebounds",
        "player_ast": "player_assists",
        "player_3pm": "player_threes",
        "player_pra": "player_points_rebounds_assists",
        "player_stl": "player_steals",
        "player_blk": "player_blocks",
        "player_pass_yds": "player_pass_yards",
        "player_rush_yds": "player_rush_yards",
        "player_rec_yds": "player_receiving_yards",
        "player_pass_td": "player_passing_touchdowns",
        "player_atd": "player_anytime_touchdown",
        "player_rec": "player_receptions",
        "pitcher_k": "pitcher_strikeouts",
        "batter_h": "batter_hits",
        "batter_tb": "batter_total_bases",
        "batter_hr": "batter_home_runs",
        "batter_rbi": "batter_rbis",
        "batter_r": "batter_runs",
        "player_g": "player_goals",
        "player_sog": "player_shots_on_goal",
        "player_ppp": "player_power_play_points",
        "tt_pts": "team_points",
        "tt_goals": "team_goals",
    }
    OFFER_MARKETS = {
        "ml": "h2h", "s": "spreads", "t": "totals",
        "rl": "run_line", "tt": "team_totals",
        "alt_s": "spreads", "alt_t": "totals",
    }

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("ODDIZE_API_KEY is not configured")
        self.api_key = api_key
        self.usage = {}
        self._sports_metadata = None

    @staticmethod
    def _price(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(str(value).replace("+", ""))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or not number.is_integer():
            return None
        price = int(number)
        return price if price != 0 and not (-100 < price < 100) else None

    @staticmethod
    def _line(value):
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _header_value(value):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return value

    def _request(self, path, params=None):
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={"X-API-Key": self.api_key, "Accept": "application/json",
                     "User-Agent": "OpenOddsIntelligence/0.6"},
        )
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                headers = response.headers
                self.usage.update({
                    "credits_remaining": self._header_value(headers.get("X-Credits-Remaining")),
                    "credits_cost": self._header_value(headers.get("X-Credits-Cost")),
                    "rate_limit_rpm": self._header_value(headers.get("X-RateLimit-Limit")),
                    "rate_limit_remaining": self._header_value(headers.get("X-RateLimit-Remaining")),
                })
                return payload
        except HTTPError as exc:
            headers = exc.headers or {}
            self.usage.update({
                "credits_remaining": headers.get("X-Credits-Remaining"),
                "credits_cost": headers.get("X-Credits-Cost"),
                "rate_limit_rpm": headers.get("X-RateLimit-Limit"),
                "rate_limit_remaining": headers.get("X-RateLimit-Remaining"),
            })
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"Oddize HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retry_after=headers.get("Retry-After"),
                retryable=exc.code in (408, 425, 429, 500, 502, 503, 504),
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"Oddize request failed: {exc}", retryable=True) from exc

    def fetch_usage(self):
        payload = self._request("/account/usage")
        if not isinstance(payload, dict):
            raise ProviderError("Oddize returned an invalid account usage response")
        try:
            remaining = int(payload["credits_remaining"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("Oddize usage response omitted credits_remaining") from None
        self.usage.update({
            "plan_id": payload.get("plan_id"),
            "credits_limit": payload.get("credits_limit"),
            "credits_used": payload.get("credits_used"),
            "credits_remaining": remaining,
            "requests_made": payload.get("requests_made"),
            "rate_limit_rpm": payload.get("rate_limit_rpm"),
            "period_start": payload.get("period_start"),
            "period_end": payload.get("period_end"),
            "resets_in_seconds": payload.get("resets_in_seconds"),
        })
        return remaining

    def _require_credits(self, amount):
        remaining = self.fetch_usage()
        if remaining < amount:
            reset = self.usage.get("resets_in_seconds")
            raise ProviderError(
                f"Oddize free Developer allowance has {remaining} credits; "
                f"{amount} required for this request",
                status_code=402,
                retry_after=str(reset) if reset is not None else None,
                retryable=False,
            )

    def fetch_sports_metadata(self):
        if self._sports_metadata is None:
            payload = self._request("/sports")
            self._sports_metadata = payload.get("sports", []) if isinstance(payload, dict) else []
        return self._sports_metadata

    def _normalize_event(self, raw, sport):
        home = str(raw.get("team2") or "").strip()
        away = str(raw.get("team1") or "").strip()
        books = {}
        for quote in raw.get("odds") or []:
            if not isinstance(quote, dict):
                continue
            price = self._price(quote.get("american_odds"))
            if price is None:
                continue
            offer = str(quote.get("offer_type") or "")
            market_key = self.OFFER_MARKETS.get(
                offer, self.OFFER_MARKETS.get(str(quote.get("market") or "").lower())
            )
            if not market_key:
                continue
            selection = str(quote.get("team") or "").strip()
            if selection == home:
                selection = home
            elif selection == away:
                selection = away
            if not selection:
                continue
            book_code = str(quote.get("book") or "").lower()
            if not book_code:
                continue
            book_key = self.BOOKS.get(book_code, book_code)
            book = books.setdefault(book_key, {
                "key": book_key,
                "title": str(quote.get("book_name") or book_key),
                "markets": {},
            })
            market = book["markets"].setdefault(market_key, {
                "key": market_key, "outcomes": [],
            })
            market["outcomes"].append({
                "name": selection,
                "description": "",
                "point": self._line(quote.get("line")),
                "price": price,
                "updated_at": quote.get("timestamp"),
                "source_provider": self.name,
            })
        return {
            "id": str(raw.get("event_id") or ""),
            "sport_key": sport,
            "sport_title": sport,
            "commence_time": str(raw.get("start_date") or ""),
            "home_team": home,
            "away_team": away,
            "source": self.name,
            "retrieved_at": None,
            "bookmakers": [
                {"key": book["key"], "title": book["title"],
                 "markets": list(book["markets"].values())}
                for book in books.values()
            ],
        }

    def fetch_odds(self, sport):
        api_sport = self.SPORTS.get(sport)
        if not api_sport:
            raise ProviderError(f"Oddize Developer tier does not cover league '{sport}'")
        self._require_credits(1)
        payload = self._request("/odds/latest", {
            "sport": api_sport,
            "limit": 100,
            "offer_types": ["s", "t", "ml", "rl", "tt"],
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ProviderError("Oddize returned an unexpected odds response")
        return [
            self._normalize_event(event, sport)
            for event in payload["events"]
            if isinstance(event, dict) and event.get("event_id")
        ]

    def fetch_alternate_lines(self, sport, events, *, max_events=1):
        """Add alternate spreads/totals for a small, configurable upcoming slate."""
        api_sport = self.SPORTS.get(sport)
        if not api_sport or max_events <= 0:
            return {"events_requested": 0, "alternate_quotes_added": 0}
        selected = [event for event in events if event.get("id")][:int(max_events)]
        quotes_added = 0
        for event in selected:
            for alt_type, market_key in (("alt_s", "spreads"), ("alt_t", "totals")):
                self._require_credits(2)
                payload = self._request(
                    f"/events/{api_sport}/{event['id']}/alts",
                    {"alt_type": alt_type},
                )
                for line_group in payload.get("lines", []) if isinstance(payload, dict) else []:
                    if not isinstance(line_group, dict):
                        continue
                    group_line = self._line(line_group.get("line"))
                    for quote in line_group.get("prices") or []:
                        if not isinstance(quote, dict):
                            continue
                        price = self._price(quote.get("american_odds"))
                        book_code = str(quote.get("book") or "").lower()
                        selection = str(quote.get("team") or "").strip()
                        if price is None or not book_code or not selection:
                            continue
                        book_key = self.BOOKS.get(book_code, book_code)
                        book_map = {book.get("key"): book for book in event["bookmakers"]}
                        book = book_map.get(book_key)
                        if book is None:
                            book = {
                                "key": book_key,
                                "title": str(quote.get("book_name") or book_key),
                                "markets": [],
                            }
                            event["bookmakers"].append(book)
                        market_map = {market.get("key"): market for market in book["markets"]}
                        market = market_map.get(market_key)
                        if market is None:
                            market = {"key": market_key, "outcomes": []}
                            book["markets"].append(market)
                        market["outcomes"].append({
                            "name": selection,
                            "description": "",
                            "point": self._line(quote.get("line")) if quote.get("line") is not None else group_line,
                            "price": price,
                            "updated_at": quote.get("timestamp"),
                            "source_provider": self.name,
                        })
                        quotes_added += 1
        return {
            "events_requested": len(selected),
            "alternate_quotes_added": quotes_added,
            "credits_remaining": self.usage.get("credits_remaining"),
            "resets_in_seconds": self.usage.get("resets_in_seconds"),
        }

    def fetch_props(self, sport, events, *, max_prop_types=1, cursor=0):
        api_sport = self.SPORTS.get(sport)
        if not api_sport or max_prop_types <= 0:
            return {"prop_types_requested": [], "props_added": 0, "next_cursor": cursor}
        metadata = self.fetch_sports_metadata()
        sport_metadata = next(
            (item for item in metadata if item.get("sport") == api_sport), {}
        )
        prop_types = [
            str(item.get("prop_type"))
            for item in sport_metadata.get("prop_types", [])
            if isinstance(item, dict) and item.get("prop_type")
        ]
        if not prop_types:
            return {"prop_types_requested": [], "props_added": 0, "next_cursor": 0}
        start = int(cursor or 0) % len(prop_types)
        selected = [
            prop_types[(start + offset) % len(prop_types)]
            for offset in range(min(int(max_prop_types), len(prop_types)))
        ]
        event_map = {str(event.get("id")): event for event in events}
        props_added = 0
        requested = []
        for prop_type in selected:
            self._require_credits(2)
            payload = self._request(f"/props/{api_sport}", {
                "prop_type": prop_type, "limit": 100,
            })
            requested.append(prop_type)
            for prop_event in (payload.get("events") or []) if isinstance(payload, dict) else []:
                if not isinstance(prop_event, dict) or not prop_event.get("event_id"):
                    continue
                event_id = str(prop_event["event_id"])
                event = event_map.get(event_id)
                if event is None:
                    event = {
                        "id": event_id, "sport_key": sport, "sport_title": sport,
                        "commence_time": str(prop_event.get("start_date") or ""),
                        "home_team": str(prop_event.get("team2") or ""),
                        "away_team": str(prop_event.get("team1") or ""),
                        "source": self.name, "bookmakers": [],
                    }
                    events.append(event)
                    event_map[event_id] = event
                books = {book.get("key"): book for book in event["bookmakers"]}
                for subject in prop_event.get("players") or []:
                    if not isinstance(subject, dict):
                        continue
                    subject_name = str(
                        subject.get("player_name") or subject.get("subject") or ""
                    ).strip()
                    subject_type = str(subject.get("subject_type") or "player")
                    raw_prices = subject.get("all_odds") or subject.get("odds") or []
                    for quote in raw_prices:
                        if not isinstance(quote, dict):
                            continue
                        price = self._price(quote.get("american_odds"))
                        if price is None:
                            continue
                        book_code = str(quote.get("book") or "").lower()
                        if not book_code:
                            continue
                        book_key = self.BOOKS.get(book_code, book_code)
                        book = books.get(book_key)
                        if book is None:
                            book = {
                                "key": book_key,
                                "title": str(quote.get("book_name") or book_key),
                                "markets": [],
                            }
                            event["bookmakers"].append(book)
                            books[book_key] = book
                        market_key = self.PROP_MARKETS.get(
                            prop_type, prop_type.replace("-", "_")
                        )
                        markets = {market.get("key"): market for market in book["markets"]}
                        market = markets.get(market_key)
                        if market is None:
                            market = {"key": market_key, "outcomes": []}
                            book["markets"].append(market)
                            markets[market_key] = market
                        selection = str(quote.get("team") or quote.get("side") or "").strip()
                        if not selection:
                            continue
                        market["outcomes"].append({
                            "name": selection,
                            "description": subject_name if subject_type == "player" else subject_name,
                            "point": self._line(quote.get("line") or subject.get("main_line")),
                            "price": price,
                            "updated_at": quote.get("timestamp"),
                            "source_provider": self.name,
                        })
                        props_added += 1
        return {
            "prop_types_requested": requested,
            "props_added": props_added,
            "next_cursor": (start + len(requested)) % len(prop_types),
            "prop_types_available": len(prop_types),
            "credits_remaining": self.usage.get("credits_remaining"),
            "resets_in_seconds": self.usage.get("resets_in_seconds"),
        }
