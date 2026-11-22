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
    assert result["actual_total_result"] == "under"
    tie = score_prediction(prediction, {
        "event_id": "g1", "sport_key": "basketball_nba", "home_score": 100, "away_score": 100,
    })
    assert tie["actual_result"] == "tie"
    assert tie["actual_total_result"] == "push"


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

def test_stored_settlements_split_into_chronological_train_calibration_holdout():
    from app.simulation_validation import split_stored_settlements
    rows = [
        {"event_id": "a", "commence_time": "2026-08-31T23:00:00Z"},
        {"event_id": "b", "commence_time": "2026-09-15T23:00:00Z"},
        {"event_id": "c", "commence_time": "2026-10-01T00:00:00Z"},
    ]
    split = split_stored_settlements(rows, "2026-09-01", "2026-10-01")
    assert [row["event_id"] for row in split["training"]] == ["a"]
    assert [row["event_id"] for row in split["calibration"]] == ["b"]
    assert [row["event_id"] for row in split["holdout"]] == ["c"]


def test_stored_settlements_split_rejects_bad_cutoff_order():
    from app.simulation_validation import split_stored_settlements
    with pytest.raises(ValueError, match="earlier than"):
        split_stored_settlements([], "2026-10-01", "2026-09-01")

@pytest.mark.parametrize("home_score,away_score", [
    (True, 0), (100.5, 99), (-1, 0), ("100", 99), (None, 99),
])
def test_prediction_scoring_rejects_invalid_settlement_score_types(home_score, away_score):
    prediction = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    settlement = {
        "event_id": "g1", "sport_key": "basketball_nba",
        "home_score": home_score, "away_score": away_score,
    }
    with pytest.raises(ValueError, match="non-negative whole-number integer"):
        score_prediction(prediction, settlement)


def test_prediction_scoring_rejects_cross_league_settlement():
    prediction = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    settlement = {
        "event_id": "g1", "sport_key": "icehockey_nhl",
        "home_score": 3, "away_score": 2,
    }
    with pytest.raises(ValueError, match="leagues do not match"):
        score_prediction(prediction, settlement)


def test_prediction_scoring_rejects_boolean_probabilities_and_projected_scores():
    base = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    settlement = {
        "event_id": "g1", "sport_key": "basketball_nba",
        "home_score": 100, "away_score": 99,
    }
    invalid_probability = {**base, "win_probabilities": {**base["win_probabilities"], "home": True}}
    with pytest.raises(ValueError, match="not boolean"):
        score_prediction(invalid_probability, settlement)
    invalid_score = {**base, "projected_score": {**base["projected_score"], "home": True}}
    with pytest.raises(ValueError, match="not boolean"):
        score_prediction(invalid_score, settlement)


def test_prediction_scoring_rejects_overflowing_numeric_inputs():
    prediction = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 10 ** 10000, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    settlement = {
        "event_id": "g1", "sport_key": "basketball_nba",
        "home_score": 100, "away_score": 99,
    }
    with pytest.raises(ValueError, match="Projected home score must be numeric"):
        score_prediction(prediction, settlement)


def test_prediction_scoring_rejects_malformed_nested_prediction_objects():
    base = {
        "event_id": "g1", "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    settlement = {
        "event_id": "g1", "sport_key": "basketball_nba",
        "home_score": 100, "away_score": 99,
    }
    for key, value, message in (
        ("win_probabilities", [], "win probabilities must be an object"),
        ("total_market", [], "total market must be an object"),
        ("projected_score", [], "projected score must be an object"),
    ):
        malformed = {**base, key: value}
        with pytest.raises(ValueError, match=message):
            score_prediction(malformed, settlement)


