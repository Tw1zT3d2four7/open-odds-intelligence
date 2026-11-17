from datetime import datetime, timezone
from . import config
from .providers.the_odds_api import TheOddsAPIProvider, ProviderError

class OddsService:
    def __init__(self):
        self._cache: dict[str, list[dict]] = {}
        self._last_error = None
        self._updated_at = None

    @property
    def configured(self):
        return config.ODDS_PROVIDER == "the_odds_api" and bool(config.THE_ODDS_API_KEY)

    def status(self):
        return {"provider": config.ODDS_PROVIDER, "configured": self.configured,
                "last_updated": self._updated_at, "last_error": self._last_error,
                "sports": config.ODDS_SPORTS, "live_data_available": bool(self._cache)}

    def refresh(self, sport=None):
        if not self.configured:
            self._last_error = "No live odds provider configured. Configure a supported provider and API key."
            return {"ok": False, "error": self._last_error, "events": 0}
        provider = TheOddsAPIProvider(config.THE_ODDS_API_KEY, config.ODDS_REGIONS,
                                      config.ODDS_MARKETS, config.ODDS_FORMAT)
        count = 0
        try:
            for key in ([sport] if sport else config.ODDS_SPORTS):
                self._cache[key] = provider.fetch_odds(key)
                count += len(self._cache[key])
            self._updated_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            return {"ok": True, "events": count, "updated_at": self._updated_at}
        except ProviderError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error, "events": count}

    def events(self, sport=None):
        if sport:
            return list(self._cache.get(sport, []))
        return [event for group in self._cache.values() for event in group]

service = OddsService()
