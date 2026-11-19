"""Run a paper-only simulator backtest against imported final scores.

Usage:
  python -m scripts.backtest_simulator --settlements results.csv
"""
import argparse
import json
from pathlib import Path

from app import config
from app.database import OddsDatabase
from app.simulation_validation import parse_settlements_csv, run_historical_backtest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settlements", required=True, help="CSV of verified final scores")
    parser.add_argument("--output", help="Optional JSON report path; defaults to stdout")
    args = parser.parse_args()

    csv_path = Path(args.settlements)
    settlements = parse_settlements_csv(csv_path.read_text(encoding="utf-8-sig"))
    database = OddsDatabase(config.DATABASE_PATH)
    report = run_historical_backtest(database, settlements)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
