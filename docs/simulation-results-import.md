## Supplying verified final scores to the simulator (no provider polling)

The simulator intentionally refuses to publish independent forecasts without
historical outcomes. The optional TheRundown score sync remains off unless the
operator explicitly enables it. You can instead seed the local settlement
store from independently verified results.

### 1. Export only event identities already in your odds archive

Run inside the application container (or from the project virtualenv against
the same SQLite database):

```bash
python -m scripts.import_simulation_results --template data/results-template.csv
```

The template contains one row per archived event with the exact event ID,
league key, kickoff timestamp, and home/away labels. It does not invent
historical games or fetch provider data. The archive can only accept scores
for events it already knows about.

### 2. Fill the final-score fields

For each event you can independently verify, fill in:

- `home_score` and `away_score`: whole-number final scores;
- `status`: exactly `final`;
- `source`: the source name you used to verify the result (for example,
  the official league results page).

Do not change the event ID, league, kickoff time, or team labels. Keep only
verified completed events. Do not enter estimated, live, postponed, or
unverified scores. Save as UTF-8 CSV.

### 3. Validate first, then explicitly apply

A dry run is the default and never writes to the live database:

```bash
python -m scripts.import_simulation_results --input data/verified-results.csv
```

The report shows accepted, duplicate, and rejected rows, including why an
event did not match the archive. Only after reviewing that report, apply it:

```bash
python -m scripts.import_simulation_results --input data/verified-results.csv --apply
```

The importer does not make network requests. Existing scores are not silently
overwritten: a correction requires a newer, timezone-aware provider update
timestamp, which this CSV workflow intentionally does not fabricate.

### 4. Fit and evaluate chronologically

Use three strictly chronological periods: an earlier training period, a later
calibration period, and a final untouched holdout period. The stored-results
backtest command enforces date ordering and event-ID separation:

```bash
python -m scripts.backtest_simulator \
  --use-stored-results \
  --training-cutoff YYYY-MM-DD \
  --calibration-cutoff YYYY-MM-DD \
  --model-output data/simulation_models.json \
  --output data/simulation-backtest.json
```

The two cutoff dates are exclusive boundaries: training is before the first,
calibration is between the two, and holdout is on/after the second. Defaults
require at least 50 archived pregame spread/total observations per league for
score-distribution fitting and at least 100 usable calibration predictions
for probability-temperature fitting. A league can have enough final scores
for team ratings but still lack enough archived pregame market snapshots for
a calibrated score-distribution model. Review the JSON report; do not lower
thresholds merely to force a green status.

This workflow deliberately does not enable TheRundown score sync, alter
environment variables, or change any provider polling settings.
