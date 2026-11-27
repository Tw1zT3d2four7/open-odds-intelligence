# Open Odds Intelligence — Controlled Testing Plan

## Purpose

This phase validates data quality, market calculations, alert delivery and operational stability. It is **not** a claim that the displayed opportunities are profitable, and it is not an automated betting system. Do not place real-money wagers based only on an alert.

## Current test scope

- Live odds from one or more configured providers (The Odds API, TheRundown, Oddize, and OddsLink), limited to configured sports, regions, supported markets, account quotas, and provider licensing.
- Moneyline, spreads and totals where complete market quotes are supplied.
- Best available prices, leave-one-book-out no-vig EV estimates and two-outcome cross-book arbitrage calculations.
- SQLite quote history and basic price/line movement.
- Optional OddsLink posted-limit snapshots, limit-change history and Discord increase alerts.
- First-qualified-quote CLV tracking against a pregame consensus close.
- Discord alerts for qualifying EV and arbitrage candidates.
- Manual provider refresh, independent multi-provider polling, source-attributed quote merging, account-group quota protection, and Discord alert evaluation against locally stored quotes.
- Subscriber/admin identity routes in opt-in `AUTH_MODE=identity`: registration, verification, recovery, session separation, MFA, CSRF, rate limits, route policy, entitlements, subscriber lifecycle, and audit behavior.
- Encrypted transactional email queue/worker, identity backup/restore tooling, billing persistence, and guarded Stripe test-mode routes.

Full historical strategy backtesting and injury/news context are not yet complete. Subscriber authentication, billing persistence, and test-mode billing paths are included in automated CI, but public subscriber launch and live billing remain disabled pending staging acceptance and explicit policy approval. The default runtime remains `legacy_basic`; identity mode is an opt-in staged test mode. Player props remain an optional, provider-quota-sensitive extension.

## Calculator validation

The calculators are local math tools and must not trigger provider requests. Verify the American/Decimal toggle converts every odds input without changing its underlying value. Spot-check payout, parlay, two-way no-vig, EV/Kelly, hedge and -1 settlement results against independent calculations.

For the synthetic -1 builder, use both sides of the selected team's moneyline and -1.5 market from the same event, sportsbook and settlement period. Confirm the de-vigged cover, push and loss probabilities sum to 100%. The fair decimal price must satisfy:

`fair decimal odds = 1 + P(loss) / P(cover)`

The one-run/one-goal push returns the stake and is not counted as a loss. Confirm inconsistent probabilities are rejected. The displayed price is a fair estimate, not a quoted sportsbook price.

## Test stages

### 1. Data-feed integrity

For each configured sport:

- Confirm the sport appears in the dashboard and `GET /api/health`.
- Refresh and confirm event counts are plausible for the current slate.
- Open the best-price board and verify that events, start times, sportsbook names, markets, lines and American prices match the provider's current feed.
- Confirm unsupported or unavailable markets are absent rather than filled with synthetic data.
- Check the provider quota fields after refresh. Avoid frequent repeated refreshes during the test; each sport/region/market request may consume quota.

**Pass criteria:** no malformed prices, missing event identity, incorrect line association or unexplained stale feed. Record sports/markets with thin sportsbook coverage.

### 2. EV calculation spot checks

For a candidate with at least three books:

1. Select the same event, market, outcome and exact line at the target book and at least two other books.
2. Convert each peer book's complete market to no-vig probabilities.
3. Compare the median peer probability with the target price.
4. Verify the displayed estimated EV is approximately:
   `(fair probability × target decimal odds − 1) × 100`.

The target sportsbook must not be included in its own fair-probability estimate. Do not compare different totals or alternate spread lines.

**Pass criteria:** independently checked examples agree with the dashboard to rounding precision. Flag large differences for investigation before trusting the feed.

### 3. Discord delivery

