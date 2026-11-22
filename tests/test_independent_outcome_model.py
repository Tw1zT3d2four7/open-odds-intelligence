import pytest

from app.team_model import fit_independent_outcome_models, project_outcome_matchup


def test_elo_outcome_model_is_fit_from_chronological_results_not_market_prices():
    rows = []
    for index in range(8):
        rows.append({
            "sport_key": "tennis_atp",
            "commence_time": f"2026-01-{index + 1:02d}T00:00:00Z",
            "home_team": "Player A",
            "away_team": "Player B",
            "home_score": 2,
            "away_score": 0,
        })
    fit = fit_independent_outcome_models(rows, min_team_matches=4)
    model = fit["models"]["tennis_atp"]
    projection = project_outcome_matchup(model, "Player A", "Player B")
    assert projection["source"] == "historical_elo"
    assert projection["home_probability"] > projection["away_probability"]
    assert projection["draw_probability"] == 0
    assert projection["home_matches"] == 8
    assert projection["away_matches"] == 8


def test_elo_projection_refuses_competitors_without_enough_history():
    rows = [{
        "sport_key": "mma_mixed_martial_arts",
        "commence_time": "2026-01-01T00:00:00Z",
        "home_team": "Fighter A", "away_team": "Fighter B",
        "home_score": 1, "away_score": 0,
    }]
    model = fit_independent_outcome_models(rows, min_team_matches=2)["models"]["mma_mixed_martial_arts"]
    with pytest.raises(ValueError, match="No independent rating"):
        project_outcome_matchup(model, "Fighter A", "Fighter B")
