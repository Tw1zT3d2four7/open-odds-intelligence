from datetime import datetime, timezone
import hashlib
import threading
import json
import time
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from . import config
from .database import OddsDatabase
from .providers.the_odds_api import TheOddsAPIProvider, ProviderError
from .providers.therundown import TheRundownProvider
from .providers.key_pool import NoAvailableKeys, ProviderKeyPool, parse_api_keys
from .providers.merge import merge_provider_events
from .providers.sports_game_odds import SportsGameOddsProvider
from .markets import analyze_markets


def _discord_event_time(value):
    """Return a Discord-rendered event time without trusting malformed provider data."""
    if not isinstance(value, str) or not value.strip():
        return "Not supplied"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return f"<t:{int(parsed.timestamp())}:F> (<t:{int(parsed.timestamp())}:R>)"
    except (TypeError, ValueError, OverflowError, OSError):
        return "Time unavailable"


def _discord_field(name, value, inline=False):
    value = str(value) if value is not None else "—"
    return {"name": str(name)[:256], "value": value[:1024] or "—", "inline": inline}


def _discord_ev_embed(row):
    """Build a compact, scannable embed for one positive-EV market quote."""
    selection = row.get("outcome_description") or row.get("outcome") or "Selection"
    point = row.get("point")
    if point is not None:
        selection = f"{selection} ({point:+g})" if isinstance(point, (int, float)) else f"{selection} ({point})"
    fair = row.get("fair_probability")
    fair_text = f"{float(fair) * 100:.1f}%" if isinstance(fair, (int, float)) else "Unavailable"
    price = row.get("price")
    price_text = f"{price:+d}" if isinstance(price, int) and not isinstance(price, bool) else str(price or "Unavailable")
    return {
        "title": f"VALUE ALERT  •  +{float(row.get('ev_percent', 0)):.2f}% EV",
        "description": f"**{row.get('matchup') or 'Matchup unavailable'}**",
        "color": 0x2ECC71,
        "fields": [
            _discord_field("Sport / Market", f"{row.get('sport') or row.get('sport_key') or 'Sport'} · {row.get('market') or 'Market'}"),
            _discord_field("Selection", selection),
            _discord_field("Sportsbook", row.get("bookmaker") or "Unknown book", True),
            _discord_field("American odds", price_text, True),
            _discord_field("Fair probability", fair_text, True),
            _discord_field("Expected value", f"+{float(row.get('ev_percent', 0)):.2f}%", True),
            _discord_field("Consensus", f"{row.get('consensus_books', 0)} comparison books", True),
            _discord_field("Game time", _discord_event_time(row.get("commence_time"))),
        ],
        "footer": {"text": "Open Odds Intelligence · Market estimate, not a guaranteed outcome"},
    }


def _discord_arbitrage_embed(row):
    """Build a leg-by-leg arbitrage card with book, side, price and stake split."""
    fields = [
        _discord_field("Sport / Market", f"{row.get('sport') or 'Sport'} · {row.get('market') or 'Market'}"),
        _discord_field("Estimated arbitrage margin", f"{float(row.get('arb_percent', 0)):.3f}%", True),
        _discord_field("Game time", _discord_event_time(row.get("commence_time"))),
    ]
    for index, leg in enumerate(row.get("legs") or [], start=1):
        price = leg.get("price")
        price_text = f"{price:+d}" if isinstance(price, int) and not isinstance(price, bool) else str(price or "Unavailable")
        selection = leg.get("outcome") or "Selection"
        point = leg.get("point")
        if point is not None:
            selection = f"{selection} ({point:+g})" if isinstance(point, (int, float)) else f"{selection} ({point})"
        fields.append(_discord_field(
            f"LEG {index}  ·  {leg.get('bookmaker') or 'Unknown book'}",
            f"**{selection}**\nOdds: **{price_text}**  ·  Suggested stake split: **{leg.get('stake_percent', 0):.2f}%**",
        ))
    return {
        "title": f"ARBITRAGE ALERT  •  {float(row.get('arb_percent', 0)):.3f}%",
        "description": f"**{row.get('matchup') or 'Matchup unavailable'}**",
        "color": 0x3498DB,
        "fields": fields,
        "footer": {"text": "Open Odds Intelligence · Verify both prices before acting; markets can move"},
    }


