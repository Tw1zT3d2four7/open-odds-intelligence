from pathlib import Path
import json
from datetime import datetime, timezone
import base64
import binascii
import logging
import asyncio
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from . import config
from .service import service

# Reuse the monitor owned by the service so OddsLink odds refreshes and the
# market-limits dashboard share one database-backed state object.
limits_monitor = service.limits_monitor
from .markets import market_board, analyze_markets
from .simulation import load_fitted_models, run_game_simulation, TRIALS as SIMULATION_TRIALS
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

async def _limits_refresh_loop():
    while True:
        delay = config.LIMITS_REFRESH_INTERVAL_SECONDS
        try:
            settings = await asyncio.to_thread(_admin_settings)
            if (settings["automatic_refresh_enabled"]
                    and settings["provider_auto_refresh_enabled"].get("oddslink", True)):
                await asyncio.to_thread(limits_monitor.refresh)
                status = await asyncio.to_thread(limits_monitor.status)
                delay = max(1, int(status.get("next_refresh_seconds") or delay))
        except Exception as exc:
            limits_monitor.last_error = f"Unexpected scheduled market-limits refresh failure: {exc}"
            logger.exception("Scheduled market-limits refresh failed")
        await asyncio.sleep(delay)




async def _simulation_results_loop():
    while True:
        try:
            settings = await asyncio.to_thread(_admin_settings)
            if (settings["automatic_refresh_enabled"]
                    and settings["provider_auto_refresh_enabled"].get("therundown", True)):
                await asyncio.to_thread(service.sync_simulation_results)
        except Exception as exc:
            service._simulation_results_status.update({
                "last_error": "Unexpected score synchronization failure: " + str(exc),
            })
            logger.exception("Simulator final-score synchronization failed")
        await asyncio.sleep(config.SIMULATION_RESULTS_SYNC_INTERVAL_SECONDS)

def _odds_refresh_interval_seconds():
    """Return the shortest enabled provider poll interval; alerts never poll odds."""
    intervals = list(service.auto_refresh_schedule().values())
    return min(intervals) if intervals else 0


async def _automatic_refresh_loop():
    interval = _odds_refresh_interval_seconds()
    while True:
        # The admin kill switch pauses provider polling without changing the
        # configured per-provider free-tier schedules.
        try:
            settings = await asyncio.to_thread(_admin_settings)
            if settings["automatic_refresh_enabled"]:
                due_providers = await asyncio.to_thread(service.providers_due_for_auto_refresh)
                due_providers = [name for name in due_providers if settings["provider_auto_refresh_enabled"].get(name, True)]
                if due_providers:
                    result = await asyncio.to_thread(service.refresh, providers=due_providers)
                    succeeded = {
                        name
                        for sport_result in (result.get("provider_results") or {}).values()
                        for name in sport_result.get("providers_succeeded", [])
                    }
                    if succeeded:
                        await asyncio.to_thread(service.mark_auto_refresh, succeeded)
        except Exception as exc:
            service._last_error = f"Unexpected scheduled odds refresh failure: {exc}"
            logger.exception("Scheduled odds refresh failed")
        await asyncio.sleep(interval)


