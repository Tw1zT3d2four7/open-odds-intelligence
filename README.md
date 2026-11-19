# Open Odds Intelligence

Open-source, self-hosted sports odds comparison and market-monitoring platform. Independent of OddsLogic and sportsbooks.

## Current working features

- FastAPI backend and responsive dark dashboard.
- Quota-free betting calculators: payout, multi-leg parlay, two-way no-vig, EV / fractional Kelly bankroll sizing, equal-outcome hedging, -1 run-line / puck-line settlement and a synthetic -1 line builder, with a dashboard-wide American / Decimal odds toggle.
- Pluggable odds-provider interface with The Odds API and TheRundown V2 adapters.
- Configurable sports, regions and supported market keys. NFL, NCAAF, NBA, NCAAB, NHL, MLB and EPL are included in the default league catalog; ATP and WTA are planned for a later tennis expansion when provider coverage is verified.
- Normalized event, sportsbook, market and outcome quotes.
- Best available price for each exact event / market / outcome / line.
- American-odds conversion and no-vig probability calculations.
- Estimated EV screening across moneyline, spreads and totals when the provider supplies complete markets.
- Leave-one-sportsbook-out consensus: a quote is evaluated against other books, not its own price.
- Two-outcome cross-book arbitrage scanning with suggested equal-payout stake percentages.
- Persistent SQLite snapshots and quote history.
- Consolidated opening-to-latest sportsbook price / line movement summaries, with exact alternate-line separation.
- Optional free-tier posted sportsbook market-limit monitoring via OddsLink, with persistent limit history, increase/decrease detection, clickable per-selection limit timelines and Discord increase alerts.
- Clickable movement timelines showing every saved line and price observation for a selection.
- Automatic first-qualified-quote capture and closing-line value (CLV) reports against the last pregame no-vig consensus from at least two other complete same-line books.
- Dashboard views for EV opportunities, arbitrage candidates, the best-price board, an optional player-props comparison board and a selected-game 10,000-trial Monte Carlo simulator.
- Sport filter, EV threshold and minimum independent-book consensus controls.
- Opt-in scheduled refresh at a configurable interval (disabled by default).
- Optional Discord EV and arbitrage alerts with persistent duplicate suppression and retry after delivery failure.
- Docker Compose deployment and GitHub Actions tests.

The EV calculation uses the median of other sportsbooks' no-vig probabilities for the same event, market, outcome and line. The default requires at least two other books (three books total). Results are estimates, not guarantees. Arbitrage candidates are mathematical price discrepancies only; verify both prices, limits, settlement rules and availability before acting.

## Access control and deployment security

The dashboard and API support optional HTTP Basic authentication. Set both `AUTH_USERNAME` and `AUTH_PASSWORD` in the private server-side `.env` file to protect the dashboard and all API routes. Leave both blank only for a trusted private development network. If exactly one is configured, protected requests fail closed with HTTP 503 rather than silently running without authentication. The `/api/health` endpoint remains unauthenticated for Docker health checks and should not be exposed publicly without a reverse proxy/network policy. Basic authentication must be used over HTTPS when traffic crosses an untrusted network; place the application behind a TLS-terminating reverse proxy before enabling remote access. Do not put credentials in the repository, screenshots, issue reports, or chat.

## Not implemented yet

This is an active development build, not a feature-complete OddsLogic replacement. Additional provider integrations, authentication and full historical strategy backtesting remain future work. CLV tracking is now implemented, but it requires new observations after installation and enough complete pregame snapshots to calculate valid closes. The backtesting implementation plan is documented in [docs/BACKTESTING_DESIGN.md](docs/BACKTESTING_DESIGN.md); it requires prospective quote history and trustworthy settlement data, and will not fabricate missing results. Sharp-money activity cannot be established from public line movement alone; any future movement labels will be described as market-movement signals.

No fabricated odds or limits are generated. Live quotes remain unavailable until a supported provider is configured. The application does not scrape sportsbook websites.

## Posted market limits (free tier)

The optional Market Limits view uses the free OddsLink API plan to read explicit sportsbook `size.kind=posted_limit` values. Its documented free allowance is 1,000 price requests per month. With the five default limit-monitor leagues and a four-hour polling interval, the monitor uses approximately 900 requests per 30-day month (five sports × six polls/day × 30 days). NCAAF and NCAAB are part of the platform's default odds coverage but are not silently added to OddsLink polling, which would exceed the 1,000-request free allowance. Add them explicitly to `LIMITS_SPORTS` only if your plan has sufficient remaining quota. Keep the league list and polling interval within your account allowance; the application displays quota headers when the provider returns them. `ODDSLINK_API_KEY` is separate from `ODDS_PROVIDER` and does not replace The Odds API or TheRundown. Configure a free key to enable it. `LIMITS_SPORTS` can restrict the monitored leagues; when blank, it follows `ODDS_SPORTS`. `LIMITS_REFRESH_INTERVAL_SECONDS` has a four-hour minimum to protect the free allowance. On first startup the monitor takes an initial snapshot, then polls at that interval. The last-attempt timestamp is persisted in SQLite, so container restarts do not trigger extra requests. Manual refresh is subject to the same quota-safe interval; the dashboard shows the remaining cooldown and disables the button until the next permitted poll. Each poll consumes one request per monitored league.

