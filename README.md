# Open Odds Intelligence

Open-source, self-hosted sports odds comparison and market-monitoring platform. Independent of OddsLogic and sportsbooks.

## Current working features

- FastAPI backend and responsive dark dashboard.
- Pluggable odds-provider interface and The Odds API adapter.
- Configurable sports, regions and supported market keys.
- Normalized event, sportsbook, market and outcome quotes.
- Best available price for each exact event / market / outcome / line.
- American-odds conversion and no-vig probability calculations.
- Estimated EV screening across moneyline, spreads and totals when the provider supplies complete markets.
- Leave-one-sportsbook-out consensus: a quote is evaluated against other books, not its own price.
- Two-outcome cross-book arbitrage scanning with suggested equal-payout stake percentages.
- Persistent SQLite snapshots and quote history.\n- Per-sportsbook price / line movement comparison between the latest two saved snapshots.
- Dashboard views for EV opportunities, arbitrage candidates and the best-price board.
- Sport filter, EV threshold and minimum independent-book consensus controls.
- Opt-in scheduled refresh at a configurable interval (disabled by default).\n- Optional Discord EV and arbitrage alerts with persistent duplicate suppression and retry after delivery failure.\n- Docker Compose deployment and GitHub Actions tests.

The EV calculation uses the median of other sportsbooks' no-vig probabilities for the same event, market, outcome and line. The default requires at least two other books (three books total). Results are estimates, not guarantees. Arbitrage candidates are mathematical price discrepancies only; verify both prices, limits, settlement rules and availability before acting.

## Not implemented yet

This is an active development build, not a feature-complete OddsLogic replacement. Closing-line value tracking, player props, additional provider integrations, authentication and full historical backtesting remain future work. Sharp-money activity cannot be established from public line movement alone; any future movement labels will be described as market-movement signals.

No fabricated odds are generated. Live quotes remain unavailable until a supported provider is configured. The application does not scrape sportsbook websites.

## Quick start

```bash
cp .env.example .env
# Set ODDS_PROVIDER=the_odds_api and THE_ODDS_API_KEY in .env.
# Preserve the API key exactly; keys are case-sensitive.
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

Set `DATABASE_PATH` to change the SQLite file location. Set `ODDS_REFRESH_INTERVAL_SECONDS` to a positive number of seconds (for example, `300`) for general automatic refresh. When a Discord webhook is configured, the app automatically refreshes odds every 5 minutes by default even if general auto-refresh is disabled; change this with `DISCORD_ALERT_REFRESH_INTERVAL_SECONDS` (minimum 60 seconds). Automatic refresh starts only when a provider and API key are configured. To enable Discord alerts, set `DISCORD_WEBHOOK_URL`, `ALERT_MIN_EV_PERCENT` and `ALERT_MIN_ARB_PERCENT`. The health endpoint reports the last alert check, evaluated candidates, qualifying candidates and delivery errors. Alerts are disabled when no webhook URL is set. Alert fingerprints are stored in SQLite to prevent repeated notifications for the same exact quote; a changed price creates a new alert.

## Provider configuration

The default provider is disabled. To use The Odds API, configure:

```dotenv
ODDS_PROVIDER=the_odds_api
THE_ODDS_API_KEY=your_exact_case_sensitive_key
ODDS_REGIONS=us
ODDS_MARKETS=h2h,spreads,totals
ODDS_FORMAT=american
```

Available sports are controlled by `ODDS_SPORTS`. Provider coverage, quota, historical access, market availability and permitted use depend on the provider's current plan and terms. Request usage can increase with the number of sports, regions and markets requested.

## API

- `GET /api/health` — provider configuration and stored-data status.
- `GET /api/odds?sport=...` — latest stored event quotes.
- `POST /api/refresh?sport=...` — request a provider refresh.
- `GET /api/market-board?sport=...` — best price for each exact market / outcome / line.
- `GET /api/opportunities?sport=...&min_ev=1.0&min_consensus_books=2` — estimated EV opportunities.
- `GET /api/arbitrage?sport=...` — two-outcome cross-book arbitrage candidates.
- `GET /api/history?event_id=...&market=...&outcome=...&bookmaker=...&point=...` — stored quote history.\n- `GET /api/movement?sport=...` — price and line changes versus the previous stored snapshot.

## Development roadmap

1. ~~Persistent SQLite quote and event storage~~
2. ~~Exact-line best-price comparison~~
3. ~~Multi-market EV screening using other-book consensus~~
4. ~~Two-outcome cross-book arbitrage scanning~~
5. Historical line-movement views, closing-line value and backtesting.
6. Player props where licensed data feeds support correct player / line identity.
7. Deduplicated Discord/webhook alerts and injury/news integrations where licensed feeds exist.
8. Additional providers, richer provider diagnostics, robust validation and optional authentication.

## License

MIT. Third-party odds data remains subject to each provider's terms and licenses.
