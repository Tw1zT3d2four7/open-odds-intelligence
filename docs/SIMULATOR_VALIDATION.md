# Simulator historical validation

## What the backtest does

The backtest is paper-only and replays the application's own immutable SQLite quote archive. For each imported final-score row it:

1. Finds the latest archived snapshot for the same event and league whose observation timestamp is strictly earlier than the event start.
2. Reconstructs the event markets from that snapshot only.
3. Runs the same fixed 10,000-trial simulator with a deterministic event-specific seed.
4. Scores the forecast against the imported final result.
5. Reports Brier score, log loss, calibration buckets, projected home/away score MAE, total MAE and margin MAE.

It never requests odds from a provider, never uses a current quote as a historical quote, and never uses a post-start snapshot as a pregame input. Events without a valid pregame spread and total, matching team identities, and a final settlement are skipped with an explicit reason.

## Settlement CSV format

Create a UTF-8 CSV with this exact required header:

```csv
event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source
```

Example fixture only (not real historical data):

```csv
event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source
example-event-1,basketball_nba,2026-01-02T00:00:00Z,Home Club,Away Club,112,105,final,verified-results-provider
```

Rules:
- `event_id` must exactly match the event ID in the local odds archive.
- `sport_key` must match the archived league key.
- `commence_time` must be an ISO-8601 timestamp with timezone.
- Team names must match the archived home/away identities (case and punctuation are ignored; home/away order is not).
- Scores must be finite, non-negative whole numbers.
- Only `status=final` is accepted. Postponed, cancelled, abandoned, live or ambiguous events are not scored.
- `source` is mandatory provenance. Only import results from an authorized, trustworthy source. Do not invent results or infer a final score from odds.

## Train and test chronologically

With the application virtual environment active and its normal .env/database path configured, keep training results earlier than test results:

```bash
python -m scripts.backtest_simulator --training-settlements /path/to/earlier-training-results.csv --settlements /path/to/later-holdout-results.csv --output simulator-backtest.json
```

The default minimum is 50 matched training events per league. A league below that threshold is not assigned fitted parameters and continues to use its broad prior. The training stage estimates per-league market residual bias, margin/total volatility and their correlation for basketball/football; low-scoring leagues additionally estimate home/away overdispersion for the Gamma-Poisson count model.

To export fitted parameters for the live single-game simulator:

```bash
python -m scripts.backtest_simulator --training-settlements /path/to/earlier-training-results.csv --settlements /path/to/later-holdout-results.csv --model-output ./data/simulation_models.json --output simulator-backtest.json
```

Set SIMULATION_MODEL_PATH=./data/simulation_models.json in .env (this is the default). The live simulator loads a league's fitted model only when a valid model file exists; otherwise it reports that it is using the broad league prior. The model file belongs in the persistent data/ volume and is not baked into the container image.

The command reads the existing DATABASE_PATH SQLite archive. It does not modify the database or call an odds provider.

## Interpreting results

- Brier score and log loss are proper scoring rules for the simulated win/tie probabilities. Lower is better.
- Calibration buckets compare average predicted home-win probability with the observed home-win frequency. Always inspect bucket sample sizes.
- Score, total and margin MAE measure forecast error in points/goals/runs.
- Reports are broken out by league and include the number of events scored and skipped.
- Fewer than 200 scored events are labelled `DESCRIPTIVE_ONLY`; even larger samples are not proof of profitability or future performance.
- Use chronological development and untouched holdout periods. Do not tune volatility or distributions on the same games used for final evaluation.
- These metrics evaluate the current market-informed simulator against outcomes. They do not establish an independent predictive edge over the sportsbook market.

## Current limitations

This is the first replay/evaluation foundation, not the final sport-aware model. It does not yet fit empirical sport-specific score distributions, model overtime/extra innings explicitly, or compare against independently constructed market probability baselines. The current simulator still uses broad league-family volatility priors. Do not describe it as calibrated or validated until those components and adequately sized out-of-sample tests pass.

A valid backtest also requires overlap between the local odds archive and imported settlement IDs. The application does not currently contain a bundled, licensed historical results dataset; a CSV must be supplied by the operator. An empty or thin local archive will correctly produce skipped events or an inconclusive report.

## Sport-specific resolution currently implemented

The simulator now resolves regulation ties for NBA/WNBA/NCAAB with repeated overtime scoring periods, NHL with overtime-goal/shootout resolution, MLB with repeated extra innings, and NCAAF with repeated overtime possessions. EPL soccer draws remain valid. NFL regular-season ties can remain tied after a simplified overtime period. These are transparent approximations, not official league rulebooks or period-by-period team models; NFL postseason rules and competition-specific tournament formats are not inferred from the event feed. The output exposes regulation tie and overtime/extra-inning counts. These assumptions still require calibration against verified historical results.