Only provider values explicitly typed as `posted_limit` are stored. Exchange resting depth and contract counts are excluded. Circa is the primary documented source for posted limits; Pinnacle is displayed only if the provider returns a posted-limit field for it. Missing limits are not inferred. Values are in each sportsbook's own currency and are public market maximums, not account-specific betting limits. A limit-change alert requires two observed values for the same event, book, market, selection and line. Discord alerts report increases by default; set `LIMITS_ALERT_DECREASES=true` to include decreases. Free-tier access and coverage are subject to OddsLink's current terms and allowance.

## Synthetic -1 line builder

The calculator can estimate a fair -1 run-line / puck-line price when a sportsbook only offers the moneyline and -1.5 / +1.5 handicap. Enter both sides of each two-way market for the same event and selected team. It removes vig from the moneyline and handicap markets separately, estimates the one-run/one-goal push probability as fair moneyline win probability minus fair -1.5 cover probability, and derives the fair -1 odds from cover-versus-loss probabilities while excluding one-run/one-goal pushes (which return the stake). Inconsistent market inputs are rejected. This is a mathematical estimate, not a live sportsbook quote; use prices from the same book and time, and verify market-period and overtime/shootout settlement rules.

## Selected-game Monte Carlo simulator

The simulator is an on-demand, single-game tool. Select a configured league, choose one game from the latest locally stored odds snapshot, then click Run Simulation. Its league selector follows the same configured league catalog as the rest of the platform; college football and college basketball are included in the default coverage. The backend executes exactly 10,000 trials for that event; it does not loop over the league or request fresh provider data. A game is selectable only when current spread and total markets are available.

The simulator remains market-informed: the latest archived sportsbook spread and total anchor expected margin and scoring level. It supports optional empirically fitted league parameters loaded from `SIMULATION_MODEL_PATH`; without a model file it clearly falls back to broad league-family priors. The training workflow estimates league-specific market-residual bias, scoring volatility and correlation for basketball/football, plus Gamma-Poisson overdispersion for low-scoring leagues. Separate later calibration data can fit per-league multiclass temperature scaling for game outcomes (home/away/tie) and totals (over/under/push). Basketball, NHL, MLB and NCAAF ties use sport-specific overtime/extra-inning approximations; soccer draws remain valid and NFL regular-season ties may remain. These resolution rules are explicit approximations, not full period-by-period rule models. An optional integer `seed` makes the 10,000-trial run reproducible.

A paper-only historical replay workflow reconstructs each forecast from the latest archived quote snapshot strictly before kickoff, matches verified final-score rows, and reports win/tie and totals Brier scores, log loss, class-specific calibration, projected-score MAE, total MAE and margin MAE. It requires separate chronological training, calibration and untouched holdout datasets, rejects event overlap and invalid time ordering, and compares fitted predictions with an unfitted broad-prior baseline on the same holdout events. The report includes paired event-bootstrap 95% uncertainty intervals overall and by league, and only labels a league evaluation-ready when its own holdout reaches the minimum sample threshold. The replay makes no provider requests and does not modify the quote database. See [docs/SIMULATOR_VALIDATION.md](docs/SIMULATOR_VALIDATION.md) for CSV requirements, command, thresholds and interpretation.

**Important validation status:** the implementation and automated tests are in place, but no licensed historical settlement dataset is bundled and no real-world empirical fit or holdout performance is claimed. You must supply verified results that overlap your archived pregame quote history. Until a sufficiently large untouched holdout demonstrates reliable calibration and lower forecast error, treat all outputs as market-informed scenario estimates—not validated independent predictions, betting advice or evidence of a profitable edge. The simulator itself uses saved odds only and consumes no additional provider quota. Separately, an optional TheRundown score-only collector can retrieve final results for events already present in the local archive; it is disabled by default and consumes provider data points when enabled.

## Optional player props

Player props are disabled by default. The Odds API requires its event-odds endpoint for non-featured markets, which means a separate request for each selected event; quota cost depends on the unique markets returned and requested bookmaker regions. The current implementation caps each poll at five events and enforces a four-hour minimum interval. Set PLAYER_PROPS_ENABLED=true, choose one PLAYER_PROPS_SPORT and specify supported comma-separated PLAYER_PROPS_MARKETS only after checking your provider plan and remaining credits. Example NBA markets are player_points,player_rebounds,player_assists; NFL examples include player_pass_yds,player_rush_yds,player_reception_yds. Market availability varies by sport, bookmaker and event. Player names are retained separately from Over/Under outcomes to prevent cross-player comparisons. TheRundown adapter currently provides main markets only; player props are not requested when TheRundown is the active provider. The CLV tracker intentionally excludes player props until player-specific CLV storage and close validation are added.

