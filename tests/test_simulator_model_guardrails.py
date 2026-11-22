import pytest

from app.team_model import fit_team_strength_models, project_matchup, fit_independent_outcome_models, project_outcome_matchup


def test_score_model_requires_eight_prior_team_games_by_default():
    rows = []
    for index in range(7):
        rows.append({
            "sport_key": "basketball_nba",
            "home_team": "Home Club", "away_team": "Away Club",
            "home_score": 110 + index, "away_score": 100 + index,
        })
    model = fit_team_strength_models(rows)["models"]["basketball_nba"]
    assert model["minimum_team_games"] == 8
    with pytest.raises(ValueError, match="No independent rating"):
        project_matchup(model, "Home Club", "Away Club")


def test_elo_model_requires_eight_prior_competitor_matches_by_default():
    rows = []
    for index in range(7):
        rows.append({
            "sport_key": "tennis_atp",
            "commence_time": f"2026-01-{index + 1:02d}T00:00:00Z",
            "home_team": "Player One", "away_team": "Player Two",
            "home_score": 2, "away_score": 0,
        })
    model = fit_independent_outcome_models(rows)["models"]["tennis_atp"]
    assert model["minimum_team_matches"] == 8
    with pytest.raises(ValueError, match="No independent rating"):
        project_outcome_matchup(model, "Player One", "Player Two")
