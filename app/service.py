from datetime import datetime, timezone
from . import config
from .database import OddsDatabase
from .providers.the_odds_api import TheOddsAPIProvider, ProviderError


class OddsService:
    def __init__(self, database=None):
        self.db = database or OddsDatabase()
        self._cache = {}
        self._last_error = None
        self._updated_at = None
        self._provider_usage = {}

    @property
    def configured(self):
        return config.ODDS_PROVIDER == "the_odds_api" and bool(config.THE_ODDS_API_KEY)

    def status(self):
        stored = self.db.snapshot_status()
        return {"provider": config.ODDS_PROVIDER, "configured": self.configured,
                "last_updated": self._updated_at or stored.get("last_updated"),
                "last_error": self._last_error, "sports": config.ODDS_SPORTS,
                "live_data_available": bool(stored.get("events")) or bool(self._cache),
                "stored_snapshots": stored["snapshots"], "stored_events": stored["events"],
                "provider_usage": self._provider_usage,
                "refresh_interval_seconds": config.ODDS_REFRESH_INTERVAL_SECONDS}

    def refresh(self, sport=None):
        if not self.configured:
            self._last_error = "No live odds provider configured. Configure a supported provider and API key."
            return {"ok": False, "error": self._last_error, "events": 0}
        provider = TheOddsAPIProvider(config.THE_ODDS_API_KEY, config.ODDS_REGIONS,
                                      config.ODDS_MARKETS, config.ODDS_FORMAT)
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
            return {"ok": True, "events": count, "updated_at": self._updated_at}
        except ProviderError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error, "events": count}

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


service = OddsService()
