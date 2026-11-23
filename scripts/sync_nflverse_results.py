"""Import NFL final scores from the public nflverse schedules release.

Only game identity, date, team abbreviations, and final scores are read.
The upstream CSV also contains betting fields; this importer deliberately
never reads or uses any betting-line, moneyline, spread, or total columns.

Results are stored in the existing training-only table. They are not eligible
for calibration/holdout or for archived-odds replay. Dry-run is the default.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from app import config
from app.database import OddsDatabase
from scripts.import_simulation_results import import_results

NFLVERSE_SCHEDULES_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)
NFLVERSE_ATTRIBUTION = "nflverse schedules (CC BY 4.0)"
SPORT_KEY = "americanfootball_nfl"
FINAL_GAME_TYPES = {"REG", "WC", "DIV", "CON", "SB"}

# Historical abbreviations are mapped to current franchise identities so
# relocations/name changes do not split one franchise into separate ratings.
TEAM_NAMES = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}
TEAM_ALIASES = {
    "OAK": "LV", "SD": "LAC", "STL": "LAR", "LA": "LAR",
    "WSH": "WAS", "WFT": "WAS", "JAC": "JAX",
}
REQUIRED_COLUMNS = {
    "game_id", "season", "game_type", "gameday",
    "away_team", "away_score", "home_team", "home_score",
}


def _team_name(abbreviation: str) -> str:
    key = (abbreviation or "").strip().upper()
    key = TEAM_ALIASES.get(key, key)
    try:
        return TEAM_NAMES[key]
    except KeyError:
        raise ValueError(f"Unsupported nflverse team abbreviation: {abbreviation!r}") from None


def parse_nflverse_games(content: str, min_season: int = 1999,
                         through_season: int | None = None) -> list[dict]:
    """Convert completed NFL games to training-only settlements.

    nflverse supplies a game date but not a consistently timezone-aware kickoff
    timestamp in this release. A noon-UTC date anchor is used solely for
    chronological ordering/splitting; it must never be used to match odds.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("nflverse schedules CSV is empty.")
    reader = csv.DictReader(io.StringIO(content))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ValueError("nflverse schedules missing columns: " + ", ".join(missing))

    records = []
    seen = {}
    for line_number, row in enumerate(reader, start=2):
        game_id = (row.get("game_id") or "").strip()
        if not game_id:
            raise ValueError(f"Line {line_number}: game_id is required.")
        try:
            season = int(row["season"])
        except (TypeError, ValueError):
            raise ValueError(f"Line {line_number}: invalid season.") from None
        if season < min_season or (through_season is not None and season > through_season):
            continue
        game_type = (row.get("game_type") or "").strip().upper()
        if game_type not in FINAL_GAME_TYPES:
            continue
        raw_date = (row.get("gameday") or "").strip()
        try:
            game_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        raw_home, raw_away = (row.get("home_score") or "").strip(), (row.get("away_score") or "").strip()
        if not raw_home or not raw_away:
            continue
        try:
            home_score, away_score = int(raw_home), int(raw_away)
        except ValueError:
            raise ValueError(f"Line {line_number}: final scores must be whole numbers.") from None
        if home_score < 0 or away_score < 0:
            raise ValueError(f"Line {line_number}: final scores cannot be negative.")

        home_team = _team_name(row.get("home_team"))
        away_team = _team_name(row.get("away_team"))
        if home_team == away_team:
            raise ValueError(f"Line {line_number}: home and away teams resolve to the same franchise.")

        # Date-only anchor: no fabricated kickoff time is used for quote matching.
        commence_time = f"{game_date.isoformat()}T12:00:00Z"
        record = {
            "season": season,
            "event_id": game_id,
            "sport_key": SPORT_KEY,
            "commence_time": commence_time,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "status": "final",
            "source": NFLVERSE_ATTRIBUTION,
        }
        prior = seen.get(game_id)
        if prior is not None:
            if prior != record:
                raise ValueError(f"Conflicting duplicate game_id {game_id!r}.")
            continue
        seen[game_id] = record
        records.append(record)

    records.sort(key=lambda item: (item["commence_time"], item["event_id"]))
    if not records:
        raise ValueError("No completed regular-season or playoff games matched the requested seasons.")
    return records


