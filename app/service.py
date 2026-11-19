from datetime import datetime, timezone
import hashlib
import threading
import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from . import config
from .database import OddsDatabase
from .providers.the_odds_api import TheOddsAPIProvider, ProviderError
from .providers.therundown import TheRundownProvider
from .markets import analyze_markets


class OddsService:
    def __init__(self, database=None):
        self.db = database or OddsDatabase()
        self._refresh_lock = threading.Lock()
        self._cache = {}
        self._last_error = None
        self._updated_at = None
        self._provider_usage = {}
        saved_usage_provider = self.db.get_state("provider_usage_provider")
        saved_usage_fingerprint = self.db.get_state("provider_usage_key_fingerprint")
        if (saved_usage_provider == config.ODDS_PROVIDER and
            saved_usage_fingerprint == self._current_provider_key_fingerprint()):
            try:
                self._provider_usage = json.loads(self.db.get_state("provider_usage") or "{}")
            except (TypeError, ValueError):
                self._provider_usage = {}
        self._player_props_status = {"enabled": False, "reason": "Disabled by default."}
        stored_props_attempt = self.db.get_state("player_props_last_attempt")
        try:
            self._player_props_last_attempt = (
                datetime.fromisoformat(stored_props_attempt.replace("Z", "+00:00"))
                if stored_props_attempt else None
            )
            if (self._player_props_last_attempt is not None and
                self._player_props_last_attempt.tzinfo is None):
                self._player_props_last_attempt = self._player_props_last_attempt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            self._player_props_last_attempt = None
        if config.PLAYER_PROPS_ENABLED:
            if config.ODDS_PROVIDER != "the_odds_api":
                self._player_props_status = {
                    "enabled": True, "sport": config.PLAYER_PROPS_SPORT,
                    "error": "Player props currently require The Odds API event-odds endpoint; TheRundown supplies main markets only.",
                }
            elif config.PLAYER_PROPS_SPORT not in config.ODDS_SPORTS:
                self._player_props_status = {
                    "enabled": True, "sport": config.PLAYER_PROPS_SPORT,
                    "error": "PLAYER_PROPS_SPORT must also be included in ODDS_SPORTS.",
                }
            else:
                remaining = 0
                if self._player_props_last_attempt is not None:
                    remaining = max(0, int(
                        config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS -
                        (datetime.now(timezone.utc) - self._player_props_last_attempt).total_seconds()
                    ))
                self._player_props_status = {
                    "enabled": True, "sport": config.PLAYER_PROPS_SPORT,
                    "markets": [m.strip() for m in config.PLAYER_PROPS_MARKETS.split(",") if m.strip()],
                    "events_requested": 0, "events_with_props": 0,
                    "reason": "Waiting for the next quota-safe player-props refresh.",
                    "next_refresh_seconds": remaining,
                    "refresh_interval_seconds": config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS,
                }
        self._alert_error = None
        self._alerts_evaluated = 0
        self._alerts_qualified = 0
        self._last_alert_check = None
        self._last_alert_evaluated = 0
        self._last_alert_qualified = 0
        self._last_alert_sent = 0
        self._last_alert_error = None

    def _current_provider_key_fingerprint(self):
        if config.ODDS_PROVIDER == "therundown":
            key = config.THERUNDOWN_API_KEY
        elif config.ODDS_PROVIDER == "the_odds_api":
            key = config.THE_ODDS_API_KEY
        else:
            key = ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""

    def _remember_provider_usage(self, usage):
        if not usage:
            return
        self._provider_usage = dict(usage)
        self.db.set_state("provider_usage", json.dumps(self._provider_usage))
        self.db.set_state("provider_usage_provider", config.ODDS_PROVIDER)
        self.db.set_state("provider_usage_key_fingerprint", self._current_provider_key_fingerprint())

    @property
    def configured(self):
        if config.ODDS_PROVIDER == "the_odds_api":
            return bool(config.THE_ODDS_API_KEY)
        if config.ODDS_PROVIDER == "therundown":
            return bool(config.THERUNDOWN_API_KEY)
        return False

    def status(self):
        stored = self.db.snapshot_status()
        return {"provider": config.ODDS_PROVIDER, "configured": self.configured,
                "last_updated": self._updated_at or stored.get("last_updated"),
                "last_error": self._last_error, "sports": config.ODDS_SPORTS,
                "odds_regions": config.ODDS_REGIONS,
                "odds_bookmakers": config.ODDS_BOOKMAKERS,
                "live_data_available": bool(stored.get("events")) or bool(self._cache),
                "stored_snapshots": stored["snapshots"], "stored_events": stored["events"],
                "provider_usage": self._provider_usage,
                "player_props": self._player_props_status,
                "refresh_interval_seconds": config.RUNDOWN_REFRESH_INTERVAL_SECONDS if config.ODDS_PROVIDER == "therundown" else (config.ODDS_REFRESH_INTERVAL_SECONDS or (config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS if config.DISCORD_WEBHOOK_URL else 0)),
                "alert_refresh_interval_seconds": config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS if config.DISCORD_WEBHOOK_URL else 0,
                "alerts_evaluated": self._alerts_evaluated,
                "alerts_qualified": self._alerts_qualified,
                "last_alert_check": self._last_alert_check,
                "last_alert_evaluated": self._last_alert_evaluated,
                "last_alert_qualified": self._last_alert_qualified,
                "last_alert_sent": self._last_alert_sent,
                "last_alert_error": self._last_alert_error,
                "alerts_enabled": bool(config.DISCORD_WEBHOOK_URL),
                "alerts_sent": self.db.alert_status()["sent"],
                "last_alert_at": self.db.alert_status()["last_sent"],
                "alert_error": self._alert_error}

    def refresh(self, sport=None):
        # Prevent manual clicks and the background scheduler from overlapping
        # provider requests and consuming duplicate quota.
        if not self._refresh_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True,
                    "error": "A refresh is already running; this request was skipped to protect provider quota.",
                    "events": 0}
        try:
            return self._refresh_locked(sport)
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self, sport=None):
        if not self.configured:
            self._last_error = "No live odds provider configured. Configure a supported provider and API key."
            return {"ok": False, "error": self._last_error, "events": 0}
        provider = None
        if config.ODDS_PROVIDER == "therundown":
            provider = TheRundownProvider(
                config.THERUNDOWN_API_KEY,
                utc_offset_minutes=config.RUNDOWN_UTC_OFFSET_MINUTES,
            )
        elif config.ODDS_PROVIDER == "the_odds_api":
            provider = TheOddsAPIProvider(config.THE_ODDS_API_KEY, config.ODDS_REGIONS,
                                          config.ODDS_MARKETS, config.ODDS_FORMAT,
                                          config.ODDS_BOOKMAKERS)
        else:
            self._last_error = f"Unsupported odds provider: {config.ODDS_PROVIDER}"
            return {"ok": False, "error": self._last_error, "events": 0}
        count = 0
        try:
            for key in ([sport] if sport else config.ODDS_SPORTS):
                events = provider.fetch_odds(key)
                self._remember_provider_usage(provider.usage)
                if config.PLAYER_PROPS_ENABLED and key == config.PLAYER_PROPS_SPORT:
                    if config.ODDS_PROVIDER == "the_odds_api":
                        now = datetime.now(timezone.utc)
                        due = (self._player_props_last_attempt is None or
                               (now - self._player_props_last_attempt).total_seconds() >=
                               config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS)
                        if due:
                            self._player_props_last_attempt = now
                            self.db.set_state("player_props_last_attempt", now.isoformat())
                            self._player_props_status = provider.fetch_player_props(
                                key, events, config.PLAYER_PROPS_MARKETS,
                                config.PLAYER_PROPS_MAX_EVENTS,
                            )
                            self._player_props_status["last_updated"] = now.isoformat()
                            self._player_props_status["refresh_interval_seconds"] = config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS
                            self._remember_provider_usage(provider.usage)
                        else:
                            remaining = max(
                                0, int(config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS -
                                       (now - self._player_props_last_attempt).total_seconds())
                            )
                            self._player_props_status = {
                                "enabled": True, "sport": key, "events_requested": 0,
                                "events_with_props": 0,
                                "reason": "Waiting for the quota-safe player-props refresh interval.",
                                "next_refresh_seconds": remaining,
                                "refresh_interval_seconds": config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS,
                            }
                    else:
                        self._player_props_status = {
                            "enabled": True, "sport": key, "events_requested": 0,
                            "events_with_props": 0,
                            "error": "Player props currently require The Odds API event-odds endpoint; TheRundown adapter supplies main markets only.",
                        }
                fetched_at = datetime.now(timezone.utc).isoformat()
                self.db.save_snapshot(key, events, provider.name, fetched_at)
                self._cache[key] = events
                # Capture the first positive-EV quote for each exact selection.
                # The entry is immutable so later refreshes cannot rewrite the
                # price whose closing-line value is being measured.
                clv_candidates = analyze_markets(
                    events, min_ev=config.CLV_MIN_EV_PERCENT,
                    min_consensus_books=2, best_only=False,
                )["opportunities"]
                self.db.record_clv_candidates(clv_candidates, fetched_at)
                count += len(events)
            self._updated_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            alerts_sent = self._dispatch_alerts()
            alert_status = self.status()
            return {"ok": True, "events": count, "updated_at": self._updated_at,
                    "alerts_sent": alerts_sent,
                    "alerts_enabled": alert_status["alerts_enabled"],
                    "alerts_evaluated": alert_status["last_alert_evaluated"],
                    "alerts_qualified": alert_status["last_alert_qualified"],
                    "alert_error": alert_status["last_alert_error"]}
        except ProviderError as exc:
            self._remember_provider_usage(getattr(provider, "usage", {}))
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error, "events": count}


    def _dispatch_alerts(self):
        """Send newly observed EV/arb candidates once; isolate failures per delivery."""
        self._last_alert_check = datetime.now(timezone.utc).isoformat()
        self._last_alert_evaluated = 0
        self._last_alert_qualified = 0
        self._last_alert_sent = 0
        self._last_alert_error = None
        if not config.DISCORD_WEBHOOK_URL:
            return 0

        errors = []
        sent = 0
        for event in self.events():
            try:
                result = analyze_markets(
                    [event], min_ev=config.ALERT_MIN_EV_PERCENT,
                    min_consensus_books=2,
                )
            except Exception as exc:
                errors.append(f"Analysis for {event.get('id', 'unknown event')}: {exc}")
                continue

            candidates = []
            for row in result["opportunities"]:
                identity = ["ev", row["event_id"], row["market"], row["outcome"],
                            row.get("outcome_description"), row.get("point"),
                            row["bookmaker_key"], row["price"]]
                candidates.append(("ev", identity,
                    f'**EV {row["ev_percent"]:.2f}%** · {row["matchup"]} · {row["market"]} '
                    f'{row.get("outcome_description") + " " if row.get("outcome_description") else ""}'
                    f'{row["outcome"]} {row.get("point") if row.get("point") is not None else ""} '
                    f'@ {row["bookmaker"]} ({row["price"]:+d}) · '
                    f'fair {row["fair_probability"]*100:.1f}% · {row["consensus_books"]} other books'))
            for row in result["arbitrage"]:
                if row["arb_percent"] < config.ALERT_MIN_ARB_PERCENT:
                    continue
                identity = ["arb", row["event_id"], row["market"], row.get("line"),
                            sorted((leg["bookmaker_key"], leg["outcome"], leg["price"])
                                   for leg in row["legs"])]
                legs = " / ".join(
                    f'{leg["outcome"]} {leg["price"]:+d} at {leg["bookmaker"]}'
                    for leg in row["legs"]
                )
                candidates.append(("arbitrage", identity,
                    f'**Arbitrage {row["arb_percent"]:.3f}%** · {row["matchup"]} · {row["market"]} · {legs}'))

            self._last_alert_evaluated += len(result["opportunities"]) + len(result["arbitrage"])
            self._last_alert_qualified += len(candidates)
            for alert_type, identity, message in candidates:
                fingerprint = hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode()
                ).hexdigest()
                try:
                    if not self.db.claim_alert(fingerprint, alert_type, message):
                        continue
                    payload = json.dumps({"content": message[:1900]}).encode()
                    request = Request(
                        config.DISCORD_WEBHOOK_URL, data=payload,
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "OpenOddsIntelligence/0.4"},
                        method="POST",
                    )
                    with urlopen(request, timeout=8) as response:
                        if response.status not in (200, 204):
                            raise RuntimeError(
                                f"Discord webhook returned HTTP {response.status}"
                            )
                    sent += 1
                except Exception as exc:
                    self.db.release_alert(fingerprint)
                    errors.append(str(exc)[:300])

        self._last_alert_sent = sent
        self._alerts_evaluated += self._last_alert_evaluated
        self._alerts_qualified += self._last_alert_qualified
        self._last_alert_error = "; ".join(errors)[:1000] if errors else None
        self._alert_error = self._last_alert_error
        return sent

    def events(self, sport=None):
        stored = self.db.latest_events(sport)
        if stored:
            return stored
        if sport:
            return list(self._cache.get(sport, []))
        return [event for group in self._cache.values() for event in group]

    def history(self, event_id, market_key, outcome_name, bookmaker_key=None, point=None,
                limit=100, outcome_description=None):
        return self.db.history(event_id, market_key, outcome_name, bookmaker_key, point,
                               limit, outcome_description)

    def clv_report(self, sport=None, limit=500):
        return self.db.clv_report(sport, limit)

    def movement(self, sport=None, limit=200):
        return self.db.movement(sport, limit)

    def movement_history(self, event_id, market_key, outcome_name,
                         bookmaker_key, limit=500, outcome_description=None):
        return self.db.movement_history(
            event_id, market_key, outcome_name, bookmaker_key, limit, outcome_description
        )


service = OddsService()
