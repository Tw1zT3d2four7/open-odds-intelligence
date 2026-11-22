"""Safely export and import verified final scores for the local simulator.

Examples:
  python -m scripts.import_simulation_results --template data/results-template.csv
  python -m scripts.import_simulation_results --input data/verified-results.csv
  python -m scripts.import_simulation_results --input data/verified-results.csv --apply
  python -m scripts.import_simulation_results --input data/historical-training.csv --training-only
  python -m scripts.import_simulation_results --input data/historical-training.csv --training-only --apply

Archived imports must match an exact event in the local odds archive. Training-only
imports accept verified historical results without archived odds and are kept in
a separate table that is prohibited from calibration and holdout use. All imports
are dry-run by default. This script never contacts an odds or scores provider.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from app import config
from app.database import OddsDatabase
from app.simulation_validation import parse_settlements_csv
from app.team_model import fit_independent_outcome_models, fit_team_strength_models

TEMPLATE_FIELDS = (
    "event_id", "sport_key", "commence_time", "home_team", "away_team",
    "home_score", "away_score", "status", "source",
)


def export_archived_event_template(database: OddsDatabase, destination: str) -> int:
    """Write one row per archived event identity; leave result fields blank."""
    query = """
        SELECT DISTINCT q.event_id, q.sport, q.commence_time, q.home_team, q.away_team
        FROM quotes q
        JOIN snapshots s ON s.id=q.snapshot_id
        JOIN (
            SELECT q2.event_id, q2.sport, MAX(s2.id) AS snapshot_id
            FROM quotes q2
            JOIN snapshots s2 ON s2.id=q2.snapshot_id
            GROUP BY q2.event_id, q2.sport
        ) latest
          ON latest.event_id=q.event_id
         AND latest.sport=q.sport
         AND latest.snapshot_id=s.id
        WHERE q.event_id <> ''
        ORDER BY q.sport, q.commence_time, q.event_id
    """
    with database.connect() as db:
        rows = db.execute(query).fetchall()
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TEMPLATE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "event_id": row["event_id"],
                "sport_key": row["sport"],
                "commence_time": row["commence_time"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "home_score": "",
                "away_score": "",
                "status": "",
                "source": "",
            })
    return len(rows)


def _dry_run_database(database: OddsDatabase) -> tuple[OddsDatabase, str]:
    """Create an isolated SQLite copy so validation cannot mutate live data."""
    handle = tempfile.NamedTemporaryFile(prefix="ooi-settlements-", suffix=".sqlite3", delete=False)
    path = handle.name
    handle.close()
    try:
        source = database.connect()
        target = sqlite3.connect(path)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        return OddsDatabase(path), path
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def import_results(
    database: OddsDatabase, content: str, apply: bool = False, training_only: bool = False
) -> dict:
    """Validate verified results; historical training rows are stored separately."""
    records = parse_settlements_csv(content)
    target = database
    temporary_path = None
    if not apply:
        target, temporary_path = _dry_run_database(database)
    try:
        result = (
            target.save_simulation_training_settlements(records)
            if training_only else target.save_simulation_settlements(records)
        )
        stored = target.simulation_settlements()
        team_fit = fit_team_strength_models(stored)
        outcome_fit = fit_independent_outcome_models(stored)
        coverage = {}
        for sport, values in team_fit["league_coverage"].items():
            coverage[sport] = {
                "stored_final_games": values["league_games"],
                "eligible_teams": values["teams_with_eligible_ratings"],
                "total_teams_seen": values["teams_total"],
                "model_family": "independent_team_strength",
            }
        for sport, values in outcome_fit["coverage"].items():
            coverage[sport] = {
                "stored_final_matches": values["training_matches"],
                "eligible_competitors": values["eligible_athletes"],
                "model_family": "independent_elo_outcomes",
            }
        result["stored_coverage"] = dict(sorted(coverage.items()))
        result["model_artifact_exists"] = Path(config.SIMULATION_MODEL_PATH).is_file()
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
            Path(temporary_path + "-wal").unlink(missing_ok=True)
            Path(temporary_path + "-shm").unlink(missing_ok=True)
    result["mode"] = "applied" if apply else "dry_run"
    result["dataset"] = "training_only" if training_only else "archived_event_results"
    result["rows_submitted"] = len(records)
    result["by_league"] = dict(sorted(Counter(row["sport_key"] for row in records).items()))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=config.DATABASE_PATH, help="SQLite database path")
    parser.add_argument("--template", help="Export archived event IDs and identities to a blank CSV template")
    parser.add_argument("--input", help="Import a completed verified final-score CSV (dry-run by default)")
    parser.add_argument("--apply", action="store_true", help="Persist accepted results; without this flag, no live data is changed")
    parser.add_argument("--training-only", action="store_true", help="Import historical results without odds-archive IDs; these rows are restricted to the training period")
    args = parser.parse_args()
    if bool(args.template) == bool(args.input):
        parser.error("Provide exactly one of --template or --input.")
    if args.apply and args.template:
        parser.error("--apply is only valid with --input.")
    database = OddsDatabase(args.database)
    if args.template:
        count = export_archived_event_template(database, args.template)
        print(json.dumps({"template": str(Path(args.template)), "archived_events": count}, indent=2))
        return
    result = import_results(
        database, Path(args.input).read_text(encoding="utf-8-sig"),
        apply=args.apply, training_only=args.training_only,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["rejected"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