## Closing-line value tracking

The CLV tracker stores the first positive-EV observation for each exact event, market, selection, line and sportsbook during odds refresh. It then waits until the event start time and compares the captured American price with the last pregame no-vig consensus price from at least two other sportsbooks offering the same complete outcome/line set. Positive CLV means the entry price was longer than the fair consensus closing price. `CLV_MIN_EV_PERCENT` controls the minimum EV threshold used to begin tracking (default `1.0`%). The first refresh after installing this version begins the dataset; older snapshots are not retroactively treated as entries. CLV is a measure of price movement relative to the closing consensus—not a win rate, realized profit, or proof of model accuracy. Events with incomplete closing coverage remain unscored.

## Controlled testing

See [docs/TESTING_PLAN.md](docs/TESTING_PLAN.md) for the test stages, spot-check method, alert-delivery checks and pass criteria. This is an observational, paper-only validation phase; EV and arbitrage estimates are not guarantees.

## Quick start

```bash
cp .env.example .env
# Set ODDS_PROVIDER=the_odds_api and THE_ODDS_API_KEY in .env.
# Preserve the API key exactly; keys are case-sensitive.
docker compose pull
docker compose up -d
```

Open http://localhost:8765. SQLite data persists in `./data/odds_history.sqlite3` through the Compose data volume.

Local install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Set `DATABASE_PATH` to change the SQLite file location. `ODDS_DATA_STALE_AFTER_SECONDS` controls when the health endpoint and dashboard mark a sport's last saved snapshot as stale (default six hours); freshness is reported per configured sport so one recently refreshed league cannot hide another league's stale feed. For TheRundown Free, set `ODDS_PROVIDER=therundown` and `THERUNDOWN_API_KEY` in `.env`; the key is sent in the `X-TheRundown-Key` header and is preserved exactly. The current Free allowance is 20,000 data points per UTC day and up to 200,000 per UTC calendar month, with a five-minute delay and pre-match main-line odds from three books. Do not assume a fixed sportsbook list across every sport: confirm availability using the provider's current affiliate catalog and account entitlements. Each returned price row consumes a data point; event, score and live-state rows can also be billed. `X-Datapoints` reports the authoritative cost of a response, while the `X-Datapoints-*` headers report quota usage. The app defaults to one refresh every four hours (`RUNDOWN_REFRESH_INTERVAL_SECONDS=14400`) to limit quota consumption. This provider-specific interval takes precedence over the Discord refresh interval, so enabling Discord does not force TheRundown to refresh every five minutes. Increase polling only after reviewing usage in your provider dashboard. `RUNDOWN_UTC_OFFSET_MINUTES` controls the event-date offset (default `0`). For The Odds API, set `ODDS_REFRESH_INTERVAL_SECONDS` to a positive number of seconds for general automatic refresh; with Discord enabled and no explicit interval, the app uses `DISCORD_ALERT_REFRESH_INTERVAL_SECONDS`. Automatic refresh starts only when a provider and API key are configured. To enable Discord alerts, set `DISCORD_WEBHOOK_URL`, `ALERT_MIN_EV_PERCENT` and `ALERT_MIN_ARB_PERCENT`. The health endpoint reports the last alert check, evaluated candidates, qualifying candidates and delivery errors. Alerts are disabled when no webhook URL is set. Alert fingerprints are stored in SQLite to prevent repeated notifications for the same exact quote; a changed price creates a new alert.

## Provider configuration

The default provider is disabled. To use The Odds API, configure:

```dotenv
ODDS_PROVIDER=the_odds_api
THE_ODDS_API_KEY=your_exact_case_sensitive_key
ODDS_REGIONS=us
# Optional: exact comma-separated bookmaker keys; overrides regions when set.
ODDS_BOOKMAKERS=
ODDS_MARKETS=h2h,spreads,totals
ODDS_FORMAT=american
```

Available sports are controlled by `ODDS_SPORTS`. Provider coverage, quota, historical access, market availability and permitted use depend on the provider's current plan and terms. Set ODDS_REGIONS=us,us2 to include the additional-US bookmaker group. Alternatively, set ODDS_BOOKMAKERS to comma-separated provider bookmaker keys; this takes precedence over regions. Usage is charged by market and region, while explicit bookmaker selection is billed in groups of up to 10 bookmakers per region-equivalent. Wider coverage can increase usage, and some books are plan-restricted or unavailable for certain sports.

## API

