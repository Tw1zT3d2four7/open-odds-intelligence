# Simulator historical validation

## What the backtest does

The backtest is paper-only and replays the application's own immutable SQLite quote archive. For each imported final-score row it:

1. Finds the latest archived snapshot for the same event and league whose observation timestamp is strictly earlier than the event start.
2. Reconstructs the event markets from that snapshot only.
3. Runs the same fixed 10,000-trial simulator with a deterministic event-specific seed.
4. Scores the forecast against the imported final result.
5. Reports Brier score, log loss, calibration buckets, projected home/away score MAE, total MAE and margin MAE.

It never requests odds from a provider, never uses a current quote as a historical quote, and never uses a post-start snapshot as a pregame input. Events without a valid pregame spread and total, matching team identities, and a final settlement are skipped with an explicit reason.

## Interactive 10,000-trial simulation

The dashboard's single-game simulator uses only the latest locally stored odds snapshot for the selected league. A game must have a complete, internally consistent sportsbook spread and total, and that league's snapshot must be marked current by the application's freshness monitor. The game selector shows the freshness state and age; stale, unavailable, or unknown snapshots are marked not ready, and the run endpoint independently rejects them with HTTP 409.

Running the 10,000 trials makes no provider/API request. If the selected league's odds are stale or unavailable, use **Load / Refresh This League** to request only that league, then reload its game list. The default stale-data threshold is six hours (`ODDS_DATA_STALE_AFTER_SECONDS=21600`, with a five-minute minimum). The threshold measures the age of the stored snapshot, not the age of each individual sportsbook quote.

This guard prevents old stored lines from being silently presented as current. It does not make a market price live or guarantee that a sportsbook still offers the displayed line; verify availability and settlement rules independently.

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

## Train, calibrate and test chronologically

For a proper three-stage evaluation, keep all three datasets separate and ordered within each league:

1. Training results fit the league-specific scoring residual parameters.
2. A later calibration set fits separate multiclass temperatures per league for game outcomes (home/away/tie) and totals (over/under/push).
3. A still later holdout set evaluates the fitted model and calibration.

Example:

```bash
python -m scripts.backtest_simulator --training-settlements training.csv --calibration-settlements calibration.csv --settlements holdout.csv --model-output ./data/simulation_models.json --output simulator-backtest.json
```

The CLI rejects overlapping event IDs and any per-league time split where training is not strictly earlier than calibration or calibration is not strictly earlier than holdout. Default minimums are 50 matched training events and 100 calibration events per league. Leagues below either minimum do not receive the corresponding fitted parameters.

The training stage estimates per-league market residual bias, margin/total volatility and their correlation for basketball/football; low-scoring leagues additionally estimate team-specific Gamma-Poisson overdispersion and a shared Gamma scoring-environment factor for positive home/away score covariance. The calibration stage fits separate multiclass temperature scaling for win/tie probabilities and over/under/push probabilities against later calibration results. The holdout report compares calibrated/fitted predictions against the same-event unfitted broad-prior baseline. When complete h2h markets were archived, it also reports a direct sportsbook moneyline benchmark built from the median bookmaker-level no-vig probabilities in that same pregame snapshot. Two-way markets are scored only on non-tied finals; three-way markets score home/away/draw. The report compares simulator and market scores on identical events with paired event-bootstrap intervals. This benchmark is descriptive of the archived market snapshot and is not an independent forecast. It now scores spread-cover outcomes (home cover, away cover, push) with Brier score and log loss, includes those metrics in paired model-versus-baseline uncertainty, and fits a separate spread-probability temperature on the calibration period. The interactive game simulator also reports home/away spread-cover frequencies and pushes against the median complete archived home spread; this is a scenario probability based on simulated final scores, not a sportsbook quote or recommendation. It exposes empirical P10/P50/P90 and standard deviation summaries for home score, away score, final margin and game total. P10–P90 describes the middle 80% of simulated outcomes; it is not a confidence interval for the true result.

To train scoring parameters without a separate probability calibration set, omit --calibration-settlements; probabilities then remain uncalibrated.

Set SIMULATION_MODEL_PATH=./data/simulation_models.json in .env (this is the default). The live simulator loads a league's fitted scoring model and/or probability temperature only when a valid model file exists; otherwise it reports broad-prior behavior. The model file belongs in the persistent data/ volume and is not baked into the container image.

The command reads the existing DATABASE_PATH SQLite archive. It does not modify the database or call an odds provider.

## Interpreting results

