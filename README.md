# Open Odds Intelligence

An open-source, self-hosted sports odds comparison and market-monitoring platform. Independent of OddsLogic and all sportsbooks.

## Foundation included
- FastAPI backend and browser dashboard
- Pluggable odds-provider contract
- Optional The Odds API integration using a user-supplied key
- Normalized events, bookmakers, markets and outcomes
- Best-price comparison, no-vig two-way probabilities, positive-EV screening and two-outcome arbitrage math
- Docker Compose deployment and starter tests

**No fabricated live data:** without a configured provider the dashboard explicitly reports that live odds are unavailable. EV is an estimate, not a guarantee or betting advice.

## Quick start
```bash
cp .env.example .env
# Optionally configure ODDS_PROVIDER and THE_ODDS_API_KEY (preserve exact key case)
docker compose up --build
```
Open http://localhost:8765

Local install:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

## Provider setup
Set `ODDS_PROVIDER=the_odds_api` and `THE_ODDS_API_KEY=...`. Sports, regions and markets are configurable in `.env`. Upstream coverage, limits, historical access and permitted use depend on the provider's current plan and terms. The app does not scrape sportsbook websites.

## API
- `GET /api/health`
- `GET /api/odds?sport=...`
- `GET /api/opportunities?sport=...&min_ev=1.0`
- `POST /api/refresh`

## Roadmap
1. More interchangeable data providers and provider health/rate-limit visibility.
2. Scheduled refresh, quote history and line movement charts.
3. Player props and additional market types where feeds support them.
4. Closing-line value tracking and historical backtesting.
5. Deduplicated Discord/webhook alerts.
6. Multi-provider consensus and stronger EV/arbitrage validation.
7. Optional authentication for remote multi-user deployments.

## License
MIT. Third-party data remains subject to its own provider terms and licenses.