def test_prediction_scoring_requires_stable_scalar_event_ids_and_objects():
    valid_prediction = {
        "event_id": "g1", "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 102, "away": 99},
        "total_market": {"consensus_line": 200, "over": 45, "under": 45, "push": 10},
    }
    valid_settlement = {
        "event_id": "g1", "home_score": 100, "away_score": 99,
    }
    with pytest.raises(ValueError, match="must be objects"):
        score_prediction([], valid_settlement)
    with pytest.raises(ValueError, match="must be objects"):
        score_prediction(valid_prediction, None)
    with pytest.raises(ValueError, match="stable event IDs"):
        score_prediction({**valid_prediction, "event_id": []}, valid_settlement)
    with pytest.raises(ValueError, match="stable event IDs"):
        score_prediction(valid_prediction, {**valid_settlement, "event_id": True})


def test_spread_probability_scoring_handles_cover_and_push_results():
    prediction = {
        "event_id": "spread-1",
        "sport": "basketball_nba",
        "win_probabilities": {"home": 60, "away": 35, "tie": 5},
        "projected_score": {"home": 105, "away": 100},
        "total_market": {"consensus_line": 205, "over": 50, "under": 45, "push": 5},
        "spread_market": {
            "consensus_home_spread": -4.5,
            "home_cover": 55,
            "away_cover": 40,
            "push": 5,
        },
    }
    cover = score_prediction(prediction, {
        "event_id": "spread-1", "sport_key": "basketball_nba",
        "home_score": 106, "away_score": 100,
    })
    assert cover["actual_spread_result"] == "home_cover"
    assert cover["spread_brier_score"] == pytest.approx((.55-1)**2 + .4**2 + .05**2)
    assert cover["spread_log_loss"] == pytest.approx(-__import__("math").log(.55))

    push_prediction = {
        **prediction,
        "spread_market": {**prediction["spread_market"], "consensus_home_spread": -4.0},
    }
    push = score_prediction(push_prediction, {
        "event_id": "spread-1", "sport_key": "basketball_nba",
        "home_score": 104, "away_score": 100,
    })
    assert push["actual_spread_result"] == "push"
    assert push["spread_log_loss"] == pytest.approx(-__import__("math").log(.05))



def test_moneyline_benchmark_builds_median_no_vig_probabilities_from_complete_books():
    from app.simulation_validation import _moneyline_market_probabilities

    event = {
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -150}, {"name": "Away", "price": 130},
            ]}]},
            {"key": "book-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Away", "price": 120}, {"name": "Home", "price": -140},
            ]}]},
        ],
    }
    result = _moneyline_market_probabilities(event)
    assert result["outcome_count"] == 2
    assert result["book_count"] == 2
    assert result["probabilities"]["home"] > result["probabilities"]["away"]
    assert result["probabilities"]["home"] + result["probabilities"]["away"] == pytest.approx(1.0)
    assert result["probabilities"]["draw"] == 0


def test_moneyline_benchmark_supports_three_way_draw_markets():
    from app.simulation_validation import _moneyline_market_probabilities, _score_moneyline_probabilities

    event = {
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [{"key": "book-a", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": 110}, {"name": "Draw", "price": 220},
            {"name": "Away", "price": 250},
        ]}]}],
    }
    market = _moneyline_market_probabilities(event)
    assert market["outcome_count"] == 3
    assert market["probabilities"]["draw"] > 0
    scored = _score_moneyline_probabilities(market["probabilities"], 3, {
        "event_id": "draw-1", "sport_key": "soccer_epl",
        "home_score": 1, "away_score": 1,
    })
    assert scored["actual_result"] == "draw"
    assert scored["brier_score"] >= 0


def test_two_way_moneyline_benchmark_skips_tied_settlements():
    from app.simulation_validation import _score_moneyline_probabilities

    result = _score_moneyline_probabilities(
        {"home": 0.6, "away": 0.4, "draw": 0}, 2,
        {"event_id": "tie-1", "sport_key": "americanfootball_nfl",
         "home_score": 20, "away_score": 20},
    )
    assert result is None


