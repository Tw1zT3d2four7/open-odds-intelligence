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
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
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