def records_to_csv(records: list[dict]) -> str:
    fields = (
        "event_id", "sport_key", "commence_time", "home_team", "away_team",
        "home_score", "away_score", "status", "source",
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def download_schedules(url: str = NFLVERSE_SCHEDULES_URL) -> str:
    request = Request(url, headers={"User-Agent": "Open-Odds-Intelligence/1.0"})
    with urlopen(request, timeout=45) as response:
        payload = response.read()
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("nflverse schedules download was not valid UTF-8 CSV.") from None


def sync_nflverse_results(database: OddsDatabase, content: str,
                          min_season: int = 1999,
                          through_season: int | None = None,
                          apply: bool = False) -> dict:
    records = parse_nflverse_games(content, min_season, through_season)
    result = import_results(
        database, records_to_csv(records), apply=apply, training_only=True
    )
    result["source"] = {
        "name": "nflverse schedules",
        "url": NFLVERSE_SCHEDULES_URL,
        "license": "CC BY 4.0",
        "attribution": NFLVERSE_ATTRIBUTION,
        "fields_used": sorted(REQUIRED_COLUMNS),
        "betting_fields_used": [],
        "date_handling": "Noon-UTC date anchor for chronology only; never for odds matching.",
    }
    result["season_range"] = {
        "first": min(int(row["commence_time"][:4]) for row in records),
        "last": max(int(row["commence_time"][:4]) for row in records),
    }
    result["training_only_rows"] = len(records)
    result["mode"] = "applied" if apply else "dry_run"
    return result


def _outcome_probabilities(row, model, trials=10_000):
    """Replay the live NFL score-draw engine without using any market prices."""
    import hashlib
    import random

    from app.simulation import LEAGUE_PRIORS, _draw_regulation_scores, _resolve_tie
    from app.team_model import project_matchup

    projection = project_matchup(model, row["home_team"], row["away_team"])
    seed = int.from_bytes(
        hashlib.sha256(str(row["event_id"]).encode("utf-8")).digest()[:4], "big"
    )
    rng = random.Random(seed)
    counts = [0, 0, 0]
    prior = LEAGUE_PRIORS["football"]
    for _ in range(trials):
        home, away = _draw_regulation_scores(
            rng, "football", projection["expected_margin"],
            projection["expected_total"], prior, {},
        )
        if home == away:
            home, away, _ = _resolve_tie(rng, SPORT_KEY, home, away)
        counts[0 if home > away else 1 if home < away else 2] += 1
    return projection, tuple(value / trials for value in counts)


def _baseline_probabilities(rows):
    counts = [1, 1, 1]
    for row in rows:
        home, away = row["home_score"], row["away_score"]
        counts[0 if home > away else 1 if home < away else 2] += 1
    total = sum(counts)
    return tuple(value / total for value in counts)


def _temperature_adjust(probabilities, temperature):
    weights = [max(1e-12, value) ** (1.0 / temperature) for value in probabilities]
    total = sum(weights)
    return tuple(value / total for value in weights)


def _fit_temperature(rows):
    best_temperature, best_loss = 1.0, float("inf")
    for step in range(50, 301, 2):
        temperature = step / 100.0
        losses = []
        for row in rows:
            actual = row["actual_index"]
            probabilities = _temperature_adjust(row["raw_probabilities"], temperature)
            losses.append(-math.log(max(1e-12, probabilities[actual])))
        loss = sum(losses) / len(losses)
        if loss < best_loss:
            best_temperature, best_loss = temperature, loss
    return best_temperature, best_loss


def _evaluate_rows(rows, temperature=1.0):
    if not rows:
        return {"sample_size": 0}
    model_brier, model_log_loss = [], []
    baseline_brier, baseline_log_loss = [], []
    home_errors, away_errors, total_errors, margin_errors = [], [], [], []
    baseline_home_errors, baseline_away_errors = [], []
    baseline_total_errors, baseline_margin_errors = [], []
    for row in rows:
        actual = row["actual_index"]
        calibrated = _temperature_adjust(row["raw_probabilities"], temperature)
        baseline = row["baseline_probabilities"]
        model_brier.append(sum(
            (probability - (1.0 if index == actual else 0.0)) ** 2
            for index, probability in enumerate(calibrated)
        ))
        baseline_brier.append(sum(
            (probability - (1.0 if index == actual else 0.0)) ** 2
            for index, probability in enumerate(baseline)
        ))
        model_log_loss.append(-math.log(max(1e-12, calibrated[actual])))
        baseline_log_loss.append(-math.log(max(1e-12, baseline[actual])))

        actual_home, actual_away = row["home_score"], row["away_score"]
        predicted_home, predicted_away = row["predicted_home"], row["predicted_away"]
        baseline_home, baseline_away = row["baseline_home"], row["baseline_away"]
        home_errors.append(abs(predicted_home - actual_home))
        away_errors.append(abs(predicted_away - actual_away))
        total_errors.append(abs(predicted_home + predicted_away - actual_home - actual_away))
        margin_errors.append(abs(predicted_home - predicted_away - actual_home + actual_away))
        baseline_home_errors.append(abs(baseline_home - actual_home))
        baseline_away_errors.append(abs(baseline_away - actual_away))
        baseline_total_errors.append(abs(baseline_home + baseline_away - actual_home - actual_away))
        baseline_margin_errors.append(abs(baseline_home - baseline_away - actual_home + actual_away))

    mean = lambda values: sum(values) / len(values)
    return {
        "sample_size": len(rows),
        "moneyline": {
            "brier_score": mean(model_brier),
            "baseline_brier_score": mean(baseline_brier),
            "log_loss": mean(model_log_loss),
            "baseline_log_loss": mean(baseline_log_loss),
        },
        "score_accuracy": {
            "home_score_mae": mean(home_errors),
            "away_score_mae": mean(away_errors),
            "total_score_mae": mean(total_errors),
            "margin_mae": mean(margin_errors),
            "baseline_home_score_mae": mean(baseline_home_errors),
            "baseline_away_score_mae": mean(baseline_away_errors),
            "baseline_total_score_mae": mean(baseline_total_errors),
            "baseline_margin_mae": mean(baseline_margin_errors),
        },
    }


def _predict_season(rows, prior_rows, min_team_games=8, trials=10_000):
    from app.team_model import fit_team_strength_models

    if not prior_rows:
        return []
    fitted = fit_team_strength_models(prior_rows, min_team_games=min_team_games)
    model = fitted["models"].get(SPORT_KEY)
    if not model:
        return []
    baseline_probabilities = _baseline_probabilities(prior_rows)
    baseline_home = model["league_home_mean"]
    baseline_away = model["league_away_mean"]
    predictions = []
    for row in rows:
        try:
            projection, raw_probabilities = _outcome_probabilities(row, model, trials=trials)
        except ValueError:
            continue
        actual_index = (
            0 if row["home_score"] > row["away_score"]
            else 1 if row["home_score"] < row["away_score"] else 2
        )
        predictions.append({
            "event_id": row["event_id"],
            "season": row["season"],
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "actual_index": actual_index,
            "raw_probabilities": raw_probabilities,
            "baseline_probabilities": baseline_probabilities,
            "predicted_home": projection["home_score"],
            "predicted_away": projection["away_score"],
            "baseline_home": baseline_home,
            "baseline_away": baseline_away,
        })
    return predictions


def build_nfl_historical_model(records, training_end_season=2018,
                               calibration_end_season=2021,
                               through_season=2025,
                               min_calibration_games=100,
                               min_holdout_games=1000,
                               min_team_games=8,
                               trials=10_000):
    """Walk-forward train/calibrate/holdout using scores only, then refit for live use.

    Each validation season is predicted by a model fitted only on seasons before
    that season. Calibration predictions never train on their own results.
    Holdout seasons can use completed calibration seasons, but never future
    games. No sportsbook prices are read or synthesized.
    """
    if training_end_season >= calibration_end_season:
        raise ValueError("Training end season must precede calibration end season.")
    if calibration_end_season >= through_season:
        raise ValueError("Calibration end season must precede the final data season.")
    if min_calibration_games < 1 or min_holdout_games < 1 or min_team_games < 2:
        raise ValueError("Minimum sample sizes must be positive and min_team_games at least 2.")
    if trials != 10_000:
        raise ValueError("Validation uses the simulator's fixed 10,000 trials.")

    ordered = sorted(
        (row for row in records if row.get("season", 0) <= through_season),
        key=lambda row: (row["season"], row["commence_time"], row["event_id"]),
    )
    training_rows = [row for row in ordered if row["season"] <= training_end_season]
    calibration_rows = [
        row for row in ordered
        if training_end_season < row["season"] <= calibration_end_season
    ]
    holdout_rows = [row for row in ordered if row["season"] > calibration_end_season]
    if not training_rows or not calibration_rows or not holdout_rows:
        raise ValueError("The selected seasons must contain training, calibration, and holdout games.")

    calibration_predictions, holdout_predictions = [], []
    calibration_seasons = sorted({row["season"] for row in calibration_rows})
    holdout_seasons = sorted({row["season"] for row in holdout_rows})
    for season in calibration_seasons:
        prior = [row for row in ordered if row["season"] < season]
        current = [row for row in calibration_rows if row["season"] == season]
        calibration_predictions.extend(
            _predict_season(current, prior, min_team_games=min_team_games, trials=trials)
        )
    for season in holdout_seasons:
        prior = [row for row in ordered if row["season"] < season]
        current = [row for row in holdout_rows if row["season"] == season]
        holdout_predictions.extend(
            _predict_season(current, prior, min_team_games=min_team_games, trials=trials)
        )

    if len(calibration_predictions) < min_calibration_games:
        calibration_temperature = None
        calibration_log_loss = None
    else:
        calibration_temperature, calibration_log_loss = _fit_temperature(calibration_predictions)

    holdout_report = _evaluate_rows(
        holdout_predictions,
        temperature=calibration_temperature if calibration_temperature is not None else 1.0,
    )
    checks = {
        "calibration_sample_size_met": len(calibration_predictions) >= min_calibration_games,
        "holdout_sample_size_met": len(holdout_predictions) >= min_holdout_games,
        "margin_mae_beats_baseline": (
            holdout_report.get("score_accuracy", {}).get("margin_mae", float("inf"))
            < holdout_report.get("score_accuracy", {}).get("baseline_margin_mae", float("-inf"))
        ),
        "moneyline_brier_beats_baseline": (
            holdout_report.get("moneyline", {}).get("brier_score", float("inf"))
            < holdout_report.get("moneyline", {}).get("baseline_brier_score", float("-inf"))
        ),
        "moneyline_log_loss_beats_baseline": (
            holdout_report.get("moneyline", {}).get("log_loss", float("inf"))
            < holdout_report.get("moneyline", {}).get("baseline_log_loss", float("-inf"))
        ),
    }
    passed = all(checks.values())
    report = {
        "status": "PASS" if passed else "BACKTEST FAILED - PICKS DISABLED",
        "sport_key": SPORT_KEY,
        "source": {
            "name": "nflverse schedules",
            "url": NFLVERSE_SCHEDULES_URL,
            "license": "CC BY 4.0",
            "attribution": NFLVERSE_ATTRIBUTION,
            "model_inputs": ["game_id", "season", "game_type", "gameday",
                             "away_team", "away_score", "home_team", "home_score"],
            "betting_fields_used": [],
        },
        "walk_forward": {
            "training_through_season": training_end_season,
            "calibration_seasons": calibration_seasons,
            "holdout_seasons": holdout_seasons,
            "training_games": len(training_rows),
            "calibration_games": len(calibration_rows),
            "calibration_predictions": len(calibration_predictions),
            "holdout_games": len(holdout_rows),
            "holdout_predictions": len(holdout_predictions),
            "method": "Each season is predicted using only final scores from earlier seasons.",
        },
        "calibration": {
            "temperature": calibration_temperature,
            "sample_size": len(calibration_predictions),
            "log_loss_before_holdout": calibration_log_loss,
            "minimum_required": min_calibration_games,
        },
        "holdout": holdout_report,
        "checks": checks,
        "limitations": [
            "NFLverse game dates are date-only; a noon-UTC anchor is used only for chronological season ordering.",
            "No historical sportsbook prices, spreads, totals, or moneylines are used.",
            "Holdout validation measures independent score and moneyline performance, not profitability of spread/total betting edges.",
            "The simulator's spread/total edge outputs still require separate market-line validation before being treated as profitable signals.",
        ],
    }
    if not passed:
        return report, None

    from app.team_model import fit_team_strength_models

    final_fit = fit_team_strength_models(ordered, min_team_games=min_team_games)
    final_team_model = final_fit["models"].get(SPORT_KEY)
    if not final_team_model:
        report["status"] = "BACKTEST FAILED - PICKS DISABLED"
        report["checks"]["final_model_fit_available"] = False
        return report, None
    artifact = {
        "models": {
            SPORT_KEY: {
                "sport_key": SPORT_KEY,
                "family": "football",
                "sample_size": len(ordered),
                "temperature": calibration_temperature,
                "independent_team_model": final_team_model,
            }
        },
        "validation": report,
        "training_data": {
            "source": NFLVERSE_ATTRIBUTION,
            "through_season": through_season,
            "games_used_for_final_fit": len(ordered),
            "betting_fields_used": [],
        },
    }
    return report, artifact


def write_model_artifact(path, artifact):
    """Atomically write a validated fitted-model artifact."""
    if not isinstance(artifact, dict) or artifact.get("validation", {}).get("status") != "PASS":
        raise ValueError("Refusing to write a model artifact without a passing holdout validation.")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=config.DATABASE_PATH)
    parser.add_argument("--min-season", type=int, default=1999)
    parser.add_argument("--through-season", type=int, default=datetime.now(timezone.utc).year - 1)
    parser.add_argument("--input", help="Use a previously downloaded CSV instead of downloading.")
    parser.add_argument("--apply", action="store_true",
                        help="Persist results. Without this flag, the live database is unchanged.")
    args = parser.parse_args()
    content = Path(args.input).read_text(encoding="utf-8-sig") if args.input else download_schedules()
    result = sync_nflverse_results(
        OddsDatabase(args.database), content,
        min_season=args.min_season, through_season=args.through_season,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("rejected"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