- Brier score and log loss are proper scoring rules for simulated home/away/tie outcomes and over/under/push totals. Lower is better.
- Calibration buckets compare predicted and observed rates separately for home/away/tie and over/under/push. Always inspect bucket sample sizes.
- Score, total and margin MAE measure forecast error in points/goals/runs.
- Reports are broken out by league and include the number of events scored and skipped. Each league receives its own sample-size readiness status; the combined report remains descriptive unless every included league reaches the 200-game threshold.
- Paired bootstrap intervals are reported overall and separately by league when multiple leagues are present. This prevents a large league's sample from masking a thin league's uncertainty.
- Fewer than 200 scored events are labelled `DESCRIPTIVE_ONLY`; even larger samples are not proof of profitability or future performance.
- Use chronological development and untouched holdout periods. Do not tune volatility or distributions on the same games used for final evaluation.
- The report's model-minus-baseline deltas are fitted-model metric minus unfitted-prior metric; negative values are better for Brier, log loss and MAE.
- The paired uncertainty section resamples the same event-level fitted/baseline pairs and reports 95% bootstrap intervals for each metric delta. Negative deltas favor the fitted model. A one-event sample has no interval; small samples remain descriptive. These intervals describe sampling uncertainty only and do not fix archive-selection bias or establish future performance.
- These metrics evaluate the current market-informed simulator against outcomes. They do not establish an independent predictive edge over the sportsbook market.

## Current limitations

This is a model-development and evaluation foundation, not proof of a validated predictive model. League-specific fitted scoring parameters and separate outcome/total probability temperatures are supported, but they only exist for leagues with sufficient supplied training and calibration rows. The live simulator falls back to broad priors for leagues without eligible fitted parameters. Sport-specific overtime and extra-inning handling is approximate and does not yet model every period, possession, lineup or competition-specific rule.

A valid backtest requires overlap between the local odds archive and imported settlement IDs. The application does not contain a bundled, licensed historical results dataset; the operator must supply verified results and retain enough archived pregame quotes. Empty or thin archives correctly produce skipped events or inconclusive reports. Passing automated tests does not establish predictive accuracy. Do not describe a league as calibrated or validated until its untouched holdout has adequate sample size, acceptable calibration and forecast errors, and performance that is robust across multiple chronological windows.

## Sport-specific resolution currently implemented

The simulator now resolves regulation ties for NBA/WNBA/NCAAB with repeated overtime scoring periods, NHL with overtime-goal/shootout resolution, MLB with repeated extra innings, and NCAAF with repeated overtime possessions. EPL soccer draws remain valid. NFL regular-season ties can remain tied after a simplified overtime period. The NFL overtime tie branch uses a 6% conditional tie prior rather than independently drawing both teams' overtime points (which had implied a 40.5% conditional tie rate). The 6% prior reflects the approximate 2012–2024 regular-season rate (12 ties in 200 overtime games); the 2025 rule change is newer and its early sample is small, so this is a transparent approximation pending a larger rule-era dataset. These are transparent approximations, not official league rulebooks or period-by-period team models; NFL postseason rules and competition-specific tournament formats are not inferred from the event feed. The output exposes regulation tie and overtime/extra-inning counts. Because sportsbook game totals normally include overtime/extra innings, each run estimates the expected extra points added after regulation from a deterministic 10,000-trial pilot and lowers the regulation scoring anchor by that amount before the final 10,000 trials. This avoids adding overtime on top of a full-game market total; the reported `expected_overtime_total_adjustment` and `regulation_total_anchor` make the correction inspectable. It remains a simulation-based approximation and must be checked against verified historical results.

The holdout report also scores the same events with the unfitted broad-prior market-anchored simulator and reports paired metric deltas. This is a baseline comparison, not a sportsbook closing-line or independently generated market-probability benchmark; interpret it accordingly.


## Automatic TheRundown score collection

When TheRundown is the active odds provider and THERUNDOWN_API_KEY is configured, score-only collection is optional and disabled by default to protect the provider data-point allowance. Enable it explicitly with SIMULATION_RESULTS_SYNC_ENABLED=true in .env. It queries only sport/date combinations already present in the local odds archive and containing games that started within the configured lookback window.

- Requests use affiliate_ids=0, so sportsbook price rows are not requested.
- The adapter accepts only explicit STATUS_FINAL records with numeric final scores.
- Before storage, the database verifies that the event ID, scheduled start time, and home/away team identities match the archived odds event.
- Results are stored once in the simulation_settlements SQLite table; duplicate finals are ignored.
- The collector shares the odds-refresh lock to respect TheRundown's one-request-per-second limit.
- Defaults: four-hour synchronization cadence, four-hour retry interval, seven-day lookback, and at most ten sport/date groups per run.
- The health endpoint and dashboard notice show whether collection is enabled, how many results are stored, and the latest inserted count/errors.

To use automatically stored results, choose exclusive date cutoffs. Games before the training cutoff fit scoring parameters; games between cutoffs fit probability temperatures; later games are the untouched holdout:

```bash
python -m scripts.backtest_simulator --use-stored-results --training-cutoff 2026-09-01 --calibration-cutoff 2026-10-01 --model-output ./data/simulation_models.json --output simulator-backtest.json
```

The automatic collector runs only with TheRundown as the active odds provider. Other providers continue to use the verified CSV import workflow. The collector does not backfill events absent from the local odds archive.