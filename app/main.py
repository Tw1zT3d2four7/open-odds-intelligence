from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import config
from .service import service
from .math import market_consensus, expected_value_percent

app = FastAPI(title=config.APP_NAME, version="0.2.0",
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


@app.get("/api/history")
def history(event_id: str, market: str, outcome: str,
            bookmaker: str | None = None, point: float | None = None,
            limit: int = Query(100, ge=1, le=500)):
    return {"quotes": service.history(event_id, market, outcome, bookmaker, point, limit)}


@app.get("/api/opportunities")
def opportunities(sport: str | None = None, min_ev: float = Query(1.0, ge=-100, le=100)):
    events = service.events(sport)
    rows = []
    for event in events:
        best = {}
        for book in event.get("bookmakers", []):
            fair = market_consensus(
                [event], "h2h", exclude_bookmaker_key=book.get("key")
            ).get(event["id"], {})
            market = next((m for m in book.get("markets", []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            for outcome in market.get("outcomes", []):
                name = outcome["name"]
                if name not in fair:
                    continue
                try:
                    ev = expected_value_percent(outcome["price"], fair[name])
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if ev >= min_ev and (name not in best or outcome["price"] > best[name]["price"]):
                    best[name] = {"outcome": name, "bookmaker": book["title"], "price": outcome["price"],
                                  "fair_probability": round(fair[name], 5), "ev_percent": round(ev, 2)}
        for item in best.values():
            rows.append({"event_id": event["id"], "sport": event["sport_title"],
                         "matchup": f'{event["away_team"]} @ {event["home_team"]}',
                         "commence_time": event["commence_time"], **item})
    return {"opportunities": rows,
            "note": "EV is an estimate against available-book consensus, not a guaranteed edge."}