- `GET /api/health` — provider configuration and stored-data status.
- `GET /api/odds?sport=...` — latest stored event quotes.
- `GET /api/simulation/leagues` — configured leagues available to the game simulator.
- `GET /api/simulation/games?sport=...` — games in one league's latest stored snapshot.
- `POST /api/simulation/run?sport=...&event_id=...&seed=12345` — run exactly 10,000 trials for the selected game only; optional integer seed reproduces the same trial outcomes.
- `POST /api/refresh?sport=...` — request a provider refresh.
- `GET /api/market-board?sport=...` — best price for each exact market / outcome / line.
- `GET /api/opportunities?sport=...&min_ev=1.0&min_consensus_books=2` — estimated EV opportunities.
- `GET /api/arbitrage?sport=...` — two-outcome cross-book arbitrage candidates.
- `GET /api/history?event_id=...&market=...&outcome=...&bookmaker=...&point=...` — stored quote history.
- `GET /api/movement?sport=...` — consolidated opening-to-latest price and line movement summaries.
- `GET /api/limits?sport=...` — latest verified posted market limits and feed status.
- `GET /api/limits/changes?sport=...&direction=increased|decreased|all` — detected changes between successive stored limit snapshots, including old/new values and percentage change.
- `GET /api/limits/history?event_id=...&bookmaker=...&market=...&outcome=...` — observed limit history.
- `POST /api/limits/refresh` — manually refresh the optional limits feed.
- `GET /api/movement/history?event_id=...&market=...&outcome=...&bookmaker=...` — chronological price and line observations for one sportsbook selection.

## Commercial licensing gate

Before enabling public subscriber access, confirm that the selected odds provider contract explicitly permits customer-facing commercial display and the intended redistribution. A paid plan is not automatically a display license. TheRundown's current plan guidance lists Starter for internal/personal use and Pro or above for customer-facing display.

## Development roadmap

### Completed foundations
1. ~~Persistent SQLite quote and event storage.~~
2. ~~Exact-line best-price comparison, multi-market EV screening and cross-book arbitrage scanning.~~
3. ~~Consolidated line movement, quote timelines and prospective CLV capture.~~
4. ~~On-demand 10,000-trial, single-game simulator with deterministic seed support and sport-specific tie/overtime resolution approximations.~~
5. ~~Simulator historical-replay foundation: verified settlement CSV import, strictly pregame archived-quote reconstruction, chronological train/calibration/holdout separation, per-league scoring-parameter fitting, separate outcome/total probability calibration, holdout scoring and same-event broad-prior baseline comparison.~~
6. ~~Automated simulator validation suite, documented paper-only backtest workflow, paired holdout uncertainty reporting and optional TheRundown final-score collection for locally archived events.~~

### Active simulator validation — not yet a validated predictive model
7. Collect authorized, trustworthy final results that overlap the locally archived pregame odds snapshots for each supported league.
8. Run the three-stage workflow league by league; inspect sample sizes, score distributions, calibration buckets, Brier/log-loss and score-error deltas on untouched holdouts.
9. Improve sport-specific scoring and overtime/extra-inning assumptions using historical evidence; add league-specific model components only when sample sizes justify them.
10. Add stronger independent market benchmarks and rolling out-of-time evaluation. Keep fitted parameters disabled for any league that fails minimum sample, calibration or holdout quality gates.

### Remaining platform work
11. Optional player props are implemented for The Odds API; expand provider coverage and add player-specific CLV only after identity and settlement validation.
12. Harden the core with deeper operational monitoring, retention/backup guidance and additional provider diagnostics.
13. Add application authentication, subscriber access tiers and recurring billing before any public commercial launch; add further licensed providers and injury/news feeds only where reliable data is available.

A successful CI run confirms code and test behavior; it does not replace real historical validation. No simulator image is considered ready for deployment solely because its automated tests pass.

## License

MIT. Third-party odds data remains subject to each provider's terms and licenses.


Historical simulator evaluation is available as a paper-only local replay. It uses verified final-score CSV imports and only archived pregame quote snapshots; it performs no provider requests. See [Simulator historical validation](docs/SIMULATOR_VALIDATION.md) for the required CSV format, command and interpretation limits. This does not yet make the simulator a validated independent predictive model.


When TheRundown is the active odds provider, an optional score-only results collector can retrieve final scores for games already present in the local odds archive. It uses `affiliate_ids=0`, validates event identity against stored quotes, and is disabled by default to protect the provider data-point allowance. Enable it with `SIMULATION_RESULTS_SYNC_ENABLED=true` in `.env`. Stored results can be split chronologically and backtested with `python -m scripts.backtest_simulator --use-stored-results --training-cutoff YYYY-MM-DD --calibration-cutoff YYYY-MM-DD`. See [Simulator historical validation](docs/SIMULATOR_VALIDATION.md).
