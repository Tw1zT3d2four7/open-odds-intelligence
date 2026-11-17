# Open Odds Intelligence — Controlled Testing Plan

## Purpose

This phase validates data quality, market calculations, alert delivery and operational stability. It is **not** a claim that the displayed opportunities are profitable, and it is not an automated betting system. Do not place real-money wagers based only on an alert.

## Current test scope

- Live odds from the configured The Odds API account, limited to the sports, regions and markets enabled in `.env`.
- Moneyline, spreads and totals where complete market quotes are supplied.
- Best available prices, leave-one-book-out no-vig EV estimates and two-outcome cross-book arbitrage calculations.
- SQLite quote history and basic price/line movement.
- Discord alerts for qualifying EV and arbitrage candidates.
- Manual refresh and Discord-enabled scheduled refresh.

Player props, closing-line value (CLV), historical strategy backtesting, injury/news context, authentication and multi-provider comparison are not yet part of this test scope.

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
- Verify the Discord message identifies the event, market, selection, sportsbook, price and estimated EV or arbitrage percentage.
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

### 5. Stability and operational checks

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

## Deployment safety

The current dashboard and API do not implement user authentication. Keep the test instance on a trusted private network. Do not port-forward it or expose it through a public reverse proxy until access control is added at the proxy or application layer. Anyone who can reach the refresh endpoint can consume the configured odds-provider quota.

## Important interpretation limits

- EV is an estimate derived from currently available sportsbook quotes; it is not a verified win probability.
- A consensus of two other books is a minimum data threshold, not a guarantee of market quality.
- Arbitrage calculations assume both quoted prices remain available and that the books settle the market consistently. Limits, void rules, account restrictions and price movement can invalidate the theoretical return.
- Line movement alone does not establish sharp-money activity.
- CLV and historical profitability are not yet measured. Do not infer either from a positive EV alert.
- Keep testing observational and paper-only until calculation accuracy, data coverage and a separate performance study are complete.