- Confirm `alerts_enabled` is true in `GET /api/health`.
- Click **Refresh odds** when the dashboard shows a qualifying candidate.
- Verify the refresh result reports evaluated, qualified and sent counts.
- With TheRundown configured for a four-hour poll and Discord alerts configured for five minutes, verify alert checks run every five minutes against stored quotes while provider requests remain on the four-hour schedule.
- Verify the Discord alert loop does not call the odds provider, and that an alert check overlapping a refresh-triggered alert check is skipped rather than run concurrently.
- Verify value and arbitrage notifications arrive as separate rich embeds, not long unformatted text.
- For value alerts, verify matchup, sport/market, selection, sportsbook, American odds, fair probability, EV, consensus count and localized event time are readable.
- For arbitrage alerts, verify each leg has its own clearly labelled sportsbook, selection, odds and suggested stake split.
- For market-limit alerts, verify direction, sportsbook, market, selection, previous/new posted limit, change amount/percentage and localized event time.
- Confirm embed footers explain the estimates/provider-reported nature, and no unintended user or role mentions are triggered.
- Refresh again without changing quotes. The same exact candidate should not be repeatedly spammed.
- When a quote changes and remains qualified, verify the changed quote can produce a new alert.
- Review `last_alert_error` if a delivery fails. A failed candidate should remain retryable; one failed delivery should not prevent other candidates from being attempted.

**Pass criteria:** manual and scheduled checks deliver qualifying alerts; duplicate suppression works; failures are visible and retryable.

### 4. History and movement

- Refresh the same sport at different times.
- Open quote history from the best-price board.
- Confirm the history reflects successive stored observations.
- Review line movement and confirm it is described only as a market-movement signal, not proof of sharp action.

**Pass criteria:** historical observations persist across container restarts and the displayed quote history matches the saved feed observations.

### 5. Posted market-limit feed

- Confirm the Market Limits status shows the OddsLink connection, remaining request allowance, configured sports and last-check time.
- Confirm returned rows are explicitly marked by the provider as `posted_limit`; exchange `resting_depth`, missing sizes and inferred values must not appear.
- Verify the displayed bookmaker, event, market, outcome, line, posted amount and odds against the provider response.
- Do not treat a single snapshot as evidence of a limit increase. Allow at least two scheduled snapshots of the same event/book/market/selection/line before expecting a change record.
- Open a detected change and verify the complete chronological limit history, previous/current amounts, absolute delta and percentage delta.
- Confirm unchanged snapshots do not create change records or repeated Discord alerts. Verify increases alert once per distinct transition; decreases remain disabled unless explicitly enabled.
- Record quota before and after a refresh. Each monitored sport consumes a provider request; automatic and manual polls share the persisted four-hour minimum. Restart the container during the cooldown and confirm no additional provider request occurs.
- Treat all timestamps as application observation times unless the provider explicitly supplies a source timestamp. Values are in the sportsbook's reported currency, not necessarily USD or an account-specific betting limit.

**Pass criteria:** only explicit provider-posted limits are displayed; successive snapshots produce accurate changes; history persists across restarts; unchanged values do not generate false changes; quota use stays within the account allowance.

### 6. Closing-line value (CLV)

- Install the CLV-enabled version and refresh odds while upcoming events have positive-EV candidates.
- Confirm the tracker records the first qualifying price once per exact event, market, outcome, line and sportsbook; later refreshes must not overwrite that entry.
- Confirm events that have not started remain pending and no post-start quotes are considered closing prices.
- After an event starts, verify the close uses the latest pregame snapshot strictly later than the entry observation and within `CLV_MAX_CLOSE_AGE_MINUTES` (default 120 minutes), with at least two other books offering the same complete outcome/line set. The snapshot that created the entry must never also serve as its close.
- Independently calculate the peer books' no-vig probabilities and median; verify the closing fair probability, fair American price and CLV calculation.
- Confirm positive CLV means the captured entry price beat the fair closing consensus price. It does not mean the wager won or that the model is profitable.
- Confirm events without two complete peer books are marked insufficient rather than assigned a fabricated close.
- Confirm the CLV history survives container restarts and new entries begin only after this version is installed.

**Pass criteria:** immutable entry quotes, fresh strictly pregame closes, exact-line and complete-market matching, reproducible consensus calculations, and clear pending/insufficient states.

### 7. Optional player props

- Keep PLAYER_PROPS_ENABLED=false unless The Odds API is the active provider and the account has enough quota for per-event requests.
- For a controlled test, select one supported sport and a small list of market keys; leave the five-event cap and four-hour minimum in place.
- Verify the feed uses the event-odds endpoint, preserves player descriptions, and never merges two players with the same Over/Under side and line.
- Compare each player, market, side, exact line, sportsbook and price against the provider response.
- Confirm the player-props refresh interval is independent of the main odds/Discord refresh, its last-attempt timestamp survives container restarts, and no requests occur while disabled.
- Confirm quota headers are updated after prop calls and the monitor stops further requests on authorization/quota errors.
- Do not expect player props from the TheRundown Free adapter, which is scoped to main markets.

