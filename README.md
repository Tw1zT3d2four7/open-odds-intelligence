# Open Odds Intelligence

Open-source, self-hosted sports odds comparison and market-monitoring platform. It is independent of OddsLogic and sportsbooks.

## Current foundation

- FastAPI service and dark browser dashboard.
- Pluggable odds-provider interface and optional The Odds API adapter.
- Normalized event, bookmaker, market and outcome data.
- American-odds conversion, no-vig calculations, estimated EV screening and two-way arbitrage math.
- **Persistent SQLite odds snapshots**: each successful provider refresh is stored on disk with quote history keyed by event, bookmaker, market, outcome and line/point.
- Historical quote endpoint: `GET /api/history?event_id=...&market=...&outcome=...&bookmaker=...&point=...`.
- Docker Compose deployment and automated test workflow.
- No fabricated live data: live quotes remain unavailable until a supported provider is configured.

This remains an early development build, **not yet a feature-complete OddsLogic replacement**. Opportunity screening is still moneyline-only. Spreads/totals are stored with their line points but are not yet fully evaluated for EV or arbitrage. Scheduled refresh, line-movement UI, alerts, player props and production-grade multi-provider consensus remain development work. EV is an estimate, not a guarantee or betting advice.

## Quick start

```bash
cp .env.example .env
# Set ODDS_PROVIDER=the_odds_api and THE_ODDS_API_KEY if you have a key.
# Preserve the key exactly; API keys are case-sensitive.
docker compose up --build
```

Open http://localhost:8765. SQLite data persists in `./data/odds_history.sqlite3` through the Compose data volume.

Local install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Set `DATABASE_PATH` to change the SQLite file location.

## Provider configuration

Configure `ODDS_PROVIDER=the_odds_api`, `THE_ODDS_API_KEY`, regions, markets and sports in `.env`. Provider coverage, quota, historical access and permitted use depend on the provider's current plan and terms. The application does not scrape sportsbook websites.

## API

- `GET /api/health`
- `GET /api/odds?sport=...`
- `GET /api/market-board?sport=...` (best price for each exact market/outcome/line; alternate points remain separate)
- `GET /api/history?event_id=...&market=...&outcome=...&bookmaker=...&point=...`
- `GET /api/opportunities?sport=...&min_ev=1.0`
- `POST /api/refresh?sport=...`

## Development roadmap

1. ~~Persistent SQLite quote/event storage~~ (implemented).
2. ~~Exact-line best-price comparison for available markets~~ (implemented; market-specific EV and arbitrage remain).
3. Multi-provider consensus with leave-one-book-out fair probability estimates.
4. Arbitrage scanning across aligned outcomes and books.
5. Historical line movement, closing-line value and backtesting.
6. Player props where data feeds support them.
7. Deduplicated Discord/webhook alerts and injury/news integrations where licensed feeds exist.
8. Provider health, quota visibility, robust validation and optional remote authentication.

## License

MIT. Third-party odds data remains subject to each provider's terms and licenses.
