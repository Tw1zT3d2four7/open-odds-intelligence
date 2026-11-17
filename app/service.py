from datetime import datetime, timezone
import hashlib
import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from . import config
from .database import OddsDatabase
from .providers.the_odds_api import TheOddsAPIProvider, ProviderError
from .markets import analyze_markets


class OddsService:
    def __init__(self, database=None):
        self.db = database or OddsDatabase()
        self._cache = {}
        self._last_error = None
        self._updated_at = None
        self._provider_usage = {}
        self._alert_error = None
        self._alerts_evaluated = 0
        self._alerts_qualified = 0
        self._last_alert_check = None
        self._last_alert_evaluated = 0
        self._last_alert_qualified = 0
        self._last_alert_sent = 0
        self._last_alert_error = None

    @property
    def configured(self):
        return config.ODDS_PROVIDER == "the_odds_api" and bool(config.THE_ODDS_API_KEY)

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
                "refresh_interval_seconds": config.ODDS_REFRESH_INTERVAL_SECONDS or (config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS if config.DISCORD_WEBHOOK_URL else 0),
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
        if not self.configured:
            self._last_error = "No live odds provider configured. Configure a supported provider and API key."
            return {"ok": False, "error": self._last_error, "events": 0}
        provider = TheOddsAPIProvider(config.THE_ODDS_API_KEY, config.ODDS_REGIONS,
                                      config.ODDS_MARKETS, config.ODDS_FORMAT,
                                      config.ODDS_BOOKMAKERS)
        count = 0
        try:
            for key in ([sport] if sport else config.ODDS_SPORTS):
                events = provider.fetch_odds(key)
                self._provider_usage = dict(provider.usage)
                fetched_at = datetime.now(timezone.utc).isoformat()
                self.db.save_snapshot(key, events, provider.name, fetched_at)
                self._cache[key] = events
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
                            row.get("point"), row["bookmaker_key"], row["price"]]
                candidates.append(("ev", identity,
                    f'**EV {row["ev_percent"]:.2f}%** · {row["matchup"]} · {row["market"]} '
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

    def history(self, event_id, market_key, outcome_name, bookmaker_key=None, point=None, limit=100):
        return self.db.history(event_id, market_key, outcome_name, bookmaker_key, point, limit)

    def movement(self, sport=None, limit=200):
        return self.db.movement(sport, limit)

    def movement_history(self, event_id, market_key, outcome_name,
                         bookmaker_key, limit=500):
        return self.db.movement_history(
            event_id, market_key, outcome_name, bookmaker_key, limit
        )


service = OddsService()
