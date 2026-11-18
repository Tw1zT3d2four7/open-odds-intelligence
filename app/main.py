from pathlib import Path
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import config
from .service import service
from .markets import market_board, analyze_markets
from .limits import limits_monitor

logger = logging.getLogger(__name__)

async def _limits_refresh_loop():
    while True:
        try:
            await asyncio.to_thread(limits_monitor.refresh)
        except Exception as exc:
            limits_monitor.last_error = f"Unexpected scheduled market-limits refresh failure: {exc}"
            logger.exception("Scheduled market-limits refresh failed")
        await asyncio.sleep(config.LIMITS_REFRESH_INTERVAL_SECONDS)


async def _automatic_refresh_loop():
    interval = (
        config.RUNDOWN_REFRESH_INTERVAL_SECONDS
        if config.ODDS_PROVIDER == "therundown"
        else (config.ODDS_REFRESH_INTERVAL_SECONDS or (config.DISCORD_ALERT_REFRESH_INTERVAL_SECONDS if config.DISCORD_WEBHOOK_URL else 0))
    )
    while True:
        await asyncio.sleep(interval)
        # Keep provider I/O and SQLite work off the ASGI event loop.
        try:
            await asyncio.to_thread(service.refresh)
        except Exception as exc:
            # Keep the scheduler alive, but expose failures in /api/health.
            service._last_error = f"Unexpected scheduled odds refresh failure: {exc}"
            logger.exception("Scheduled odds refresh failed")
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    limits_task = None
    if config.ODDSLINK_API_KEY:
        limits_task = asyncio.create_task(_limits_refresh_loop())
    if (config.ODDS_PROVIDER == "therundown" or config.ODDS_REFRESH_INTERVAL_SECONDS > 0 or config.DISCORD_WEBHOOK_URL) and service.configured:
        task = asyncio.create_task(_automatic_refresh_loop())
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


app = FastAPI(title=config.APP_NAME, version="0.4.0",
              description="Self-hosted open-source odds comparison and market intelligence.",
              lifespan=lifespan)
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return WEB.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok", "app": config.APP_NAME, **service.status(),
            "market_limits": limits_monitor.status()}



@app.post("/api/limits/refresh")
def refresh_limits():
    return limits_monitor.refresh()


@app.get("/api/limits")
def market_limits(sport: str | None = None,
                  limit: int = Query(500, ge=1, le=2000)):
    return {"limits": limits_monitor.current(sport, limit),
            "status": limits_monitor.status(),
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

@app.get("/api/odds")
def odds(sport: str | None = None):
    return {"events": service.events(sport), "status": service.status()}


@app.post("/api/refresh")
def refresh(sport: str | None = None):
    return service.refresh(sport)


@app.get("/api/market-board")
def market_board_api(sport: str | None = None):
    return {"quotes": market_board(service.events(sport))}


@app.get("/api/player-props")
def player_props(sport: str | None = None):
    events = service.events(sport)
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
    return {"quotes": quotes, "status": service.status()["player_props"],
            "note": "Player props are fetched only when explicitly enabled and only from supported provider event-odds endpoints. Each event-level request can consume provider quota."}


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
    result = analyze_markets(service.events(sport), min_ev, min_consensus_books)
    return {"opportunities": result["opportunities"],
            "note": "Estimated EV uses median no-vig probabilities from other sportsbooks at the same market line; it is not guaranteed."}


@app.get("/api/arbitrage")
def arbitrage(sport: str | None = None):
    result = analyze_markets(service.events(sport), min_ev=101)
    return {"arbitrage": result["arbitrage"],
            "note": "Two-outcome mathematical price discrepancies only. Confirm both prices, limits, rules and availability before acting."}