class OddsService:
    def __init__(self, database=None):
        self.db = database or OddsDatabase()
        self._refresh_lock = threading.Lock()
        self._alert_lock = threading.Lock()
        self._cache = {}
        self._last_error = None
        self._updated_at = None
        self._provider_usage = {}
        self._provider_key_pools = {
            "the_odds_api": ProviderKeyPool(
                "the_odds_api", parse_api_keys(config.THE_ODDS_API_KEYS, config.THE_ODDS_API_KEY)
            ),
            "sports_game_odds": ProviderKeyPool(
                "sports_game_odds", parse_api_keys(
                    config.SPORTSGAMEODDS_API_KEYS, config.SPORTSGAMEODDS_API_KEY
                )
            ),
            "therundown": ProviderKeyPool(
                "therundown", parse_api_keys(config.THERUNDOWN_API_KEYS, config.THERUNDOWN_API_KEY)
            ),
        }
        self._simulation_results_status = {
            "enabled": bool(config.SIMULATION_RESULTS_SYNC_ENABLED and config.ODDS_PROVIDER == "therundown" and config.THERUNDOWN_API_KEY),
            "provider": config.ODDS_PROVIDER,
            "last_sync": self.db.get_state("simulation_results_last_sync"),
            "last_error": self.db.get_state("simulation_results_last_error"),
            "settlements_stored": len(self.db.simulation_settlements()),
            "inserted_last_run": 0, "rejected_last_run": 0,
            "reason": "Optional score synchronization is disabled." if not config.SIMULATION_RESULTS_SYNC_ENABLED else None,
        }
        saved_usage_provider = self.db.get_state("provider_usage_provider")
        saved_usage_fingerprint = self.db.get_state("provider_usage_key_fingerprint")
        configured_fingerprints = {
            hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            for key in parse_api_keys(
                config.THERUNDOWN_API_KEYS, config.THERUNDOWN_API_KEY
            ) if config.ODDS_PROVIDER == "therundown"
        } | {
            hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            for key in parse_api_keys(
                config.THE_ODDS_API_KEYS, config.THE_ODDS_API_KEY
            ) if config.ODDS_PROVIDER == "the_odds_api"
        } | {
            hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            for key in parse_api_keys(
                config.SPORTSGAMEODDS_API_KEYS, config.SPORTSGAMEODDS_API_KEY
            ) if config.ODDS_PROVIDER == "sports_game_odds"
        }
        if (saved_usage_provider == config.ODDS_PROVIDER and
            saved_usage_fingerprint in configured_fingerprints):
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
            if "the_odds_api" not in self.active_provider_names():
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

    def _current_provider_key_fingerprint(self, provider_name=None):
        provider_name = provider_name or config.ODDS_PROVIDER
        if provider_name == "therundown":
            keys = parse_api_keys(config.THERUNDOWN_API_KEYS, config.THERUNDOWN_API_KEY)
        elif provider_name == "the_odds_api":
            keys = parse_api_keys(config.THE_ODDS_API_KEYS, config.THE_ODDS_API_KEY)
        elif provider_name == "sports_game_odds":
            keys = parse_api_keys(config.SPORTSGAMEODDS_API_KEYS, config.SPORTSGAMEODDS_API_KEY)
        else:
            keys = []
        material = "\\0".join(keys)
        return hashlib.sha256(material.encode("utf-8")).hexdigest() if material else ""

    def _remember_provider_usage(self, usage, provider_name=None, key_fingerprint=None):
        if not usage:
            return
        provider_name = provider_name or config.ODDS_PROVIDER
        self._provider_usage = dict(usage)
        self.db.set_state("provider_usage", json.dumps(self._provider_usage))
        self.db.set_state("provider_usage_provider", provider_name)
        self.db.set_state(
            "provider_usage_key_fingerprint",
            key_fingerprint or self._current_provider_key_fingerprint(provider_name),
        )

    def _new_provider(self, provider_name, api_key):
        if provider_name == "therundown":
            return TheRundownProvider(
                api_key, utc_offset_minutes=config.RUNDOWN_UTC_OFFSET_MINUTES,
            )
        if provider_name == "the_odds_api":
            return TheOddsAPIProvider(
                api_key, config.ODDS_REGIONS, config.ODDS_MARKETS,
                config.ODDS_FORMAT, config.ODDS_BOOKMAKERS,
            )
        if provider_name == "sports_game_odds":
            return SportsGameOddsProvider(api_key)
        raise ProviderError(f"Unsupported odds provider: {provider_name}")

    def _fetch_provider_odds_with_rotation(self, provider_name, sport):
        pool = self._provider_key_pools.get(provider_name)
        if pool is None or not pool.configured:
            raise ProviderError(f"No API keys configured for provider: {provider_name}")
        last_error = None
        for _ in range(pool.status()["configured_keys"]):
            try:
                lease = pool.acquire()
            except NoAvailableKeys as exc:
                raise ProviderError(str(exc)) from exc
            provider = self._new_provider(provider_name, lease.key)
            try:
                events = self._fetch_odds_with_retry(provider, sport)
            except ProviderError as exc:
                # Preserve quota headers from failed keys too; they are often
                # the only authoritative signal that a free allowance is spent.
                usage = getattr(provider, "usage", {})
                pool.record_usage(lease, usage)
                self._remember_provider_usage(usage, provider_name, lease.fingerprint)
                disposition = pool.mark_failure(
                    lease,
                    status_code=getattr(exc, "status_code", None),
                    message=str(exc),
                    retry_after_seconds=getattr(exc, "retry_after", None),
                )
                last_error = exc
                if disposition in ("disabled", "cooldown"):
                    continue
                raise
            pool.record_usage(lease, getattr(provider, "usage", {}))
            pool.mark_success(lease)
            return events, provider, lease
        if last_error is not None:
            raise last_error
        raise ProviderError(f"No usable API keys remain for provider: {provider_name}")

    def active_provider_names(self):
        if getattr(config, "ODDS_PROVIDERS_EXPLICIT", False):
            selected = config.ODDS_PROVIDERS
        else:
            selected = [config.ODDS_PROVIDER] if config.ODDS_PROVIDER else []
        return list(dict.fromkeys(
            name for name in selected if name in self._provider_key_pools
        ))

    def auto_refresh_provider_names(self):
        # SportsGameOdds' permanently free Amateur plan is object-metered.
        # Keep it manual-only until its object budget can be scheduled safely.
        return [
            name for name in self.active_provider_names()
            if name != "sports_game_odds" and self._provider_key_pools[name].configured
        ]

    @property
    def configured(self):
        return any(
            self._provider_key_pools[name].configured
            for name in self.active_provider_names()
        )

    def status(self):
        stored = self.db.snapshot_status()
        now = datetime.now(timezone.utc)
        latest_by_sport = {item["sport"]: item for item in stored.get("sports", [])}
        sport_freshness = []
        for sport in config.ODDS_SPORTS:
            snapshot = latest_by_sport.get(sport)
            if snapshot is None:
                sport_freshness.append({
                    "sport": sport, "last_updated": None, "age_seconds": None,
                    "event_count": 0, "freshness": "unavailable",
                })
                continue
            try:
                fetched = datetime.fromisoformat(str(snapshot["fetched_at"]).replace("Z", "+00:00"))
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=timezone.utc)
                age = max(0, int((now - fetched.astimezone(timezone.utc)).total_seconds()))
                freshness = "stale" if age > config.ODDS_DATA_STALE_AFTER_SECONDS else "current"
            except (TypeError, ValueError):
                age, freshness = None, "unknown"
            sport_freshness.append({
                "sport": sport, "last_updated": snapshot["fetched_at"],
                "age_seconds": age, "event_count": snapshot["event_count"],
                "freshness": freshness,
            })
        states = {item["freshness"] for item in sport_freshness}
        if not states:
            data_freshness = "unavailable"
        elif "stale" in states:
            data_freshness = "stale"
        elif "unavailable" in states or "unknown" in states:
            data_freshness = "partial"
        else:
            data_freshness = "current"
        alert_status = self.db.alert_status()
        return {"provider": config.ODDS_PROVIDER,
                "providers": self.active_provider_names(),
                "auto_refresh_providers": self.auto_refresh_provider_names(),
                "configured": self.configured,
                "last_updated": self._updated_at or stored.get("last_updated"),
                "last_error": self._last_error, "sports": config.ODDS_SPORTS,
                "odds_regions": config.ODDS_REGIONS,
                "odds_bookmakers": config.ODDS_BOOKMAKERS,
                "live_data_available": bool(stored.get("events")) or bool(self._cache),
                "data_freshness": data_freshness,
                "data_stale_after_seconds": config.ODDS_DATA_STALE_AFTER_SECONDS,
                "sport_freshness": sport_freshness,
                "stored_snapshots": stored["snapshots"], "stored_events": stored["events"],
                "provider_usage": self._provider_usage,
                "provider_keys": {
                    name: pool.status() for name, pool in self._provider_key_pools.items()
                },
                "simulation_results": self._simulation_results_status,
                "player_props": self._player_props_status,
                "refresh_interval_seconds": config.RUNDOWN_REFRESH_INTERVAL_SECONDS if config.ODDS_PROVIDER == "therundown" else config.ODDS_REFRESH_INTERVAL_SECONDS,
                "alert_refresh_interval_seconds": config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS if config.DISCORD_WEBHOOK_URL else 0,
                "alerts_evaluated": self._alerts_evaluated,
                "alerts_qualified": self._alerts_qualified,
                "last_alert_check": self._last_alert_check,
                "last_alert_evaluated": self._last_alert_evaluated,
                "last_alert_qualified": self._last_alert_qualified,
                "last_alert_sent": self._last_alert_sent,
                "last_alert_error": self._last_alert_error,
                "alerts_enabled": bool(config.DISCORD_WEBHOOK_URL),
                "alerts_sent": alert_status["sent"],
                "last_alert_at": alert_status["last_sent"],
                "alert_error": self._alert_error}

    def refresh(self, sport=None, providers=None):
        # Prevent manual clicks and the background scheduler from overlapping
        # provider requests and consuming duplicate quota.
        if not self._refresh_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True,
                    "error": "A refresh is already running; this request was skipped to protect provider quota.",
                    "events": 0}
        try:
            return self._refresh_locked(sport, providers)
        finally:
            self._refresh_lock.release()

    @staticmethod
    def _retry_delay(error, attempt):
        """Return a bounded delay, honoring Retry-After without trusting unbounded input."""
        base = min(
            config.PROVIDER_RETRY_BASE_DELAY_SECONDS * (2 ** attempt),
            config.PROVIDER_RETRY_MAX_DELAY_SECONDS,
        )
        raw = getattr(error, "retry_after", None)
        if raw:
            try:
                requested = float(raw)
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(raw))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    requested = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    requested = base
            # Never retry earlier than the provider requested. If its delay
            # exceeds our bounded in-refresh wait budget, defer to a later
            # operator/scheduler refresh instead of violating Retry-After.
            if requested > config.PROVIDER_RETRY_MAX_DELAY_SECONDS:
                return None
            return max(0.0, requested)
        return base

    def _fetch_odds_with_retry(self, provider, sport):
        """Retry only transient failures; never repeat quota or credential failures."""
        attempts = config.PROVIDER_RETRY_ATTEMPTS
        for attempt in range(attempts + 1):
            try:
                return provider.fetch_odds(sport)
            except ProviderError as exc:
                # A 429 means the provider has explicitly denied quota/rate
                # capacity. Do not issue another request during this refresh.
                if (attempt >= attempts or not getattr(exc, "retryable", False)
                        or getattr(exc, "status_code", None) == 429):
                    raise
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                time.sleep(delay)

    def _refresh_locked(self, sport=None, providers=None):
        provider_names = self.active_provider_names()
        if providers is not None:
            requested_providers = set(providers)
            provider_names = [name for name in provider_names if name in requested_providers]
        configured_names = [
            name for name in provider_names if self._provider_key_pools[name].configured
        ]
        if not configured_names:
            self._last_error = "No configured API keys for the selected odds providers."
            return {"ok": False, "error": self._last_error, "events": 0}
        sports = [sport] if sport else config.ODDS_SPORTS
        count = 0
        successful_sports = 0
        provider_errors = []
        provider_results_summary = {}
        for sport_key in sports:
            source_results = []
            successful_providers = []
            for provider_name in provider_names:
                pool = self._provider_key_pools.get(provider_name)
                if pool is None or not pool.configured:
                    provider_errors.append(
                        f"{sport_key} / {provider_name}: no API key configured"
                    )
                    continue
                try:
                    events, provider, lease = self._fetch_provider_odds_with_rotation(
                        provider_name, sport_key
                    )
                    self._remember_provider_usage(
                        provider.usage, provider_name, lease.fingerprint
                    )
                    if (config.PLAYER_PROPS_ENABLED
                            and sport_key == config.PLAYER_PROPS_SPORT
                            and provider_name == "the_odds_api"):
                        now = datetime.now(timezone.utc)
                        due = (
                            self._player_props_last_attempt is None
                            or (now - self._player_props_last_attempt).total_seconds()
                            >= config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS
                        )
                        if due:
                            self._player_props_last_attempt = now
                            self.db.set_state("player_props_last_attempt", now.isoformat())
                            self._player_props_status = provider.fetch_player_props(
                                sport_key, events, config.PLAYER_PROPS_MARKETS,
                                config.PLAYER_PROPS_MAX_EVENTS,
                            )
                            self._player_props_status["last_updated"] = now.isoformat()
                            self._player_props_status["refresh_interval_seconds"] = (
                                config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS
                            )
                            self._remember_provider_usage(
                                provider.usage, provider_name, lease.fingerprint
                            )
                        else:
                            remaining = max(
                                0, int(
                                    config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS
                                    - (now - self._player_props_last_attempt).total_seconds()
                                )
                            )
                            self._player_props_status = {
                                "enabled": True, "sport": sport_key,
                                "events_requested": 0, "events_with_props": 0,
                                "reason": "Waiting for the quota-safe player-props refresh interval.",
                                "next_refresh_seconds": remaining,
                                "refresh_interval_seconds": config.PLAYER_PROPS_REFRESH_INTERVAL_SECONDS,
                            }
                    source_results.append((provider_name, events))
                    successful_providers.append(provider_name)
                except ProviderError as exc:
                    provider_errors.append(f"{sport_key} / {provider_name}: {exc}")
                    continue

            if not source_results:
                continue

            events = merge_provider_events(source_results)
            fetched_at = datetime.now(timezone.utc).isoformat()
            snapshot_provider = "+".join(successful_providers)
            self.db.save_snapshot(sport_key, events, snapshot_provider, fetched_at)
            self._cache[sport_key] = events
            clv_candidates = analyze_markets(
                events, min_ev=config.CLV_MIN_EV_PERCENT,
                min_consensus_books=2, best_only=False,
            )["opportunities"]
            self.db.record_clv_candidates(clv_candidates, fetched_at)
            count += len(events)
            successful_sports += 1
            provider_results_summary[sport_key] = {
                "providers_succeeded": successful_providers,
                "events": len(events),
            }

        self._updated_at = datetime.now(timezone.utc).isoformat()
        complete = successful_sports == len(sports)
        self._last_error = "; ".join(provider_errors)[:2000] if provider_errors else None
        alerts_sent = self._dispatch_alerts() if successful_sports else 0
        alert_status = self.status()
        return {
            "ok": complete and successful_sports > 0,
            "partial": bool(provider_errors) or not complete,
            "events": count,
            "sports_refreshed": successful_sports,
            "sports_requested": len(sports),
            "provider_results": provider_results_summary,
            "provider_errors": provider_errors,
            "error": "; ".join(provider_errors)[:2000] if provider_errors else None,
            "updated_at": self._updated_at,
            "alerts_sent": alerts_sent,
            "alerts_enabled": alert_status["alerts_enabled"],
            "alerts_evaluated": alert_status["last_alert_evaluated"],
            "alerts_qualified": alert_status["last_alert_qualified"],
            "alert_error": alert_status["last_alert_error"],
        }


    def sync_simulation_results(self):
        """Collect final scores only for games already present in local odds history."""
        enabled = bool(config.SIMULATION_RESULTS_SYNC_ENABLED and
                       config.ODDS_PROVIDER == "therundown" and config.THERUNDOWN_API_KEY)
        if not enabled:
            self._simulation_results_status.update({
                "enabled": False,
                "reason": "Score sync is disabled or TheRundown is not the active configured odds provider.",
            })
            return {"ok": False, "skipped": True, "reason": "disabled"}
        if not self._refresh_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True,
                    "reason": "Odds refresh is active; score sync deferred to protect the provider rate limit."}
        provider = TheRundownProvider(
            config.THERUNDOWN_API_KEY, utc_offset_minutes=config.RUNDOWN_UTC_OFFSET_MINUTES
        )
        now = datetime.now(timezone.utc)
        inserted = corrected = duplicates = rejected = groups_checked = events_returned = 0
        errors = []
        try:
            groups = self.db.pending_simulation_result_dates(
                [sport for sport in config.ODDS_SPORTS if sport in provider.SPORTS],
                lookback_days=config.SIMULATION_RESULTS_LOOKBACK_DAYS, now=now,
                offset_minutes=config.RUNDOWN_UTC_OFFSET_MINUTES,
                recheck_days=config.SIMULATION_RESULTS_RECHECK_DAYS,
            )
            due_groups = []
            for group in groups:
                state_key = "simulation_results_attempt:{}:{}".format(group["sport"], group["event_date"])
                previous = self.db.get_state(state_key)
                try:
                    previous_at = datetime.fromisoformat(previous.replace("Z", "+00:00")) if previous else None
                    if previous_at is not None and previous_at.tzinfo is None:
                        previous_at = previous_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    previous_at = None
                if previous_at is None or (now - previous_at.astimezone(timezone.utc)).total_seconds() >= config.SIMULATION_RESULTS_RETRY_SECONDS:
                    due_groups.append((group, state_key))
            due_groups = due_groups[:config.SIMULATION_RESULTS_MAX_GROUPS_PER_RUN]
            for group, state_key in due_groups:
                self.db.set_state(state_key, datetime.now(timezone.utc).isoformat())
                try:
                    results = provider.fetch_scores(group["sport"], [group["event_date"]])
                    events_returned += len(results)
                    saved = self.db.save_simulation_settlements(results)
                    inserted += saved["inserted"]
                    corrected += saved.get("corrected", 0)
                    duplicates += saved["duplicates"]
                    rejected += len(saved["rejected"])
                    groups_checked += 1
                except ProviderError as exc:
                    errors.append(str(group["sport"]) + " " + str(group["event_date"]) + ": " + str(exc))
                    continue
            self._remember_provider_usage(provider.usage)
            last_sync = datetime.now(timezone.utc).isoformat()
            last_error = "; ".join(errors)[:1000] if errors else None
            self.db.set_state("simulation_results_last_sync", last_sync)
            self.db.set_state("simulation_results_last_error", last_error or "")
            self._simulation_results_status = {
                "enabled": True, "provider": provider.name, "last_sync": last_sync,
                "last_error": last_error, "groups_pending": len(groups),
                "groups_checked": groups_checked, "events_returned": events_returned,
                "inserted_last_run": inserted, "corrected_last_run": corrected,
                "duplicates_last_run": duplicates,
                "rejected_last_run": rejected,
                "settlements_stored": len(self.db.simulation_settlements()),
                "lookback_days": config.SIMULATION_RESULTS_LOOKBACK_DAYS,
                "recheck_days": config.SIMULATION_RESULTS_RECHECK_DAYS,
                "refresh_interval_seconds": config.SIMULATION_RESULTS_SYNC_INTERVAL_SECONDS,
                "retry_interval_seconds": config.SIMULATION_RESULTS_RETRY_SECONDS,
                "max_groups_per_run": config.SIMULATION_RESULTS_MAX_GROUPS_PER_RUN,
                "data_policy": "Score-only affiliate_ids=0 requests; only final scores for event IDs already in local odds snapshots are stored.",
            }
            return {"ok": not bool(errors), "groups_checked": groups_checked,
                    "events_returned": events_returned, "inserted": inserted,
                    "corrected": corrected, "duplicates": duplicates,
                    "rejected": rejected, "error": last_error}
        except ProviderError as exc:
            self._remember_provider_usage(provider.usage)
            last_error = str(exc)
            self.db.set_state("simulation_results_last_error", last_error)
            self._simulation_results_status.update({"enabled": True, "last_error": last_error})
            return {"ok": False, "error": last_error, "groups_checked": groups_checked}
        finally:
            self._refresh_lock.release()

    def evaluate_alerts(self):
        """Evaluate current stored odds snapshots without refreshing a provider."""
        if not config.DISCORD_WEBHOOK_URL:
            return 0
        return self._dispatch_alerts()

    def _dispatch_alerts(self):
        """Serialize alert checks so scheduled and post-refresh checks cannot overlap."""
        if not self._alert_lock.acquire(blocking=False):
            return 0
        try:
            return self._dispatch_alerts_locked()
        finally:
            self._alert_lock.release()

    def _dispatch_alerts_locked(self):
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
                summary = (
                    f'VALUE +{row["ev_percent"]:.2f}% · {row["matchup"]} · '
                    f'{row["market"]} · {row["outcome"]} · {row["bookmaker"]} '
                    f'({row["price"]:+d})'
                )
                candidates.append(("ev", identity, summary, _discord_ev_embed(row)))
            for row in result["arbitrage"]:
                if row["arb_percent"] < config.ALERT_MIN_ARB_PERCENT:
                    continue
                identity = ["arb", row["event_id"], row["market"], row.get("line"),
                            sorted((leg["bookmaker_key"], leg["outcome"], leg["price"])
                                   for leg in row["legs"])]
                summary = (
                    f'ARBITRAGE {row["arb_percent"]:.3f}% · {row["matchup"]} · '
                    f'{row["market"]}'
                )
                candidates.append(("arbitrage", identity, summary, _discord_arbitrage_embed(row)))

            self._last_alert_evaluated += len(result["opportunities"]) + len(result["arbitrage"])
            self._last_alert_qualified += len(candidates)
            for alert_type, identity, message, embed in candidates:
                fingerprint = hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode()
                ).hexdigest()
                try:
                    if not self.db.claim_alert(fingerprint, alert_type, message):
                        continue
                    payload = json.dumps({"embeds": [embed], "allowed_mentions": {"parse": []}}).encode()
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