async def _discord_alert_loop():
    """Re-evaluate locally stored quotes without making provider requests."""
    while True:
        await asyncio.sleep(config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(service.evaluate_alerts)
        except Exception as exc:
            service._last_alert_error = f"Unexpected scheduled Discord alert check failure: {exc}"
            service._alert_error = service._last_alert_error
            logger.exception("Scheduled Discord alert evaluation failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    limits_task = None
    results_task = None
    alert_task = None
    oddslink_selected = (
        "oddslink" in service.active_provider_names()
        and service._provider_key_pools["oddslink"].configured
    )
    if (config.ODDSLINK_API_KEY or config.ODDSLINK_API_KEYS) and not oddslink_selected:
        limits_task = asyncio.create_task(_limits_refresh_loop())
    if service.configured and _odds_refresh_interval_seconds() > 0:
        task = asyncio.create_task(_automatic_refresh_loop())
    if service.configured and config.DISCORD_WEBHOOK_URL:
        alert_task = asyncio.create_task(_discord_alert_loop())
    if (config.SIMULATION_RESULTS_SYNC_ENABLED
            and "therundown" in service.active_provider_names()
            and (config.THERUNDOWN_API_KEY or config.THERUNDOWN_API_KEYS)):
        results_task = asyncio.create_task(_simulation_results_loop())
    try:
        yield
    finally:
        if limits_task is not None:
            limits_task.cancel()
            try:
                await limits_task
            except asyncio.CancelledError:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if alert_task is not None:
            alert_task.cancel()
            try:
                await alert_task
            except asyncio.CancelledError:
                pass
        if results_task is not None:
            results_task.cancel()
            try:
                await results_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title=config.APP_NAME, version="0.4.0",
              description="Self-hosted open-source odds comparison and market intelligence.",
              lifespan=lifespan)
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"
ADMIN_WEB = Path(__file__).resolve().parent.parent / "web" / "admin.html"

ADMIN_DEFAULTS = {
    "automatic_refresh_enabled": True,
    "manual_refresh_enabled": True,
    "manual_refresh_cooldown_seconds": 900,
    "manual_refresh_daily_limit": 3,
    "provider_auto_refresh_enabled": {
        "therundown": True, "the_odds_api": True, "oddize": True, "oddslink": True,
    },
}


def _admin_settings():
    try:
        saved = service.db.get_state("admin_control_settings")
        values = json.loads(saved) if saved else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        values = {}
    merged = {**ADMIN_DEFAULTS, **{key: value for key, value in values.items() if key in ADMIN_DEFAULTS}}
    provider_switches = values.get("provider_auto_refresh_enabled", {})
    if not isinstance(provider_switches, dict):
        provider_switches = {}
    merged["provider_auto_refresh_enabled"] = {
        **ADMIN_DEFAULTS["provider_auto_refresh_enabled"],
        **{name: enabled for name, enabled in provider_switches.items()
           if name in ADMIN_DEFAULTS["provider_auto_refresh_enabled"] and isinstance(enabled, bool)},
    }
    return merged


def _manual_refresh_usage():
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        used = int(service.db.get_state(f"admin_manual_refresh_day:{today}") or 0)
    except (TypeError, ValueError):
        used = 0
    settings = _admin_settings()
    return {
        "used_today": used,
        "remaining_today": max(0, settings["manual_refresh_daily_limit"] - used),
        "daily_limit": settings["manual_refresh_daily_limit"],
        "last_refresh_at": service.db.get_state("admin_manual_refresh_last_at"),
    }


def _reserve_manual_refresh_slot():
    settings = _admin_settings()
    if not settings["manual_refresh_enabled"]:
        raise HTTPException(status_code=403, detail="Manual provider refreshes are disabled by the administrator.")
    result = service.db.consume_admin_refresh_slot(
        settings["manual_refresh_cooldown_seconds"],
        settings["manual_refresh_daily_limit"],
    )
    if not result["allowed"]:
        if result["reason"] == "cooldown":
            raise HTTPException(
                status_code=429,
                detail=f"Manual refresh cooldown is active. Try again in {result['retry_after_seconds']} seconds.",
                headers={"Retry-After": str(result["retry_after_seconds"])},
            )
        raise HTTPException(
            status_code=429,
            detail="The administrator-configured daily manual refresh limit has been reached.",
        )
    return result


def _basic_credentials(request: Request):
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return "", ""
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
        return (username, password) if separator else ("", "")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "", ""


def _credentials_match(request: Request, username: str, password: str) -> bool:
    supplied_user, supplied_password = _basic_credentials(request)
    return (
        secrets.compare_digest(supplied_user.encode(), username.encode())
        and secrets.compare_digest(supplied_password.encode(), password.encode())
    )


def _admin_path(path: str) -> bool:
    return (
        path == "/admin"
        or path.startswith("/api/admin/")
        or path in {
            "/api/refresh", "/api/limits/refresh",
            "/api/simulation/games/refresh",
        }
    )


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    """Enforce separate administrator credentials for private controls.

    The public health probe is deliberately reduced to non-sensitive status.
    Admin routes use ADMIN_USERNAME/ADMIN_PASSWORD and never fall back to
    subscriber-wide AUTH credentials.
    """
    path = request.url.path
    if path in {"/api/health", "/assets/odds-intelligence.svg"}:
        return await call_next(request)

    if _admin_path(path):
        username, password = config.ADMIN_USERNAME, config.ADMIN_PASSWORD
        if not username or not password:
            return JSONResponse(
                status_code=503,
                content={"detail": "Admin access is not configured. Set both ADMIN_USERNAME and ADMIN_PASSWORD in the private .env file."},
            )
        if (config.AUTH_USERNAME and config.AUTH_PASSWORD
                and secrets.compare_digest(username.encode(), config.AUTH_USERNAME.encode())
                and secrets.compare_digest(password.encode(), config.AUTH_PASSWORD.encode())):
            return JSONResponse(
                status_code=503,
                content={"detail": "Administrator credentials must be different from subscriber-wide AUTH credentials."},
            )
        if not _credentials_match(request, username, password):
            return JSONResponse(
                status_code=401,
                content={"detail": "Administrator authentication required."},
                headers={"WWW-Authenticate": 'Basic realm="Open Odds Intelligence Admin", charset="UTF-8"'},
            )
        return await call_next(request)

    username, password = config.AUTH_USERNAME, config.AUTH_PASSWORD
    if bool(username) != bool(password):
        return JSONResponse(
            status_code=503,
            content={"detail": "Authentication is misconfigured; set both AUTH_USERNAME and AUTH_PASSWORD."},
        )
    if username and not _credentials_match(request, username, password):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="Open Odds Intelligence", charset="UTF-8"'},
        )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return WEB.read_text(encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    return ADMIN_WEB.read_text(encoding="utf-8")


@app.get("/assets/odds-intelligence.svg")
def brand_logo():
    return FileResponse(WEB.parent / "assets" / "odds-intelligence.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    """Public, deliberately non-sensitive status for dashboard and container checks."""
    status = service.status()
    return {
        "status": "ok",
        "app": config.APP_NAME,
        "last_updated": status.get("last_updated"),
        "sports": status.get("sports", config.ODDS_SPORTS),
        "data_freshness": status.get("data_freshness", "unavailable"),
        "sport_freshness": status.get("sport_freshness", []),
        "live_data_available": bool(status.get("live_data_available")),
    }


@app.get("/api/admin/control")
def admin_control():
    status = service.status()
    return {
        "app": config.APP_NAME,
        "providers": status.get("providers", []),
        "provider": status.get("provider"),
        "provider_keys": status.get("provider_keys", {}),
        "provider_usage": status.get("provider_usage", {}),
        "provider_coverage": status.get("provider_coverage", {}),
        "auto_refresh_schedule": status.get("auto_refresh_schedule", {}),
        "auto_refresh_providers": status.get("auto_refresh_providers", []),
        "last_updated": status.get("last_updated"),
        "last_error": status.get("last_error"),
        "sports": status.get("sports", config.ODDS_SPORTS),
        "odds_regions": status.get("odds_regions"),
        "odds_bookmakers": status.get("odds_bookmakers"),
        "odds_markets": config.ODDS_MARKETS,
        "odds_format": config.ODDS_FORMAT,
        "player_props": status.get("player_props", {}),
        "simulation_results": status.get("simulation_results", {}),
        "market_limits": limits_monitor.status(),
        "settings": _admin_settings(),
        "manual_refresh_usage": _manual_refresh_usage(),
    }


@app.post("/api/admin/settings")
async def update_admin_settings(request: Request):
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="A JSON settings object is required.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON settings object is required.")
    current = _admin_settings()
    updated = dict(current)
    if "provider_auto_refresh_enabled" in payload:
        switches = payload["provider_auto_refresh_enabled"]
        if not isinstance(switches, dict):
            raise HTTPException(status_code=422, detail="provider_auto_refresh_enabled must be an object of provider booleans.")
        allowed = set(ADMIN_DEFAULTS["provider_auto_refresh_enabled"])
        if any(name not in allowed or not isinstance(enabled, bool) for name, enabled in switches.items()):
            raise HTTPException(status_code=422, detail="Provider auto-refresh switches must use supported provider names and boolean values.")
        updated["provider_auto_refresh_enabled"] = {
            **current["provider_auto_refresh_enabled"], **switches,
        }
    for key in ("automatic_refresh_enabled", "manual_refresh_enabled"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise HTTPException(status_code=422, detail=f"{key} must be true or false.")
            updated[key] = payload[key]
    for key, low, high in (
        ("manual_refresh_cooldown_seconds", 300, 86400),
        ("manual_refresh_daily_limit", 1, 12),
    ):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(status_code=422, detail=f"{key} must be an integer from {low} to {high}.")
            updated[key] = value
    service.db.set_state("admin_control_settings", json.dumps(updated, sort_keys=True))
    return {"settings": updated, "manual_refresh_usage": _manual_refresh_usage()}


@app.post("/api/admin/refresh")
def admin_refresh(
    sport: str = Query(...),
    providers: list[str] | None = Query(None),
):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select exactly one configured league per manual refresh.")
    active = service.active_provider_names()
    selected = providers if providers is not None else active
    if not selected or len(set(selected)) != len(selected) or any(name not in active for name in selected):
        raise HTTPException(status_code=400, detail="Choose one or more currently enabled providers.")
    reservation = _reserve_manual_refresh_slot()
    result = service.refresh(sport, providers=selected)
    return {
        **result,
        "sport": sport,
        "providers_requested": selected,
        "manual_refresh_usage": {
            "used_today": reservation["used_today"],
            "remaining_today": reservation["remaining_today"],
        },
    }



@app.post("/api/limits/refresh")
def refresh_limits():
    _reserve_manual_refresh_slot()
    oddslink_selected = (
        "oddslink" in service.active_provider_names()
        and service._provider_key_pools["oddslink"].configured
    )
    if oddslink_selected:
        # The OddsLink all-book feed already includes posted-limit metadata.
        # Reuse that single odds request rather than spending a second quota
        # request for the Circa/Pinnacle-only limit poller.
        result = service.refresh(providers=["oddslink"])
        status = service.limits_monitor.status()
        return {
            **status,
            "ok": result.get("ok", False),
            "source": "oddslink_all_book_odds_refresh",
            "error": result.get("error"),
            "diagnostic": status.get("last_diagnostic") or {},
            "odds_refresh": result,
        }
    return service.limits_monitor.refresh()


@app.get("/api/limits")
def market_limits(sport: str | None = None,
                  limit: int = Query(500, ge=1, le=2000)):
    return {"limits": limits_monitor.current(sport, limit),
            "note": "Only provider-reported posted market limits are shown. Missing values are not inferred; limits are not account-specific betting caps."}


@app.get("/api/limits/changes")
def market_limits_changes(sport: str | None = None,
                          direction: str = Query("increased", pattern="^(increased|decreased|all)$"),
                          limit: int = Query(200, ge=1, le=2000)):
    return {"changes": limits_monitor.changes(sport, direction, limit),
            "note": "Changes are calculated from successive stored provider-reported posted-limit snapshots. Observation time is when this application received the snapshot, not a claimed sportsbook publication time."}


@app.get("/api/limits/history")
def market_limits_history(event_id: str, bookmaker: str, market: str,
                          outcome: str, point: float | None = None,
                          limit: int = Query(500, ge=1, le=2000)):
    return {"observations": limits_monitor.history(
        event_id, bookmaker, market, outcome, point, limit
    )}

def _public_events(sport=None):
    """Return subscriber-safe event data without internal provider attribution."""
    def strip_private_fields(value):
        if isinstance(value, dict):
            return {
                key: strip_private_fields(item)
                for key, item in value.items()
                if str(key).lower() not in {
                    "source_provider", "size_source_provider",
                    "provider", "provider_name",
                }
            }
        if isinstance(value, list):
            return [strip_private_fields(item) for item in value]
        return value
    return strip_private_fields(service.events(sport))


@app.get("/api/odds")
def odds(sport: str | None = None):
    # Odds and bookmaker prices are subscriber-facing data; provider diagnostics
    # and account-level quota information stay behind /api/admin/control.
    return {"events": _public_events(sport)}


@app.post("/api/refresh")
def refresh(sport: str | None = None):
    if sport is None:
        raise HTTPException(status_code=400, detail="Manual refresh must target one configured league to protect free-tier quotas.")
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    _reserve_manual_refresh_slot()
    return service.refresh(sport)


@app.get("/api/market-board")
def market_board_api(sport: str | None = None):
    return {"quotes": market_board(_public_events(sport))}


@app.get("/api/bookmaker-board")
def bookmaker_board_api(sport: str | None = None):
    """Return every valid quote from every sportsbook in the merged snapshot."""
    return {"quotes": market_board(_public_events(sport), best_only=False)}


@app.get("/api/simulation/leagues")
def simulation_leagues():
    return {"leagues": [
        {"sport": sport, "label": sport.replace("_", " ").upper()}
        for sport in config.ODDS_SPORTS
    ]}


def _simulation_snapshot_freshness(sport: str):
    """Return the selected league's stored-snapshot freshness record, if any."""
    status = service.status()
    records = status.get("sport_freshness", []) if isinstance(status, dict) else []
    if not isinstance(records, list):
        return None
    return next((item for item in records
                 if isinstance(item, dict) and item.get("sport") == sport), None)


def _simulation_games_for_sport(sport: str):
    games = []
    fitted_models = load_fitted_models(config.SIMULATION_MODEL_PATH)
    league_model = fitted_models.get(sport)
    freshness = _simulation_snapshot_freshness(sport)
    freshness_state = (
        freshness.get("freshness", "unknown")
        if isinstance(freshness, dict) else "unavailable"
    )
    freshness_age = freshness.get("age_seconds") if isinstance(freshness, dict) else None
    for event in _public_events(sport):
        if not event.get("id") or not event.get("home_team") or not event.get("away_team"):
            continue
        from .simulation import market_readiness
        readiness = market_readiness(
            event, model_parameters=league_model, require_independent_model=True
        )
        readiness["snapshot_freshness"] = freshness_state
        readiness["snapshot_age_seconds"] = freshness_age
        if freshness_state != "current":
            readiness["can_simulate"] = False
            freshness_reason = (
                f"The league odds snapshot is {freshness_state}"
                + (f" ({freshness_age} seconds old)" if isinstance(freshness_age, int) else "")
                + "; refresh this league before simulating."
            )
            readiness["reason"] = (
                f'{readiness["reason"]} {freshness_reason}'
                if readiness.get("reason") else freshness_reason
            )
        games.append({
            "event_id": event["id"],
            "matchup": f'{event["away_team"]} @ {event["home_team"]}',
            "commence_time": event.get("commence_time"),
            **readiness,
        })
    games.sort(key=lambda item: item.get("commence_time") or "")
    return games


@app.get("/api/simulation/games")
def simulation_games(sport: str):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    games = _simulation_games_for_sport(sport)
    return {"games": games, "note": "Games come from the latest locally stored odds snapshot; this read-only request makes no provider call."}


@app.post("/api/simulation/games/refresh")
def refresh_simulation_games(sport: str):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    _reserve_manual_refresh_slot()
    result = service.refresh(sport)
    if not result.get("ok"):
        detail = result.get("error") or "The selected league could not be refreshed."
        raise HTTPException(status_code=502, detail=detail)
    games = _simulation_games_for_sport(sport)
    return {
        "games": games,
        "refresh": result,
        "note": "Only the selected league was refreshed. No other league was requested.",
    }


@app.post("/api/simulation/run")
def simulate_game(sport: str, event_id: str, seed: int | None = Query(None, description="Optional integer seed for reproducible simulation runs.")):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    event = next((item for item in _public_events(sport) if str(item.get("id")) == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail="That game is not in the latest stored odds snapshot. Reload the game list.")
    from .simulation import _validate_upcoming_event
    try:
        _validate_upcoming_event(event)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    freshness = _simulation_snapshot_freshness(sport)
    if not isinstance(freshness, dict) or freshness.get("freshness") != "current":
        state = freshness.get("freshness", "unknown") if isinstance(freshness, dict) else "unavailable"
        age = freshness.get("age_seconds") if isinstance(freshness, dict) else None
        detail = (
            f"Simulation blocked: {sport} odds snapshot is {state}"
            + (f" ({age} seconds old)" if isinstance(age, int) else "")
            + ". Refresh this league before simulating."
        )
        raise HTTPException(status_code=409, detail=detail)
    try:
        models = load_fitted_models(config.SIMULATION_MODEL_PATH)
        result = run_game_simulation(
            event, SIMULATION_TRIALS, seed=seed,
            model_parameters=models.get(sport), require_independent_model=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


@app.get("/api/player-props")
def player_props(sport: str | None = None):
    events = _public_events(sport)
    quotes = []
    for event in events:
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if not str(market.get("key", "")).startswith("player_"):
                    continue
                for outcome in market.get("outcomes", []):
                    quotes.append({
                        "event_id": event.get("id"),
                        "sport": event.get("sport_title"),
                        "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                        "commence_time": event.get("commence_time"),
                        "market": market.get("key"),
                        "player": outcome.get("description") or outcome.get("participant") or "Player not identified",
                        "side": outcome.get("name"),
                        "point": outcome.get("point"),
                        "bookmaker": book.get("title", book.get("key")),
                        "bookmaker_key": book.get("key"),
                        "price": outcome.get("price"),
                    })
    quotes.sort(key=lambda row: (row["commence_time"] or "", row["matchup"],
                                 row["player"], row["market"], str(row["point"])))
    return {"quotes": quotes, "status": {"enabled": bool(config.PLAYER_PROPS_ENABLED)},
            "note": "Player prop quotes shown here come from the latest locally stored snapshot."}


@app.get("/api/clv")
def closing_line_value(sport: str | None = None,
                       limit: int = Query(500, ge=1, le=2000)):
    return service.clv_report(sport, limit)


@app.get("/api/movement")
def movement(sport: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    return {"movements": service.movement(sport, limit),
            "note": "Movement reports meaningful changes across consecutive stored snapshots, with opening baselines at the first observed exact line. It is not proof of sharp-money activity."}


@app.get("/api/movement/history")
def movement_history(event_id: str, market: str, outcome: str, bookmaker: str,
                     limit: int = Query(500, ge=1, le=1000),
                     description: str | None = None):
    quotes = (service.movement_history(event_id, market, outcome, bookmaker, limit, description)
              if description is not None else
              service.movement_history(event_id, market, outcome, bookmaker, limit))
    return {"quotes": quotes}


@app.get("/api/history")
def history(event_id: str, market: str, outcome: str,
            bookmaker: str | None = None, point: float | None = None,
            limit: int = Query(100, ge=1, le=500),
            description: str | None = None):
    quotes = (service.history(event_id, market, outcome, bookmaker, point, limit, description)
              if description is not None else
              service.history(event_id, market, outcome, bookmaker, point, limit))
    return {"quotes": quotes}


@app.get("/api/opportunities")
def opportunities(sport: str | None = None,
                  min_ev: float = Query(1.0, ge=-100, le=100),
                  min_consensus_books: int = Query(2, ge=1, le=20)):
    result = analyze_markets(
        _public_events(sport), min_ev, min_consensus_books,
        include_arbitrage=False,
    )
    return {"opportunities": result["opportunities"],
            "note": "Estimated EV uses median no-vig probabilities from other sportsbooks at the same market line; it is not guaranteed."}


@app.get("/api/arbitrage")
def arbitrage(sport: str | None = None):
    result = analyze_markets(
        _public_events(sport), min_ev=101, include_arbitrage=True,
    )
    return {"arbitrage": result["arbitrage"],
            "note": "Two-outcome mathematical price discrepancies only. Confirm both prices, limits, rules and availability before acting."}
