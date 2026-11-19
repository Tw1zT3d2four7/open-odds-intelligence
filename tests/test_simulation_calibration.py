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
        })
    result = fit_probability_temperatures(rows, min_sample_size=100)
    assert "basketball_nba" in result["temperatures"]
    assert 0.5 <= result["temperatures"]["basketball_nba"]["temperature"] <= 3.0
    assert 0.5 <= result["temperatures"]["basketball_nba"]["total_temperature"] <= 3.0
    too_small = fit_probability_temperatures(rows[:99], min_sample_size=100)
    assert too_small["temperatures"] == {}
    assert too_small["insufficient"]["basketball_nba"]["sample_size"] == 99