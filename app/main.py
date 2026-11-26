from pathlib import Path
import base64
import binascii
import logging
import asyncio
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import config
from .service import service

# Reuse the monitor owned by the service so OddsLink odds refreshes and the
# market-limits dashboard share one database-backed state object.
limits_monitor = service.limits_monitor
from .markets import market_board, analyze_markets
from .simulation import load_fitted_models, run_game_simulation, TRIALS as SIMULATION_TRIALS
from .route_policy import IDENTITY_ROUTE_ACCESS_POLICY, classify_route, matched_route_path
from .route_policy import ROUTE_ACCESS_POLICY
from .route_authorization import dependencies_for_route
from .entitlements import (
    require_product_administrator,
    require_product_csrf,
    require_product_entitlement,
    require_product_subscriber,
)
from .dependencies import get_identity_store
from .auth import resolve_session
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

def _include_premium_data(request: Request) -> bool:
    """Return the server-authorized source scope established by route dependencies."""
    return config.AUTH_MODE == "legacy_basic" or bool(
        getattr(request.state, "include_premium_data", False)
    )


async def _limits_refresh_loop():
    while True:
        delay = config.LIMITS_REFRESH_INTERVAL_SECONDS
        try:
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
        # Refresh providers immediately when their independent schedule is due.
        # Keep provider I/O and SQLite work off the ASGI event loop.
        try:
            due_providers = await asyncio.to_thread(service.providers_due_for_auto_refresh)
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
    email_task = None
    if (not _include_premium_data(request)
            and config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink") != "free"):
        return {"ok": False, "skipped": True, "error": "Market-limit source is not available in the free data tier."}
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
    if service.discord_results_sync_enabled() or (
            config.SIMULATION_RESULTS_SYNC_ENABLED
            and "therundown" in service.active_provider_names()
            and service._provider_key_pools["therundown"].configured):
        results_task = asyncio.create_task(_simulation_results_loop())
    if config.AUTH_MODE == "identity" and config.EMAIL_PROVIDER == "resend":
        from .email_queue_worker import email_delivery_loop
        email_task = asyncio.create_task(email_delivery_loop())
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
        if email_task is not None:
            email_task.cancel()
            try:
                await email_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title=config.APP_NAME, version="0.4.0",
              description="Self-hosted open-source odds comparison and market intelligence.",
              lifespan=lifespan)
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    """Enforce the selected legacy Basic Auth or staged identity boundary.

    Legacy mode preserves the original optional Basic Auth contract. Identity
    mode admits only classified identity/product routes; product route
    dependencies enforce subscriber entitlements, admin sessions, and CSRF.
    The safe health probe remains independent of browser authentication.
    """
    username = config.AUTH_USERNAME
    password = config.AUTH_PASSWORD
    # Keep the orchestrator health probe available in every authentication mode.
    if request.url.path == "/api/health" and request.method in {"GET", "HEAD"}:
        return await call_next(request)

    # Resolve parameterized route templates before policy lookup. Middleware
    # executes before Starlette populates scope["route"], so exact URL lookup alone
    # would reject valid subscriber/session-specific admin endpoints.
    policy_path = matched_route_path(request.scope, request.app.router.routes) or request.url.path
    # Stripe authenticates this transport with its signed raw-body header, not browser Basic Auth.
    if classify_route(request.method, policy_path, ROUTE_ACCESS_POLICY) == "stripe_signature":
        return await call_next(request)
    identity_path_set = {path for _method, path in IDENTITY_ROUTE_ACCESS_POLICY}
    if config.AUTH_MODE == "identity":
        # Identity pages/endpoints are admitted by their separate policy. Existing
        # product endpoints are admitted only because each APIRoute has explicit
        # session/entitlement dependencies assembled from ROUTE_ACCESS_POLICY.
        if classify_route(request.method, policy_path, IDENTITY_ROUTE_ACCESS_POLICY) is not None:
            return await call_next(request)
        product_access = classify_route(
            request.method, policy_path, ROUTE_ACCESS_POLICY
        )
        if product_access in {"public_minimal", "stripe_signature"}:
            # Stripe's route handler performs HMAC authentication against the exact raw body.
            return await call_next(request)
        if product_access in {"subscriber_entitled", "administrator_only"}:
            # Framework-generated docs routes cannot receive APIRoute dependencies;
            # protect those exact paths here with the same server-side admin session.
            if policy_path in {
                "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"
            }:
                try:
                    resolve_session(request, get_identity_store(request), "administrator")
                except HTTPException as exc:
                    return JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                    )
                except Exception:
                    return JSONResponse(
                        status_code=503,
                        content={"detail": "Authentication services are temporarily unavailable."},
                    )
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={"detail": "This route has no enabled identity-mode access policy."},
        )

    # The identity endpoints are not part of the legacy self-hosted experience.
    # Hide them in legacy mode even when legacy Basic Auth is intentionally unset.
    if policy_path in identity_path_set:
        return JSONResponse(status_code=404, content={"detail": "Not found."})

    if bool(username) != bool(password):
        return JSONResponse(
            status_code=503,
            content={"detail": "Authentication is misconfigured; set both AUTH_USERNAME and AUTH_PASSWORD."},
        )
    if not username:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    supplied_user = supplied_password = ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "basic" and token:
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
            supplied_user, separator, supplied_password = decoded.partition(":")
            if not separator:
                supplied_user = supplied_password = ""
        except (binascii.Error, UnicodeDecodeError, ValueError):
            supplied_user = supplied_password = ""

    valid_user = secrets.compare_digest(supplied_user.encode(), username.encode())
    valid_password = secrets.compare_digest(supplied_password.encode(), password.encode())
    if not (valid_user and valid_password):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="Open Odds Intelligence", charset="UTF-8"'},
        )
    return await call_next(request)


