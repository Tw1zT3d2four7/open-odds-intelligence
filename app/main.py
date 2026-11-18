from pathlib import Path
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import config
from .service import service
from .markets import market_board, analyze_markets
from .limits import limits_monitor

async def _limits_refresh_loop():
    while True:
        await asyncio.sleep(config.LIMITS_REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(limits_monitor.refresh)
        except Exception:
            continue


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
        except Exception:
            # A transient storage/runtime failure must not silently kill the scheduler.
            # The next interval retries; manual refresh remains available.
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


@app.get("/api/movement")
def movement(sport: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    return {"movements": service.movement(sport, limit),
            "note": "Movement reports meaningful changes across consecutive stored snapshots, with opening baselines at the first observed exact line. It is not proof of sharp-money activity."}


@app.get("/api/movement/history")
def movement_history(event_id: str, market: str, outcome: str, bookmaker: str,
                     limit: int = Query(500, ge=1, le=1000)):
    return {"quotes": service.movement_history(
        event_id, market, outcome, bookmaker, limit
    )}


@app.get("/api/history")
def history(event_id: str, market: str, outcome: str,
            bookmaker: str | None = None, point: float | None = None,
            limit: int = Query(100, ge=1, le=500)):
    return {"quotes": service.history(event_id, market, outcome, bookmaker, point, limit)}


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
