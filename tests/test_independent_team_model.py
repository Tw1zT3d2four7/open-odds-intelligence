import pytest

from app.team_model import fit_team_strength_models, project_matchup
from app.simulation import run_game_simulation


def training_rows():
    rows = []
    # Eight prior meetings give both clubs enough history. No odds are present.
    scores = [(112, 96), (108, 94), (115, 98), (110, 92),
              (97, 109), (95, 111), (99, 113), (96, 107)]
    for index, (home_score, away_score) in enumerate(scores):
        home, away = ("Alpha", "Beta") if index % 2 == 0 else ("Beta", "Alpha")
        if home == "Beta":
            home_score, away_score = away_score, home_score
        rows.append({
            "event_id": f"game-{index}",
            "sport_key": "basketball_nba",
            "home_team": home,
            "away_team": away,
            "home_score": home_score,
            "away_score": away_score,
        })
    return rows


def market_event(spread=-2.5, total=215.5):
    return {
        "id": "future-1",
        "sport_key": "basketball_nba",
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": "Alpha",
        "away_team": "Beta",
        "bookmakers": [{
            "key": "book-a",
            "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Alpha", "point": spread, "price": -110},
                    {"name": "Beta", "point": -spread, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total, "price": -110},
                    {"name": "Under", "point": total, "price": -110},
                ]},
            ],
        }],
    }


def test_team_model_fits_without_any_sportsbook_quotes():
    fit = fit_team_strength_models(training_rows(), min_team_games=4)
    model = fit["models"]["basketball_nba"]
    projection = project_matchup(model, "Alpha", "Beta")
    assert projection["source"] == "historical_team_offense_defense"
    assert projection["home_team_games"] == 8
    assert projection["away_team_games"] == 8
    assert projection["expected_margin"] > 0


def test_independent_projection_does_not_change_when_sportsbook_lines_change():
    model = fit_team_strength_models(training_rows(), min_team_games=4)["models"]["basketball_nba"]
    parameters = {
        "sport_key": "basketball_nba",
        "family": "basketball",
        "sample_size": 8,
        "independent_team_model": model,
    }
    first = run_game_simulation(market_event(-2.5, 215.5), seed=42,
                                model_parameters=parameters, require_independent_model=True)
    second = run_game_simulation(market_event(-8.5, 230.5), seed=42,
                                 model_parameters=parameters, require_independent_model=True)
    assert first["projected_score"] == second["projected_score"]
    assert first["margin_market"]["independent_projection"] == second["margin_market"]["independent_projection"]
    assert first["spread_market"]["consensus_home_spread"] != second["spread_market"]["consensus_home_spread"]
    assert first["total_market"]["consensus_line"] != second["total_market"]["consensus_line"]
    assert first["data_quality"]["model_source"] == "independent_historical_team_model"
    assert first["market_edge_analysis"]["spread"]["book_count"] == 1
    assert first["market_edge_analysis"]["total"]["book_count"] == 1
    assert first["market_edge_analysis"]["spread"]["sides"]["home"]["expected_value_percent"] is not None


def test_live_simulation_fails_closed_without_independent_model():
    with pytest.raises(ValueError, match="No independent historical team model"):
        run_game_simulation(market_event(), seed=1, require_independent_model=True)


def test_projection_refuses_unseen_teams():
    model = fit_team_strength_models(training_rows(), min_team_games=4)["models"]["basketball_nba"]
    with pytest.raises(ValueError, match="No independent rating"):
        project_matchup(model, "Alpha", "Never Seen")