@app.middleware("http")
async def prevent_identity_response_caching(request: Request, call_next):
    """Prevent browsers and intermediaries from caching auth/admin responses, including errors."""
    response = await call_next(request)
    path = request.url.path
    identity_page_paths = {
        "/login", "/register", "/admin", "/admin/login", "/account",
        "/forgot-password", "/verify-email", "/reset-password",
    }
    protected_product = False
    if config.AUTH_MODE == "identity":
        access_class = classify_route(request.method, path, ROUTE_ACCESS_POLICY)
        protected_product = access_class in {"subscriber_entitled", "administrator_only"}
    if (
        path in identity_page_paths
        or path.startswith(("/auth/", "/admin/"))
        or protected_product
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse, dependencies=dependencies_for_route("GET", "/", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def dashboard():
    return WEB.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    # Identity mode exposes only the liveness contract needed by Docker/Caddy.
    # Detailed provider and operational diagnostics are exposed only through
    # the MFA-protected Admin Overview, never through the public health route.
    if config.AUTH_MODE == "identity":
        return {"status": "ok", "app": config.APP_NAME}
    return {"status": "ok", "app": config.APP_NAME, **service.status(),
            "market_limits": limits_monitor.status()}



@app.post("/api/limits/refresh", dependencies=dependencies_for_route("POST", "/api/limits/refresh", subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def refresh_limits(request: Request):
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


@app.get("/api/limits", dependencies=dependencies_for_route("GET", "/api/limits", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def market_limits(request: Request, sport: str | None = None,
                  limit: int = Query(25, ge=1, le=100),
                  offset: int = Query(0, ge=0)):
    rows = (limits_monitor.current(sport, 2000) if _include_premium_data(request) or config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink") == "free" else [])
    page = _page_results(rows, limit, offset)
    return {"limits": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "status": limits_monitor.status(),
            "note": "Only provider-reported posted market limits are shown. Missing values are not inferred; limits are not account-specific betting caps."}


@app.get("/api/limits/changes", dependencies=dependencies_for_route("GET", "/api/limits/changes", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def market_limits_changes(request: Request, sport: str | None = None,
                          direction: str = Query("increased", pattern="^(increased|decreased|all)$"),
                          limit: int = Query(25, ge=1, le=100),
                          offset: int = Query(0, ge=0)):
    rows = (limits_monitor.changes(sport, direction, 2000) if _include_premium_data(request) or config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink") == "free" else [])
    page = _page_results(rows, limit, offset)
    return {"changes": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "note": "Changes are calculated from successive stored provider-reported posted-limit snapshots. Observation time is when this application received the snapshot, not a claimed sportsbook publication time."}


@app.get("/api/limits/history", dependencies=dependencies_for_route("GET", "/api/limits/history", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def market_limits_history(request: Request, event_id: str, bookmaker: str, market: str,
                          outcome: str, point: float | None = None,
                          limit: int = Query(500, ge=1, le=2000)):
    observations = (limits_monitor.history(
        event_id, bookmaker, market, outcome, point, limit
    ) if _include_premium_data(request) or config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink") == "free" else [])
    return {"observations": observations}

@app.get("/api/odds", dependencies=dependencies_for_route("GET", "/api/odds", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def odds(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    return {"events": service.events(sport, limit=limit, offset=offset, include_premium=_include_premium_data(request)), "limit": limit, "offset": offset, "status": service.status()}


@app.post("/api/refresh", dependencies=dependencies_for_route("POST", "/api/refresh", subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def refresh(sport: str | None = None):
    if sport is not None and sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league or omit sport to refresh all configured leagues.")
    return service.refresh(sport)


def _page_results(items, limit, offset):
    """Paginate rendered result rows, not source events (one event can yield many rows)."""
    total = len(items)
    start = max(0, int(offset))
    size = max(1, min(int(limit), 100))
    return {"items": items[start:start + size], "total": total,
            "limit": size, "offset": start}


@app.get("/api/market-board", dependencies=dependencies_for_route("GET", "/api/market-board", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def market_board_api(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    page = _page_results(market_board(service.events(sport, include_premium=_include_premium_data(request))), limit, offset)
    return {"quotes": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"]}


@app.get("/api/bookmaker-board", dependencies=dependencies_for_route("GET", "/api/bookmaker-board", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def bookmaker_board_api(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    """Return every valid quote, paginated as 25 rendered sportsbook rows."""
    page = _page_results(market_board(service.events(sport, include_premium=_include_premium_data(request)), best_only=False), limit, offset)
    return {"quotes": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"]}


@app.get("/api/simulation/leagues", dependencies=dependencies_for_route("GET", "/api/simulation/leagues", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
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


def _simulation_games_for_sport(sport: str, include_premium: bool = False):
    games = []
    fitted_models = load_fitted_models(config.SIMULATION_MODEL_PATH)
    league_model = fitted_models.get(sport)
    freshness = _simulation_snapshot_freshness(sport)
    freshness_state = (
        freshness.get("freshness", "unknown")
        if isinstance(freshness, dict) else "unavailable"
    )
    freshness_age = freshness.get("age_seconds") if isinstance(freshness, dict) else None
    for event in service.events(sport, include_premium=include_premium):
        if not event.get("id") or not event.get("home_team") or not event.get("away_team"):
            continue
        from .simulation import market_readiness
        readiness = market_readiness(
            event, model_parameters=league_model, require_independent_model=bool(league_model)
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


@app.get("/api/simulation/games", dependencies=dependencies_for_route("GET", "/api/simulation/games", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def simulation_games(request: Request, sport: str):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    games = _simulation_games_for_sport(sport, _include_premium_data(request))
    return {"games": games, "note": "Games come from the latest locally stored odds snapshot; this read-only request makes no provider call."}


@app.post("/api/simulation/games/refresh", dependencies=dependencies_for_route("POST", "/api/simulation/games/refresh", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def refresh_simulation_games(request: Request, sport: str):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    result = service.refresh(sport)
    if not result.get("ok"):
        detail = result.get("error") or "The selected league could not be refreshed."
        raise HTTPException(status_code=502, detail=detail)
    games = _simulation_games_for_sport(sport, _include_premium_data(request))
    return {
        "games": games,
        "refresh": result,
        "note": "Only the selected league was refreshed. No other league was requested.",
    }


@app.post("/api/simulation/run", dependencies=dependencies_for_route("POST", "/api/simulation/run", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def simulate_game(request: Request, sport: str, event_id: str, seed: int | None = Query(None, description="Optional integer seed for reproducible simulation runs.")):
    if sport not in config.ODDS_SPORTS:
        raise HTTPException(status_code=400, detail="Select a configured league.")
    event = next((item for item in service.events(sport, include_premium=_include_premium_data(request)) if str(item.get("id")) == event_id), None)
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
            model_parameters=models.get(sport), require_independent_model=bool(models.get(sport)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


@app.get("/api/player-props", dependencies=dependencies_for_route("GET", "/api/player-props", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def player_props(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    events = service.events(sport, include_premium=_include_premium_data(request))
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
    page = _page_results(quotes, limit, offset)
    return {"quotes": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "status": service.status()["player_props"],
            "note": "Player props are fetched only when explicitly enabled and only from supported provider event-odds endpoints. Each event-level request can consume provider quota."}


@app.get("/api/clv", dependencies=dependencies_for_route("GET", "/api/clv", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def closing_line_value(request: Request, sport: str | None = None,
                       limit: int = Query(25, ge=1, le=100),
                       offset: int = Query(0, ge=0)):
    result = service.clv_report(sport, 2000) if _include_premium_data(request) else {
        "entries": [], "note": "Historical CLV is temporarily withheld from free accounts until source-level classification is available for every archived calculation."
    }
    page = _page_results(result.get("entries", []), limit, offset)
    return {**result, "entries": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"]}


@app.get("/api/movement", dependencies=dependencies_for_route("GET", "/api/movement", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def movement(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100),
             offset: int = Query(0, ge=0)):
    rows = service.movement(sport, 1000, include_premium=_include_premium_data(request))
    page = _page_results(rows, limit, offset)
    return {"movements": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "note": "Movement reports meaningful changes across consecutive stored snapshots, with opening baselines at the first observed exact line. It is not proof of sharp-money activity."}


@app.get("/api/movement/history", dependencies=dependencies_for_route("GET", "/api/movement/history", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def movement_history(request: Request, event_id: str, market: str, outcome: str, bookmaker: str,
                     limit: int = Query(500, ge=1, le=1000),
                     description: str | None = None):
    quotes = (service.movement_history(event_id, market, outcome, bookmaker, limit, description, _include_premium_data(request))
              if description is not None else
              service.movement_history(event_id, market, outcome, bookmaker, limit, include_premium=_include_premium_data(request)))
    return {"quotes": quotes}


@app.get("/api/history", dependencies=dependencies_for_route("GET", "/api/history", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def history(request: Request, event_id: str, market: str, outcome: str,
            bookmaker: str | None = None, point: float | None = None,
            limit: int = Query(100, ge=1, le=500),
            description: str | None = None):
    quotes = (service.history(event_id, market, outcome, bookmaker, point, limit, description, _include_premium_data(request))
              if description is not None else
              service.history(event_id, market, outcome, bookmaker, point, limit, include_premium=_include_premium_data(request)))
    return {"quotes": quotes}


@app.get("/api/opportunities", dependencies=dependencies_for_route("GET", "/api/opportunities", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def opportunities(request: Request, sport: str | None = None,
                  min_ev: float = Query(1.0, ge=-100, le=100),
                  min_consensus_books: int = Query(2, ge=1, le=20),
                  limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    result = analyze_markets(service.events(sport, include_premium=_include_premium_data(request)), min_ev, min_consensus_books)
    page = _page_results(result["opportunities"], limit, offset)
    return {"opportunities": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "note": "Estimated EV uses median no-vig probabilities from other sportsbooks at the same market line; it is not guaranteed."}


@app.get("/api/arbitrage", dependencies=dependencies_for_route("GET", "/api/arbitrage", entitlement_dependency=require_product_entitlement, subscriber_dependency=require_product_subscriber, administrator_dependency=require_product_administrator, csrf_dependency=require_product_csrf))
def arbitrage(request: Request, sport: str | None = None, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    result = analyze_markets(service.events(sport, include_premium=_include_premium_data(request)), min_ev=101, arbitrage_only=True)
    page = _page_results(result["arbitrage"], limit, offset)
    return {"arbitrage": page["items"], "total": page["total"],
            "limit": page["limit"], "offset": page["offset"],
            "note": "Two-outcome mathematical price discrepancies only. Confirm both prices, limits, rules and availability before acting."}



# Import and mount after the product routes are declared. This keeps router
# registration deterministic even when the identity modules are imported by
# test harnesses or future application factories first.
from .identity_routes import router as identity_router
from .billing_routes import receive_stripe_webhook

app.include_router(identity_router)
# Register the signed webhook as a concrete application route. This keeps it
# present in the runtime route inventory while remaining excluded from OpenAPI.
app.add_api_route(
    "/webhooks/stripe",
    receive_stripe_webhook,
    methods=["POST"],
    include_in_schema=False,
)
_registered_identity_route_keys = {
    (method, route.path)
    for route in app.router.routes
    for method in (getattr(route, "methods", None) or set())
    if method not in {"HEAD", "OPTIONS"}
}
for _identity_route in identity_router.routes:
    _identity_route_keys = {
        (method, _identity_route.path)
        for method in (_identity_route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    if not _identity_route_keys.issubset(_registered_identity_route_keys):
        app.router.routes.append(_identity_route)
        _registered_identity_route_keys.update(_identity_route_keys)