**Pass criteria:** opt-in only, event-specific market identity preserved, request count capped, polling throttled, provider quota visible and no fabricated props.

### Provider failure and quota recovery

Run these checks with mocked provider responses; do not use a live key to simulate failures.

- A 401 or 403 is surfaced as a credential/permission failure and is not retried.
- A 429 is surfaced with any available quota and `Retry-After` metadata, and the refresh stops without repeating that provider request or advancing to additional sports in the same run.
- A transient 408, 425, 500, 502, 503, 504 or transport failure may be retried only within the configured `PROVIDER_RETRY_ATTEMPTS` limit.
- Retry delays use bounded exponential backoff. A numeric or HTTP-date `Retry-After` is never shortened; when it exceeds `PROVIDER_RETRY_MAX_DELAY_SECONDS`, the request is deferred to a later refresh.
- Verify quota headers returned with an HTTP error remain visible in provider diagnostics after the refresh fails.
- Confirm a transient retry does not create a snapshot until a successful provider response is received, and a repeated manual refresh cannot run concurrently with the in-flight request.
- Confirm retry settings are read from the private `.env`; leave the defaults in place unless the provider's rate/usage terms support a change.

**Pass criteria:** no retries for credentials or quota exhaustion, no retry before a provider-specified deadline, bounded attempts and waits, visible quota diagnostics, and no duplicate/in-flight refresh amplification.

## 8. Stability and operational checks

- Leave the service running through multiple scheduled refresh intervals.
- Confirm the container remains healthy and `GET /api/health` remains responsive.
- Watch provider quota consumption and the `last_error` / `alert_error` fields.
- Confirm data remains present after a container rebuild; the Compose data volume must remain mounted.
- Back up `data/odds_history.sqlite3` before any database maintenance.

**Pass criteria:** no unhandled crash, lost history, runaway refresh loop or unexplained provider quota use during a multi-day observation period.

## Test log fields

For each reviewed candidate, record:

- Observation timestamp and sport
- Event and start time
- Market, outcome and exact line
- Target sportsbook and American price
- Peer sportsbooks used for consensus
- Fair probability and estimated EV
- Whether a Discord alert was sent
- Whether the candidate was a duplicate, changed quote or delivery retry
- Any data or calculation discrepancy

## Authentication and deployment safety

- Leave both `AUTH_USERNAME` and `AUTH_PASSWORD` blank only on a trusted private development network.
- Configure both values in the private server-side `.env` file and restart the application. Never commit credentials or paste them into logs/issues.
- Confirm an unauthenticated request to `/`, `/api/odds` and `/api/refresh` returns HTTP 401.
- Confirm the correct case-sensitive Basic credentials allow the dashboard and API, and a wrong password is rejected.
- Confirm configuring only one credential fails closed with HTTP 503.
- Confirm `/api/health` remains reachable for the Docker health check. Do not expose detailed operational health publicly without a reverse-proxy/network policy.
- Use HTTPS whenever credentials cross an untrusted network. Basic authentication over plain HTTP is not suitable for public internet access.
- Before remote exposure, place the service behind a TLS-terminating reverse proxy, restrict inbound access, and verify that the proxy does not log Authorization headers.
- Anyone who can reach an unprotected refresh endpoint can consume the configured odds-provider quota.

## Important interpretation limits

- EV is an estimate derived from currently available sportsbook quotes; it is not a verified win probability.
- A consensus of two other books is a minimum data threshold, not a guarantee of market quality.
- Arbitrage calculations assume both quoted prices remain available and that the books settle the market consistently. Limits, void rules, account restrictions and price movement can invalidate the theoretical return.
- Line movement alone does not establish sharp-money activity.
- CLV is now measured prospectively when sufficient pregame snapshots exist; historical profitability and settlement results are not yet measured. Do not infer either from a positive EV alert.
- Posted-limit changes show only what the feed reported between observations. They do not establish sharp action, actual available customer limits, or the reason a sportsbook changed its limit.
- Keep testing observational and paper-only until calculation accuracy, data coverage and a separate performance study are complete.
