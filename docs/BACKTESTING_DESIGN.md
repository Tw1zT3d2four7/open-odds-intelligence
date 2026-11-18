# Historical Backtesting — Engineering Design

## Objective

Build a reproducible, paper-only backtesting engine using odds observations collected by this application. The engine must measure settled outcomes without presenting theoretical EV or CLV as realized profitability.

## Non-negotiable data rules

- Replay only immutable quote snapshots actually stored in SQLite. Do not synthesize historical prices or silently backfill observations from current odds.
- Use each snapshot's `fetched_at` as the strategy's information cutoff. A candidate may use only quotes present in that snapshot.
- Exclude events whose `commence_time` is at or before the snapshot timestamp.
- Keep market, selection, player identity (if later supported), and exact line separate. Spread line matching may normalize the absolute handicap magnitude, but never merge Home and Away selections.
- Never use post-start quotes as pregame entries or closing prices.
- Store entry observations once. A later refresh must not rewrite an earlier entry.
- Do not call a provider's historical endpoint during routine backtests. Backtests over the local archive should make zero odds-provider requests.

## Data required

### Existing quote archive

The current `snapshots` and `quotes` tables retain the observation timestamp, event identity, scheduled start, sportsbook, market, selection, line and price. This is sufficient to replay the quote information available at each saved refresh, subject to the archive's actual coverage and gaps.

### Settlements

A separate, auditable settlement source is required before ROI can be calculated. The first implementation should support importing final event results from a licensed/authorized source or a user-supplied CSV. Each settlement record must include:

- Stable event ID matching the quote archive.
- Final home and away scores.
- Final/cancelled/postponed status.
- Source name and source observation/import timestamp.
- A record identifier or content hash to make imports idempotent.

Do not infer a final result from an event's disappearance, odds movement, or an unverified score. Missing or ambiguous results remain unsettled and are excluded from performance metrics.

## Replay and signal rules

1. Read snapshots in chronological observation order.
2. Reconstruct the event markets exactly as stored in each snapshot.
3. Run the same tested market analysis used by the live dashboard, with strategy parameters frozen for that run (sports, markets, minimum EV, minimum independent consensus books, and any price/odds bounds).
4. Create a candidate only from a pregame snapshot and only when the complete market requirements are met.
5. Apply a deterministic duplicate policy. The initial strategy should take the first qualifying observation per event/market/selection/line and should not count every refresh as a new bet.
6. Record the actual best executable quote available in that snapshot, its sportsbook, American price, fair probability, EV estimate, snapshot ID and timestamp.
7. Do not select strategy thresholds after inspecting the test-period results. Parameter exploration must be separated from final out-of-sample evaluation.

## Settlement rules for initial markets

- Moneyline (`h2h`): settle the selected team or draw against the final result. Sport-specific overtime and draw rules must be explicit; unsupported or ambiguous market rules are excluded.
- Spread: settle the selected team using its quoted handicap and final score difference. Pushes return the stake and count separately.
- Total: settle Over/Under using the final combined score and quoted total. Exact equality is a push.
- Cancelled, abandoned, postponed without a final result, or otherwise void markets do not count as wins or losses. Record them as void.
- Do not include player props until participant identity, player settlement and market-specific rules are independently implemented and tested.

## Metrics

Every report must show the number of eligible signals, settled bets, wins, losses, pushes, voids and unsettled records. Financial metrics should include flat-stake net units, ROI/yield on settled stake, average entry odds, and a clearly defined win-rate denominator. Show CLV separately from realized results.

Break results down by sport, market, strategy configuration and sportsbook where sample size permits. Always show sample size. Add uncertainty intervals before making comparative claims. Small samples must be labelled inconclusive; a positive ROI on a small sample is not evidence of an edge.

## Validation requirements

- Deterministic fixtures for moneyline wins/losses/draws where supported, spread wins/losses/pushes, totals wins/losses/pushes, and voids.
- No look-ahead: changing a later snapshot must not change an earlier signal.
- No post-start quote can become an entry.
- No duplicate refresh can create duplicate bets for the same strategy selection.
- Exact-line and side matching across books, including signed spread conventions.
- Missing/ambiguous settlements are excluded, not guessed.
- Re-running the same backtest over unchanged snapshots, settlements and parameters produces identical results.
- Snapshot replay performs no network/provider requests.

## Walk-forward evaluation

The engine should support chronological training/selection and later test windows. Strategy configuration must be frozen before each test window. Results should be reported by time period and not only as one aggregate. Do not tune thresholds on the final holdout period and then report that same period as validation.

## Delivery sequence

1. Finish CLV and quote-identity correctness tests.
2. Define and test the settlement import format and settlement rules.
3. Implement snapshot replay and deterministic signal capture.
4. Implement settlement and flat-stake performance metrics.
5. Add walk-forward reports and dashboard presentation.
6. Only after paper validation, consider broader strategy research and subscriber-facing reporting.

## Current limitation

Historical backtesting is not yet implemented. The app must first accumulate a sufficiently large prospective quote archive and obtain trustworthy settlement data. A backtest must report archive coverage and missing settlements alongside its results; it must not imply that the current database contains a complete historical sample.
