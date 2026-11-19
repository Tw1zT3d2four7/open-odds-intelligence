"""Run a paper-only simulator backtest against imported final scores.

Usage:
  python -m scripts.backtest_simulator --settlements results.csv
"""
import argparse
import json
from pathlib import Path

from app import config
from app.database import OddsDatabase
from app.simulation_validation import (
    fit_league_models, parse_settlements_csv, run_historical_backtest,
    validate_chronological_split,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settlements", required=True, help="CSV of verified final scores")
    parser.add_argument("--training-settlements", help="Earlier-period verified results used only to fit league distributions")
    parser.add_argument("--min-training-sample", type=int, default=50, help="Minimum training events required per league (default: 50)")
    parser.add_argument("--model-output", help="Optional path to save fitted models JSON for live simulation")
    parser.add_argument("--output", help="Optional JSON report path; defaults to stdout")
    args = parser.parse_args()

    csv_path = Path(args.settlements)
    settlements = parse_settlements_csv(csv_path.read_text(encoding="utf-8-sig"))
    database = OddsDatabase(config.DATABASE_PATH)
    models = None
    training_report = None
    if args.training_settlements:
        training_path = Path(args.training_settlements)
        training_rows = parse_settlements_csv(training_path.read_text(encoding="utf-8-sig"))
        validate_chronological_split(training_rows, settlements)
        training_report = fit_league_models(database, training_rows, args.min_training_sample)
        models = training_report["models"]
    report = run_historical_backtest(database, settlements, model_parameters=models)
    if training_report is not None:
        report["training"] = training_report
    if args.model_output:
        if training_report is None:
            parser.error("--model-output requires --training-settlements")
        model_payload = {
            "models": models,
            "training_events_used": training_report["training_events_used"],
            "minimum_sample_size": training_report["minimum_sample_size"],
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
