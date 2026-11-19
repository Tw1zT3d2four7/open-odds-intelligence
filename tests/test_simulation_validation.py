import pytest

from app.simulation_validation import (
    evaluate_predictions, parse_settlements_csv, score_prediction,
)


def test_settlement_csv_requires_verified_final_and_source():
    csv = (
        "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
        "g1,basketball_nba,2026-01-01T00:00:00Z,A,B,101,98,final,official-results\n"
    )
    rows = parse_settlements_csv(csv)
    assert rows[0]["home_score"] == 101
    assert rows[0]["source"] == "official-results"


@pytest.mark.parametrize("status", ["scheduled", "postponed", "cancelled", ""])
def test_settlement_csv_rejects_non_final(status):
    csv = (
        "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
        f"g1,nba,2026-01-01T00:00:00Z,A,B,101,98,{status},feed\n"
    )
    with pytest.raises(ValueError, match="only verified final"):
        parse_settlements_csv(csv)


def test_settlement_csv_rejects_duplicate_event_ids_and_invalid_scores():
    header = "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
    duplicate = header + "g1,nba,2026-01-01T00:00:00Z,A,B,1,0,final,feed\n" + "g1,nba,2026-01-01T00:00:00Z,A,B,2,0,final,feed\n"
    with pytest.raises(ValueError, match="duplicate event_id"):
        parse_settlements_csv(duplicate)
    invalid = header + "g2,nba,2026-01-01T00:00:00Z,A,B,-1,0,final,feed\n"
    with pytest.raises(ValueError, match="non-negative"):
        parse_settlements_csv(invalid)


def test_prediction_scoring_handles_home_away_and_tie():
    prediction = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200.0, "over": 45, "under": 45, "push": 10},
    }
    result = score_prediction(prediction, {
        "event_id": "g1", "sport_key": "basketball_nba", "home_score": 101, "away_score": 98,
    })
    assert result["actual_result"] == "home"
    assert result["brier_score"] == pytest.approx((.6-1)**2 + .35**2 + .05**2)
    assert result["total_score_error"] == 2
    tie = score_prediction(prediction, {
        "event_id": "g1", "sport_key": "basketball_nba", "home_score": 100, "away_score": 100,
    })
    assert tie["actual_result"] == "tie"


def test_prediction_scoring_rejects_probability_sum_and_event_mismatch():
    prediction = {
        "event_id": "g1", "win_probabilities": {"home": 60, "away": 30, "tie": 0},
        "projected_score": {"home": 100, "away": 90},
    }
    settlement = {"event_id": "other", "home_score": 100, "away_score": 90}
    with pytest.raises(ValueError, match="IDs do not match"):
        score_prediction(prediction, settlement)
    settlement["event_id"] = "g1"
    with pytest.raises(ValueError, match="sum to 100"):
        score_prediction(prediction, settlement)


def test_evaluation_reports_sample_sizes_metrics_and_calibration():
    rows = [
        {"sport": "nba", "actual_result": "home", "predicted_home_probability": .8, "predicted_away_probability": .15, "predicted_tie_probability": .05,
         "predicted_over_probability": .7, "predicted_under_probability": .25, "predicted_total_push_probability": .05, "actual_total_result": "over", "total_brier_score": .2, "total_log_loss": .35,
         "brier_score": .08, "log_loss": .223, "home_score_error": 2,
         "away_score_error": 1, "total_score_error": 3, "margin_error": 1},
        {"sport": "nba", "actual_result": "away", "predicted_home_probability": .2, "predicted_away_probability": .75, "predicted_tie_probability": .05,
         "predicted_over_probability": .2, "predicted_under_probability": .75, "predicted_total_push_probability": .05, "actual_total_result": "under", "total_brier_score": .2, "total_log_loss": .35,
         "brier_score": .08, "log_loss": .223, "home_score_error": 1,
         "away_score_error": 2, "total_score_error": 3, "margin_error": 1},
    ]
    report = evaluate_predictions(rows, calibration_bins=5)
    assert report["sample_size"] == 2
    assert report["status"] == "DESCRIPTIVE_ONLY"
    assert report["overall"]["brier_score"] == pytest.approx(.08)
    assert report["by_sport"]["nba"]["sample_size"] == 2
    assert sum(x["sample_size"] for x in report["calibration"]) == 2
    assert set(report["calibration_by_class"]) == {"home", "away", "tie"}
    assert set(report["total_calibration_by_class"]) == {"over", "under", "push"}


def test_empty_evaluation_is_explicitly_insufficient():
    report = evaluate_predictions([])
    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["sample_size"] == 0


def test_chronological_split_rejects_overlapping_event_ids():
    from app.simulation_validation import validate_chronological_split
    row = {"event_id": "same", "sport_key": "basketball_nba",
           "commence_time": "2026-01-01T00:00:00Z"}
    with pytest.raises(ValueError, match="overlap by event_id"):
        validate_chronological_split([row], [row])


def test_chronological_split_rejects_time_leakage_within_league():
    from app.simulation_validation import validate_chronological_split
    training = [{"event_id": "train", "sport_key": "basketball_nba",
                "commence_time": "2026-01-03T00:00:00Z"}]
    test = [{"event_id": "test", "sport_key": "basketball_nba",
            "commence_time": "2026-01-02T00:00:00Z"}]
    with pytest.raises(ValueError, match="must all precede"):
        validate_chronological_split(training, test)


def test_chronological_split_accepts_separate_later_holdout():
    from app.simulation_validation import validate_chronological_split
    training = [{"event_id": "train", "sport_key": "basketball_nba",
                "commence_time": "2026-01-01T00:00:00Z"}]
    test = [{"event_id": "test", "sport_key": "basketball_nba",
            "commence_time": "2026-01-02T00:00:00Z"}]
    validate_chronological_split(training, test)

def test_settlement_csv_rejects_invalid_start_timestamp():
    csv = (
        "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
        "g1,basketball_nba,not-a-date,A,B,100,99,final,verified-feed\n"
    )
    with pytest.raises(ValueError, match="commence_time must be ISO-8601"):
        parse_settlements_csv(csv)

def test_settlement_csv_requires_explicit_timezone_and_rows():
    header = "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
    with pytest.raises(ValueError, match="ISO-8601 with timezone"):
        parse_settlements_csv(header + "g1,nba,2026-01-01T00:00:00,A,B,1,0,final,feed\n")
    with pytest.raises(ValueError, match="no result rows"):
        parse_settlements_csv(header)