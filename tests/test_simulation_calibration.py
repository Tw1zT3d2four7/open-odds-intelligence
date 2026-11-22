import pytest



def test_temperature_calibration_requires_minimum_sample_and_fits_per_league():
    from app.simulation_validation import fit_probability_temperatures
    rows = []
    for index in range(100):
        rows.append({
            "sport": "basketball_nba",
            "actual_result": "home" if index % 2 == 0 else "away",
            "predicted_home_probability": 0.8,
            "predicted_away_probability": 0.2,
            "predicted_tie_probability": 0.0,
            "actual_total_result": "over" if index % 2 == 0 else "under",
            "predicted_over_probability": 0.8,
            "predicted_under_probability": 0.2,
            "predicted_total_push_probability": 0.0,
            "actual_spread_result": "home_cover" if index % 2 == 0 else "away_cover",
            "predicted_home_cover_probability": 0.8,
            "predicted_away_cover_probability": 0.2,
            "predicted_spread_push_probability": 0.0,
        })
    result = fit_probability_temperatures(rows, min_sample_size=100)
    assert "basketball_nba" in result["temperatures"]
    assert 0.5 <= result["temperatures"]["basketball_nba"]["temperature"] <= 3.0
    assert 0.5 <= result["temperatures"]["basketball_nba"]["total_temperature"] <= 3.0
    assert 0.5 <= result["temperatures"]["basketball_nba"]["spread_temperature"] <= 3.0
    too_small = fit_probability_temperatures(rows[:99], min_sample_size=100)
    assert too_small["temperatures"] == {}
    assert too_small["insufficient"]["basketball_nba"]["sample_size"] == 99


def test_outcome_only_elo_calibration_does_not_require_score_market_fields():
    from app.simulation_validation import fit_probability_temperatures

    rows = []
    for index in range(100):
        rows.append({
            "sport": "tennis_atp",
            "actual_result": "home" if index % 2 == 0 else "away",
            "predicted_home_probability": 0.7,
            "predicted_away_probability": 0.3,
            "predicted_tie_probability": 0.0,
        })
    result = fit_probability_temperatures(rows, min_sample_size=100)
    values = result["temperatures"]["tennis_atp"]
    assert 0.5 <= values["temperature"] <= 3.0
    assert "total_temperature" not in values
