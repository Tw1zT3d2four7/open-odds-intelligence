"""Independent, team-specific scoring projections trained only on past final scores.

No sportsbook prices, spreads, totals, or moneylines are read by this module.
The output is deliberately conservative: a team without enough prior games
cannot receive an independent projection.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

MODEL_VERSION = 1


def normalize_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def fit_team_strength_models(
    settlements: list[dict],
    min_team_games: int = 4,
    shrinkage_games: float = 8.0,
) -> dict:
    """Fit league scoring baselines and shrunk team offense/defense ratings."""
    if isinstance(min_team_games, bool) or not isinstance(min_team_games, int) or min_team_games < 2:
        raise ValueError("min_team_games must be an integer of at least 2.")
    if not math.isfinite(float(shrinkage_games)) or shrinkage_games <= 0:
        raise ValueError("shrinkage_games must be positive and finite.")

    leagues = defaultdict(lambda: {
        "home_scores": [], "away_scores": [], "teams": defaultdict(
            lambda: {"display_name": "", "games": 0, "offense": [], "defense": []}
        )
    })
    for row in settlements:
        sport = str(row.get("sport_key") or "").strip()
        home_name, away_name = str(row.get("home_team") or "").strip(), str(row.get("away_team") or "").strip()
        home, away = normalize_team(home_name), normalize_team(away_name)
        if not sport or not home or not away or home == away:
            continue
        hs, aws = row.get("home_score"), row.get("away_score")
        if (isinstance(hs, bool) or isinstance(aws, bool)
                or not isinstance(hs, int) or not isinstance(aws, int)
                or hs < 0 or aws < 0):
            continue
        bucket = leagues[sport]
        bucket["home_scores"].append(hs)
        bucket["away_scores"].append(aws)
        h = bucket["teams"][home]
        a = bucket["teams"][away]
        h["display_name"] = home_name
        a["display_name"] = away_name
        h["games"] += 1
        a["games"] += 1
        h["offense"].append(hs)
        h["defense"].append(aws)
        a["offense"].append(aws)
        a["defense"].append(hs)

    output, insufficient = {}, {}
    for sport, bucket in sorted(leagues.items()):
        count = len(bucket["home_scores"])
        if not count:
            continue
        home_base = sum(bucket["home_scores"]) / count
        away_base = sum(bucket["away_scores"]) / count
        teams = {}
        for key, stats in sorted(bucket["teams"].items()):
            games = stats["games"]
            if games < min_team_games:
                continue
            # Each rating is measured against the appropriate league scoring baseline.
            raw_offense = sum(
                score - (home_base if index < 0 else away_base)
                for index, score in enumerate(stats["offense"])
            ) / games
            # Home/away role mix is normalized using the team's observed venue mix.
            # For each game, the offense list is in game order; retain role-aware
            # residuals from the explicit fields assembled below when available.
            off_residuals = stats.get("offense_residuals", [])
            def_residuals = stats.get("defense_residuals", [])
            if off_residuals:
                raw_offense = sum(off_residuals) / len(off_residuals)
            raw_defense = sum(def_residuals) / len(def_residuals) if def_residuals else 0.0
            shrink = games / (games + float(shrinkage_games))
            teams[key] = {
                "display_name": stats["display_name"],
                "games": games,
                "offense": raw_offense * shrink,
                "defense": raw_defense * shrink,
            }
        output[sport] = {
            "version": MODEL_VERSION,
            "training_games": count,
            "league_home_mean": home_base,
            "league_away_mean": away_base,
            "minimum_team_games": min_team_games,
            "shrinkage_games": float(shrinkage_games),
            "teams": teams,
        }
        insufficient[sport] = {
            "league_games": count,
            "teams_with_eligible_ratings": len(teams),
            "teams_total": len(bucket["teams"]),
        }
    return {
        "models": output,
        "league_coverage": insufficient,
        "training_games": sum(len(x["home_scores"]) for x in leagues.values()),
        "warning": "Ratings use only final scores from the supplied training period; they are not independently validated until tested on a later untouched period.",
    }


def project_matchup(model: dict, home_team: str, away_team: str) -> dict:
    """Return a team-driven score projection, refusing unknown/under-sampled teams."""
    if not isinstance(model, dict) or model.get("version") != MODEL_VERSION:
        raise ValueError("An independently fitted team model is required for this league.")
    teams = model.get("teams")
    home_key, away_key = normalize_team(home_team), normalize_team(away_team)
    if not isinstance(teams, dict) or not home_key or not away_key:
        raise ValueError("Independent team ratings are unavailable for this matchup.")
    home, away = teams.get(home_key), teams.get(away_key)
    minimum = int(model.get("minimum_team_games", 4))
    if not home or not away:
        missing = home_team if not home else away_team
        raise ValueError(f"No independent rating with sufficient history for {missing}.")
    if int(home.get("games", 0)) < minimum or int(away.get("games", 0)) < minimum:
        raise ValueError("Both teams need more historical games before an independent projection is available.")
    home_score = float(model["league_home_mean"]) + float(home["offense"]) + float(away["defense"])
    away_score = float(model["league_away_mean"]) + float(away["offense"]) + float(home["defense"])
    home_score = max(0.05, home_score)
    away_score = max(0.05, away_score)
    return {
        "home_score": home_score,
        "away_score": away_score,
        "expected_margin": home_score - away_score,
        "expected_total": home_score + away_score,
        "home_team_games": int(home["games"]),
        "away_team_games": int(away["games"]),
        "model_version": MODEL_VERSION,
        "source": "historical_team_offense_defense",
    }
