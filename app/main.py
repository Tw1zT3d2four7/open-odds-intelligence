from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import config
from .service import service
from .markets import market_board, analyze_markets

app = FastAPI(title=config.APP_NAME, version="0.3.0",
              description="Self-hosted open-source odds comparison and market intelligence.")
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return WEB.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok", "app": config.APP_NAME, **service.status()}


@app.get("/api/odds")
def odds(sport: str | None = None):
    return {"events": service.events(sport), "status": service.status()}


@app.post("/api/refresh")
def refresh(sport: str | None = None):
    return service.refresh(sport)


@app.get("/api/market-board")
def market_board_api(sport: str | None = None):
    return {"quotes": market_board(service.events(sport))}


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
