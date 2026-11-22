"""Run a paper-only simulator backtest against imported or automatically stored final scores.

CSV mode:
  python -m scripts.backtest_simulator --training-settlements train.csv --calibration-settlements calibrate.csv --settlements holdout.csv

Stored TheRundown results mode:
  python -m scripts.backtest_simulator --use-stored-results --training-cutoff 2026-09-01 --calibration-cutoff 2026-10-01
"""
import argparse
import json
from pathlib import Path

from app import config
from app.database import OddsDatabase
from app.simulation_validation import (
    fit_league_models, fit_probability_temperatures, parse_settlements_csv,
    run_historical_backtest, split_stored_settlements, validate_chronological_split,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settlements", help="CSV of verified holdout final scores")
    parser.add_argument("--training-settlements", help="Earlier-period verified results used only to fit league distributions")
    parser.add_argument("--calibration-settlements", help="Separate later verified results used to fit probability temperatures")
    parser.add_argument("--use-stored-results", action="store_true", help="Use automatically collected final scores from the local SQLite database")
    parser.add_argument("--training-cutoff", help="Stored-results mode: exclusive YYYY-MM-DD end of training period")
    parser.add_argument("--calibration-cutoff", help="Stored-results mode: exclusive YYYY-MM-DD end of calibration period")
    parser.add_argument("--min-training-sample", type=int, default=50, help="Minimum training events required per league (default: 50)")
    parser.add_argument("--min-calibration-sample", type=int, default=100, help="Minimum calibration events required per league (default: 100)")
    parser.add_argument("--model-output", help="Optional path to save fitted models JSON for live simulation")
    parser.add_argument("--output", help="Optional JSON report path; defaults to stdout")
    args = parser.parse_args()

    database = OddsDatabase(config.DATABASE_PATH)
    training_rows = None
    calibration_rows = None
    stored_split = None
    if args.use_stored_results:
        if args.settlements or args.training_settlements or args.calibration_settlements:
            parser.error("--use-stored-results cannot be combined with CSV settlement arguments")
        if not args.training_cutoff or not args.calibration_cutoff:
            parser.error("--use-stored-results requires --training-cutoff and --calibration-cutoff")
        stored = database.simulation_settlements()
        if not stored:
            parser.error("No stored final scores yet. Wait for TheRundown score sync or import verified CSV results.")
        try:
            stored_split = split_stored_settlements(stored, args.training_cutoff, args.calibration_cutoff)
        except ValueError as exc:
            parser.error(str(exc))
        training_rows = stored_split["training"]
        calibration_rows = stored_split["calibration"]
        settlements = stored_split["holdout"]
        misplaced_training_only = [
            row.get("event_id") for row in calibration_rows + settlements
            if row.get("training_only")
        ]
        if misplaced_training_only:
            parser.error(
                "Training-only historical results must all precede --training-cutoff; "
                "move the cutoff later or remove the misplaced rows. Examples: "
                + ", ".join(str(value) for value in misplaced_training_only[:5])
            )
    else:
        if not args.settlements:
            parser.error("Provide --settlements or use --use-stored-results.")
        if args.calibration_settlements and not args.training_settlements:
            parser.error("--calibration-settlements requires --training-settlements")
        settlements = parse_settlements_csv(Path(args.settlements).read_text(encoding="utf-8-sig"))
        if args.training_settlements:
            training_rows = parse_settlements_csv(Path(args.training_settlements).read_text(encoding="utf-8-sig"))
        if args.calibration_settlements:
            calibration_rows = parse_settlements_csv(Path(args.calibration_settlements).read_text(encoding="utf-8-sig"))

    models = None
    training_report = None
    calibration_report = None
    calibration_fit = None
    if training_rows is not None:
        validate_chronological_split(training_rows, settlements)
        training_report = fit_league_models(database, training_rows, args.min_training_sample)
        models = training_report["models"]
    if calibration_rows is not None:
        validate_chronological_split(training_rows or [], calibration_rows)
        validate_chronological_split(calibration_rows, settlements)
        calibration_report = run_historical_backtest(
            database, calibration_rows, model_parameters=models, include_prediction_rows=True
        )
        calibration_fit = fit_probability_temperatures(
            calibration_report.get("scored_predictions", []) +
            calibration_report.get("scored_outcome_predictions", []),
            args.min_calibration_sample
        )
        for sport, values in calibration_fit["temperatures"].items():
            family = next((name for token, name in (("basketball", "basketball"),
                ("football", "football"), ("baseball", "baseball"),
                ("hockey", "hockey"), ("soccer", "soccer"),
                ("tennis", "tennis"), ("mma", "mma")) if token in sport), None)
            if family is not None:
                if models is None:
                    models = {}
                models.setdefault(sport, {"sport_key": sport, "family": family, "sample_size": 0})
                models[sport]["temperature"] = values["temperature"]
                if "total_temperature" in values:
                    models[sport]["total_temperature"] = values["total_temperature"]
                if "spread_temperature" in values:
                    models[sport]["spread_temperature"] = values["spread_temperature"]

    report = run_historical_backtest(database, settlements, model_parameters=models)
    if training_report is not None:
        report["training"] = training_report
    if stored_split is not None:
        report["stored_results_split"] = {
            "cutoffs": stored_split["cutoffs"],
            "training_events": len(training_rows),
            "calibration_events": len(calibration_rows),
            "holdout_events": len(settlements),
        }
    if calibration_report is not None:
        report["calibration"] = {
            "evaluation": {key: value for key, value in calibration_report.items() if key not in ("scored_predictions", "scored_outcome_predictions")},
            "fitted_temperatures": calibration_fit,
        }
    if args.model_output:
        if training_report is None:
            parser.error("--model-output requires a training dataset or --use-stored-results")
        model_payload = {
            "models": models or {},
            "training_events_used": training_report["training_events_used"],
            "minimum_sample_size": training_report["minimum_sample_size"],
            "calibration": calibration_fit,
            "warning": training_report["warning"],
        }
        Path(args.model_output).write_text(
            json.dumps(model_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()