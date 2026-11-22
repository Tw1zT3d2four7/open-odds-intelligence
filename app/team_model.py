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
    min_team_games: int = 8,
    shrinkage_games: float = 12.0,
) -> dict:
    """Fit league scoring baselines and shrunk team offense/defense ratings."""
    if isinstance(min_team_games, bool) or not isinstance(min_team_games, int) or min_team_games < 2:
        raise ValueError("min_team_games must be an integer of at least 2.")
    if not math.isfinite(float(shrinkage_games)) or shrinkage_games <= 0:
        raise ValueError("shrinkage_games must be positive and finite.")

    leagues = defaultdict(lambda: {
        "home_scores": [], "away_scores": [], "games": [], "teams": defaultdict(
            lambda: {"display_name": "", "games": 0}
        )
    })
    for row in settlements:
        sport = str(row.get("sport_key") or "").strip()
        home_name, away_name = str(row.get("home_team") or "").strip(), str(row.get("away_team") or "").strip()
        home, away = normalize_team(home_name), normalize_team(away_name)
        if (not sport or not any(token in sport.lower() for token in
                ("basketball", "americanfootball", "baseball", "icehockey", "soccer"))
                or not home or not away or home == away):
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
        bucket["games"].append((home, away, hs, aws))

    output, insufficient = {}, {}
    for sport, bucket in sorted(leagues.items()):
        count = len(bucket["home_scores"])
        if not count:
            continue
        home_base = sum(bucket["home_scores"]) / count
        away_base = sum(bucket["away_scores"]) / count
        team_stats = bucket["teams"]
        ratings = {key: {"offense": 0.0, "defense": 0.0}
                   for key in team_stats}
        # Iterative opponent adjustment: a team's attack is evaluated against
        # opponent defense and its defense against opponent attack. This avoids
        # treating every schedule as equally difficult.
        for _ in range(30):
            offense_observations = defaultdict(list)
            defense_observations = defaultdict(list)
            for home, away, home_score, away_score in bucket["games"]:
                offense_observations[home].append(
                    home_score - home_base + ratings[away]["defense"]
                )
                offense_observations[away].append(
                    away_score - away_base + ratings[home]["defense"]
                )
                defense_observations[home].append(
                    away_base + ratings[away]["offense"] - away_score
                )
                defense_observations[away].append(
                    home_base + ratings[home]["offense"] - home_score
                )
            updated = {}
            for key, stats in team_stats.items():
                games = stats["games"]
                shrink = games / (games + float(shrinkage_games))
                raw_offense = sum(offense_observations[key]) / games
                raw_defense = sum(defense_observations[key]) / games
                updated[key] = {
                    "offense": raw_offense * shrink,
                    "defense": raw_defense * shrink,
                }
            ratings = updated

        teams = {}
        for key, stats in sorted(team_stats.items()):
            games = stats["games"]
            if games < min_team_games:
                continue
            teams[key] = {
                "display_name": stats["display_name"],
                "games": games,
                "offense": ratings[key]["offense"],
                "defense": ratings[key]["defense"],
            }
        output[sport] = {
            "version": MODEL_VERSION,
            "training_games": count,
            "league_home_mean": home_base,
            "league_away_mean": away_base,
            "minimum_team_games": min_team_games,
            "shrinkage_games": float(shrinkage_games),
            "rating_method": "iterative_opponent_adjusted_offense_defense",
            "rating_iterations": 30,
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
    minimum = int(model.get("minimum_team_games", 8))
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



def fit_independent_outcome_models(
    settlements: list[dict],
    min_team_matches: int = 8,
    k_factor: float = 24.0,
) -> dict:
    """Fit chronological Elo ratings for tennis and combat sports from final outcomes."""
    if isinstance(min_team_matches, bool) or not isinstance(min_team_matches, int) or min_team_matches < 2:
        raise ValueError("min_team_matches must be an integer of at least 2.")
    if not math.isfinite(float(k_factor)) or k_factor <= 0:
        raise ValueError("k_factor must be positive and finite.")
    grouped = defaultdict(list)
    for row in settlements:
        sport = str(row.get("sport_key") or "").lower()
        if "tennis" not in sport and "mma" not in sport and "mixed_martial_arts" not in sport:
            continue
        home, away = normalize_team(row.get("home_team")), normalize_team(row.get("away_team"))
        hs, aws = row.get("home_score"), row.get("away_score")
        if (not sport or not home or not away or home == away
                or isinstance(hs, bool) or isinstance(aws, bool)
                or not isinstance(hs, int) or not isinstance(aws, int)
                or hs < 0 or aws < 0):
            continue
        grouped[sport].append((str(row.get("commence_time") or ""), home, away,
                               str(row.get("home_team") or ""), str(row.get("away_team") or ""),
                               1.0 if hs > aws else 0.0 if hs < aws else 0.5))

    models = {}
    for sport, games in sorted(grouped.items()):
        games.sort(key=lambda row: row[0])
        ratings, counts = defaultdict(lambda: 1500.0), defaultdict(int)
        displays = {}
        draws = 0
        for _, home, away, home_label, away_label, actual in games:
            displays[home], displays[away] = home_label, away_label
            expected_home = 1.0 / (1.0 + 10.0 ** ((ratings[away] - ratings[home]) / 400.0))
            change = float(k_factor) * (actual - expected_home)
            ratings[home] += change
            ratings[away] -= change
            counts[home] += 1
            counts[away] += 1
            draws += int(actual == 0.5)
        eligible = {
            key: {"display_name": displays[key], "rating": rating, "matches": counts[key]}
            for key, rating in ratings.items() if counts[key] >= min_team_matches
        }
        models[sport] = {
            "version": MODEL_VERSION,
            "training_matches": len(games),
            "minimum_team_matches": min_team_matches,
            "draw_rate": 0.0 if "tennis" in sport else (draws + 1.0) / (len(games) + 50.0),
            "teams": eligible,
        }
    return {
        "models": models,
        "coverage": {
            sport: {"training_matches": model["training_matches"],
                    "eligible_athletes": len(model["teams"])}
            for sport, model in models.items()
        },
        "training_matches": sum(model["training_matches"] for model in models.values()),
    }


def project_outcome_matchup(model: dict, home_team: str, away_team: str) -> dict:
    """Generate independent match/contest probabilities from historical Elo ratings."""
    if not isinstance(model, dict) or model.get("version") != MODEL_VERSION:
        raise ValueError("An independently fitted athlete/team outcome model is required.")
    teams = model.get("teams")
    home_key, away_key = normalize_team(home_team), normalize_team(away_team)
    if not isinstance(teams, dict) or not home_key or not away_key:
        raise ValueError("Independent athlete/team ratings are unavailable.")
    home, away = teams.get(home_key), teams.get(away_key)
    minimum = int(model.get("minimum_team_matches", 8))
    if not home or not away:
        missing = home_team if not home else away_team
        raise ValueError(f"No independent rating with sufficient history for {missing}.")
    if int(home.get("matches", 0)) < minimum or int(away.get("matches", 0)) < minimum:
        raise ValueError("Both competitors need more historical matches before an independent projection is available.")
    home_rating, away_rating = float(home["rating"]), float(away["rating"])
    home_win_no_draw = 1.0 / (1.0 + 10.0 ** ((away_rating - home_rating) / 400.0))
    draw_rate = float(model.get("draw_rate", 0.0))
    if not math.isfinite(draw_rate) or not 0 <= draw_rate <= 0.25:
        raise ValueError("Independent outcome model has an invalid draw rate.")
    home_probability = home_win_no_draw * (1.0 - draw_rate)
    away_probability = (1.0 - home_win_no_draw) * (1.0 - draw_rate)
    return {
        "home_probability": home_probability,
        "away_probability": away_probability,
        "draw_probability": draw_rate,
        "home_rating": home_rating,
        "away_rating": away_rating,
        "home_matches": int(home["matches"]),
        "away_matches": int(away["matches"]),
        "model_version": MODEL_VERSION,
        "source": "historical_elo",
    }