def test_moneyline_benchmark_report_compares_paired_simulator_and_market_scores():
    from app.simulation_validation import _moneyline_benchmark_report

    model_rows = [
        {"event_id": "g1", "sport": "basketball_nba", "actual_result": "home",
         "predicted_home_probability": 0.7, "predicted_away_probability": 0.3,
         "predicted_tie_probability": 0.0},
        {"event_id": "g2", "sport": "basketball_nba", "actual_result": "away",
         "predicted_home_probability": 0.4, "predicted_away_probability": 0.6,
         "predicted_tie_probability": 0.0},
    ]
    market_rows = [
        {"event_id": "g1", "sport": "basketball_nba", "actual_result": "home",
         "outcome_count": 2, "brier_score": 0.5, "log_loss": 0.9, "book_count": 3},
        {"event_id": "g2", "sport": "basketball_nba", "actual_result": "away",
         "outcome_count": 2, "brier_score": 0.5, "log_loss": 0.9, "book_count": 2},
    ]
    report = _moneyline_benchmark_report(model_rows, market_rows)
    assert report["sample_size"] == 2
    assert report["simulator"]["brier_score"] < report["sportsbook_no_vig_consensus"]["brier_score"]
    assert report["simulator_minus_market"]["brier_score"]["mean_delta"] < 0
    assert report["by_sport"]["basketball_nba"]["sample_size"] == 2


def test_evaluation_reports_spread_brier_and_log_loss_when_available():
    rows = [{
        "sport": "nba",
        "actual_result": "home",
        "predicted_home_probability": .6,
        "predicted_away_probability": .35,
        "predicted_tie_probability": .05,
        "predicted_over_probability": .5,
        "predicted_under_probability": .45,
        "predicted_total_push_probability": .05,
        "actual_total_result": "over",
        "total_brier_score": .2,
        "total_log_loss": .69,
        "brier_score": .3,
        "log_loss": .51,
        "home_score_error": 1,
        "away_score_error": 2,
        "total_score_error": 3,
        "margin_error": 1,
        "actual_spread_result": "home_cover",
        "predicted_home_cover_probability": .55,
        "predicted_away_cover_probability": .4,
        "predicted_spread_push_probability": .05,
        "spread_brier_score": .205,
        "spread_log_loss": .598,
    }]
    report = evaluate_predictions(rows)
    assert report["overall"]["spread_sample_size"] == 1
    assert report["overall"]["spread_brier_score"] == pytest.approx(.205)
    assert report["overall"]["spread_log_loss"] == pytest.approx(.598)


def test_historical_backtest_includes_same_snapshot_sportsbook_moneyline_benchmark(tmp_path):
    from app.database import OddsDatabase
    from app.simulation_validation import run_historical_backtest

    database = OddsDatabase(tmp_path / "benchmark.sqlite3")
    event = {
        "id": "benchmark-event",
        "sport_title": "Basketball",
        "commence_time": "2026-11-18T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -120}, {"name": "Away", "price": 100},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "price": -110, "point": -3.5},
                    {"name": "Away", "price": -110, "point": 3.5},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": -110, "point": 220.5},
                    {"name": "Under", "price": -110, "point": 220.5},
                ]},
            ]},
            {"key": "book-b", "title": "Book B", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Away", "price": 105}, {"name": "Home", "price": -125},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "price": -105, "point": -4.0},
                    {"name": "Away", "price": -115, "point": 4.0},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": -105, "point": 221.0},
                    {"name": "Under", "price": -115, "point": 221.0},
                ]},
            ]},
        ],
    }
    database.save_snapshot(
        "basketball_nba", [event], "test", "2026-11-17T12:00:00Z"
    )
    report = run_historical_backtest(database, [{
        "event_id": "benchmark-event",
        "sport_key": "basketball_nba",
        "commence_time": "2026-11-18T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "home_score": 112,
        "away_score": 105,
        "status": "final",
        "source": "test-fixture",
    }])
    benchmark = report["sportsbook_moneyline_benchmark"]
    assert report["events_scored"] == 1
    assert benchmark["sample_size"] == 1
    assert benchmark["market_book_count_mean"] == 2
    assert benchmark["simulator_minus_market"]["brier_score"]["ci_95"] is None
