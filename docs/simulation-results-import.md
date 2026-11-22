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

### Historical training data that has no archived odds

The archived-event template may not contain enough seasons of history to
train every team or athlete. For that older history, prepare a separate CSV
using the same required columns, but use a stable unique event ID from the
historical data source. Set `status=final` and put the specific source name or
URL in `source`. Then validate and apply it using the explicit training-only
mode:

```bash
python -m scripts.import_simulation_results --input data/historical-training.csv --training-only
python -m scripts.import_simulation_results --input data/historical-training.csv --training-only --apply
```

Training-only rows are stored separately from archived-event settlements.
They can fit independent team/athlete ratings, but they cannot be used as
calibration or holdout observations. The backtest CLI rejects them if their
dates fall on or after the training cutoff. Keep these results strictly in
the oldest training period, before calibration and holdout. The source field
is an operator attestation; verify scores against authoritative league or
tournament results before importing. Never fabricate event IDs or scores.

For team sports, enter the final home and away scores. For ATP/WTA, the two
participant fields are neutral competitor slots; use a consistent participant
ordering and encode the winner as 1 and the loser as 0 in the score fields.
That lets the Elo model learn outcomes without pretending tennis has home
teams or conventional point totals.

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
event did not match the archive. It also reports stored final-game coverage
per league, the number of teams/competitors meeting the current eight-game
rating minimum, and whether the fitted model artifact already exists. This is
coverage reporting, not a claim that a league has passed calibration or
holdout validation. Only after reviewing that report, apply it:

```bash
python -m scripts.import_simulation_results --input data/verified-results.csv --apply
```

The importer does not make network requests. Existing archived scores are
not silently overwritten: a correction requires a newer, timezone-aware
provider update timestamp, which this CSV workflow intentionally does not
fabricate. Training-only rows are immutable duplicates; re-importing the same
event ID is reported as a duplicate.

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
