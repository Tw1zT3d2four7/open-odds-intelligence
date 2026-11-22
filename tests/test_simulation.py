import pytest

from app.simulation import run_game_simulation


def sample_event():
    return {
        "id": "nba-1",
        "sport_key": "basketball_nba",
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": -4.5, "price": -110},
                    {"name": "Away", "point": 4.5, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 221.5, "price": -110},
                    {"name": "Under", "point": 221.5, "price": -110},
                ]},
            ]},
            {"key": "book-b", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": -5.0, "price": -105},
                    {"name": "Away", "point": 5.0, "price": -115},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 222.0, "price": -105},
                    {"name": "Under", "point": 222.0, "price": -115},
                ]},
            ]},
        ],
    }


def test_simulation_runs_exactly_ten_thousand_trials_for_one_event():
    result = run_game_simulation(sample_event())
    assert result["trials"] == 10_000
    assert result["event_id"] == "nba-1"
    assert result["data_quality"]["spread_books"] == 2
    assert result["data_quality"]["total_books"] == 2
    assert result["total_market"]["consensus_line"] == 221.75
    assert result["margin_market"]["consensus_home_margin"] == 4.75
    assert abs(sum(result["win_probabilities"].values()) - 100) <= 0.02
    assert abs(result["total_market"]["over"] + result["total_market"]["under"] + result["total_market"]["push"] - 100) <= 0.02
    ranges = result["simulation_ranges"]
    for key in ("home_score", "away_score", "home_margin", "game_total"):
        summary = ranges[key]
        assert summary["p10"] <= summary["median"] <= summary["p90"]
        assert summary["standard_deviation"] >= 0
        assert summary["interval"] == "empirical_p10_p90"
    assert ranges["home_score"]["mean"] == result["projected_score"]["home"]
    assert ranges["away_score"]["mean"] == result["projected_score"]["away"]
    assert "not a confidence interval" in ranges["interpretation"]


def test_simulation_rejects_event_without_spread_or_total():
    event = sample_event()
    event["bookmakers"][0]["markets"] = []
    event["bookmakers"][1]["markets"] = []
    try:
        run_game_simulation(event)
    except ValueError as exc:
        assert "spread and total" in str(exc)
    else:
        raise AssertionError("Expected missing market inputs to be rejected")


def test_simulation_supports_soccer_poisson_scoring():
    event = sample_event()
    event["sport_key"] = "soccer_epl"
    result = run_game_simulation(event)
    assert result["data_quality"]["distribution"] == "poisson"
    assert result["trials"] == 10_000


def test_market_readiness_rejects_market_names_without_usable_lines():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"][0]["markets"] = [
        {"key": "spreads", "outcomes": [{"name": "Home", "price": -110}]},
        {"key": "totals", "outcomes": [{"name": "Over", "price": -110}]},
    ]
    event["bookmakers"][1]["markets"] = []
    result = market_readiness(event)
    assert result["can_simulate"] is False
    assert result["has_spread"] is False
    assert result["has_total"] is False
    assert "spread" in result["reason"]
    assert "total" in result["reason"]


def test_market_readiness_accepts_usable_spread_and_total_lines():
    from app.simulation import market_readiness

    result = market_readiness(sample_event())
    assert result["can_simulate"] is True
    assert result["has_spread"] is True
    assert result["has_total"] is True
    assert result["spread_books"] == 2
    assert result["total_books"] == 2
    assert result["reason"] is None


def test_simulation_matches_provider_team_labels_case_and_whitespace_insensitively():
    event = sample_event()
    event["home_team"] = "  HOME "
    event["away_team"] = "away"
    event["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = "home"
    event["bookmakers"][0]["markets"][0]["outcomes"][1]["name"] = " AWAY "
    event["bookmakers"][1]["markets"][0]["outcomes"][0]["name"] = "Home"
    event["bookmakers"][1]["markets"][0]["outcomes"][1]["name"] = "Away"
    result = run_game_simulation(event)
    assert result["data_quality"]["spread_books"] == 2
    assert result["margin_market"]["consensus_home_margin"] == 4.75


def test_simulation_reports_spread_team_mapping_separately_from_total():
    event = sample_event()
    for book in event["bookmakers"]:
        book["markets"][0]["outcomes"][0]["name"] = "Unknown Home Club"
        book["markets"][0]["outcomes"][1]["name"] = "Unknown Away Club"
    try:
        run_game_simulation(event)
    except ValueError as exc:
        assert "spread" in str(exc)
    else:
        raise AssertionError("Expected unknown spread team labels to be rejected")


def test_simulation_matches_city_event_names_to_full_nfl_bookmaker_names():
    event = sample_event()
    event["sport_key"] = "americanfootball_nfl"
    event["home_team"] = "Houston"
    event["away_team"] = "Indianapolis"
    for book in event["bookmakers"]:
        book["markets"][0]["outcomes"] = [
            {"name": "Houston Texans", "point": -3.5, "price": -110},
            {"name": "Indianapolis Colts", "point": 3.5, "price": -110},
        ]
    from app.simulation import market_readiness
    assert market_readiness(event)["can_simulate"] is True
    result = run_game_simulation(event)
    assert result["data_quality"]["spread_books"] == 2
    assert result["margin_market"]["consensus_home_margin"] == 3.5


def test_simulation_matches_full_ncaab_school_names_to_event_names():
    event = sample_event()
    event["sport_key"] = "basketball_ncaab"
    event["home_team"] = "West Virginia"
    event["away_team"] = "Florida"
    for book in event["bookmakers"]:
        book["markets"][0]["outcomes"] = [
            {"name": "Florida Gators", "point": -6.5, "price": -110},
            {"name": "West Virginia Mountaineers", "point": 6.5, "price": -110},
        ]
    from app.simulation import market_readiness
    assert market_readiness(event)["can_simulate"] is True
    result = run_game_simulation(event)
    assert result["data_quality"]["spread_books"] == 2
    assert result["margin_market"]["consensus_home_margin"] == -6.5


def test_team_prefix_matching_rejects_ambiguous_shared_abbreviations():
    from app.simulation import _team_side
    assert _team_side("LA", "Los Angeles Clippers", "Los Angeles Lakers") is None


def test_spread_requires_both_teams_and_complementary_points():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"][0]["markets"][0]["outcomes"].pop()
    event["bookmakers"][1]["markets"][0]["outcomes"][1]["point"] = 3.0
    result = market_readiness(event)
    assert result["spread_books"] == 0
    assert result["has_spread"] is False


def test_total_requires_matching_over_and_under_points():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"][0]["markets"][1]["outcomes"][1]["point"] = 220.5
    event["bookmakers"][1]["markets"][1]["outcomes"].pop()
    result = market_readiness(event)
    assert result["total_books"] == 0
    assert result["has_total"] is False


def test_non_finite_market_points_are_rejected():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = float("nan")
    event["bookmakers"][1]["markets"][1]["outcomes"][0]["point"] = float("inf")
    result = market_readiness(event)
    assert result["spread_books"] == 1
    assert result["total_books"] == 1


def test_invalid_prices_are_not_used_as_market_inputs():
    from app.simulation import market_readiness

    event = sample_event()
    for book in event["bookmakers"]:
        for market in book["markets"]:
            for outcome in market["outcomes"]:
                outcome["price"] = 0
    result = market_readiness(event)
    assert result["can_simulate"] is False


def test_duplicate_bookmaker_records_do_not_inflate_consensus_counts():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"].append(event["bookmakers"][0])
    result = market_readiness(event)
    assert result["spread_books"] == 2
    assert result["total_books"] == 2
    assert any("duplicate bookmaker" in str(item) for item in result["market_diagnostics"])


def test_market_diagnostics_explain_incomplete_book_quotes():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"][0]["markets"][0]["outcomes"] = []
    result = market_readiness(event)
    assert result["can_simulate"] is True
    diagnostic = next(item for item in result["market_diagnostics"] if item["bookmaker"] == "book-a")
    assert "complete" in diagnostic["spread"]


def test_malformed_bookmaker_containers_are_handled_without_crashing():
    from app.simulation import market_readiness

    event = sample_event()
    event["bookmakers"] = [None, "bad", {"key": "broken", "markets": None}]
    result = market_readiness(event)
    assert result["can_simulate"] is False
    assert result["spread_books"] == 0
    assert result["total_books"] == 0


def test_simulation_rejects_non_dict_event():
    try:
        run_game_simulation(None)
    except ValueError as exc:
        assert "valid stored game event" in str(exc)
    else:
        raise AssertionError("Expected invalid event to be rejected")


def test_simulation_seed_reproduces_identical_results():
    event = sample_event()
    first = run_game_simulation(event, seed=20261119)
    second = run_game_simulation(event, seed=20261119)
    assert first == second
    assert first["simulation_seed"] == 20261119


def test_simulation_rejects_non_integer_seed():
    event = sample_event()
    try:
        run_game_simulation(event, seed="not-an-integer")
    except ValueError as exc:
        assert "seed must be an integer" in str(exc)
    else:
        raise AssertionError("Expected invalid simulation seed to be rejected")


def test_simulation_accepts_only_matching_empirical_league_parameters():
    event = sample_event()
    params = {
        "sport_key": "basketball_nba", "family": "basketball", "sample_size": 250,
        "margin_sd": 10.0, "total_sd": 15.0, "margin_total_corr": 0.35,
        "margin_bias": 0.5, "total_bias": -1.0,
    }
    result = run_game_simulation(event, seed=7, model_parameters=params)
    assert result["data_quality"]["model_source"] == "empirical_training_fit"
    assert result["data_quality"]["training_sample_size"] == 250
    assert result["margin_market"]["home_margin_sd"] == 10.0
    params["sport_key"] = "basketball_ncaab"
    try:
        run_game_simulation(event, seed=7, model_parameters=params)
    except ValueError as exc:
        assert "selected league" in str(exc)
    else:
        raise AssertionError("Expected fitted model for another league to be rejected")


@pytest.mark.parametrize("parameter", [
    "margin_sd", "total_sd", "margin_total_corr", "margin_bias",
    "total_bias", "temperature", "total_temperature", "spread_temperature", "home_alpha", "away_alpha", "shared_alpha",
])
def test_simulation_rejects_boolean_fitted_model_parameters(parameter):
    event = sample_event()
    params = {
        "sport_key": "basketball_nba", "family": "basketball", "sample_size": 250,
        parameter: True,
    }
    with pytest.raises(ValueError, match=parameter):
        run_game_simulation(event, model_parameters=params)


def test_simulation_rejects_invalid_empirical_parameters():
    event = sample_event()
    params = {"sport_key": "basketball_nba", "family": "basketball",
              "margin_sd": 0.0, "total_sd": 15.0}
    try:
        run_game_simulation(event, model_parameters=params)
    except ValueError as exc:
        assert "margin_sd" in str(exc)
    else:
        raise AssertionError("Expected invalid fitted variance to be rejected")

def test_load_fitted_models_accepts_valid_per_league_file_and_missing_file(tmp_path):
    import json
    from app.simulation import load_fitted_models
    assert load_fitted_models(tmp_path / "missing.json") == {}
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"basketball_nba": {
        "sport_key": "basketball_nba", "family": "basketball", "sample_size": 200,
        "margin_sd": 10.0, "total_sd": 15.0, "margin_total_corr": 0.2,
        "margin_bias": 0.0, "total_bias": 0.0,
    }}}), encoding="utf-8")
    assert load_fitted_models(path)["basketball_nba"]["sample_size"] == 200


def test_load_fitted_models_rejects_wrong_league_identity(tmp_path):
    import json
    from app.simulation import load_fitted_models
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"basketball_nba": {
        "sport_key": "basketball_ncaab", "family": "basketball", "margin_sd": 10,
    }}}), encoding="utf-8")
    try:
        load_fitted_models(path)
    except ValueError as exc:
        assert "selected league" in str(exc)
    else:
        raise AssertionError("Expected league mismatch to be rejected")

def test_league_specific_tie_resolution_rules_are_applied():
    import random
    from app.simulation import _resolve_tie
    for sport in ("basketball_nba", "basketball_ncaab", "icehockey_nhl",
                  "baseball_mlb", "americanfootball_ncaaf"):
        home, away, overtime = _resolve_tie(random.Random(1234), sport, 2, 2)
        assert home != away
        assert overtime is True
    home, away, overtime = _resolve_tie(random.Random(1234), "soccer_epl", 1, 1)
    assert (home, away, overtime) == (1, 1, False)


def test_nba_simulation_reports_final_winner_after_overtime():
    event = sample_event()
    result = run_game_simulation(event, seed=12345)
    assert result["win_probabilities"]["tie"] == 0
    assert result["game_resolution"]["regulation_ties"] >= result["game_resolution"]["games_with_overtime_or_extra_innings"]


def test_nhl_simulation_has_no_final_ties_after_overtime_or_shootout():
    event = sample_event()
    event["sport_key"] = "icehockey_nhl"
    event["bookmakers"][0]["markets"][1]["outcomes"] = [
        {"name": "Over", "point": 6.5, "price": -110},
        {"name": "Under", "point": 6.5, "price": -110},
    ]
    event["bookmakers"][1]["markets"][1]["outcomes"] = [
        {"name": "Over", "point": 6.5, "price": -110},
        {"name": "Under", "point": 6.5, "price": -110},
    ]
    result = run_game_simulation(event, seed=12345)
    assert result["win_probabilities"]["tie"] == 0
    assert result["game_resolution"]["rule"] == "league_specific_approximation"

def test_simulation_applies_fitted_multiclass_temperature():
    event = sample_event()
    params = {
        "sport_key": "basketball_nba", "family": "basketball", "sample_size": 250,
        "margin_sd": 10.0, "total_sd": 15.0, "margin_total_corr": 0.2,
        "temperature": 1.5, "total_temperature": 1.7,
    }
    result = run_game_simulation(event, seed=22, model_parameters=params)
    probs = result["win_probabilities"]
    assert abs(probs["home"] + probs["away"] + probs["tie"] - 100) < 0.001
    assert result["data_quality"]["model_source"] == "empirical_training_fit"
    assert result["margin_market"]["probability_temperature"] == 1.5
    assert result["total_market"]["probability_temperature"] == 1.7

def test_nfl_regular_season_overtime_can_remain_tied():
    from app.simulation import _resolve_tie
    class NoScoreOvertime:
        @staticmethod
        def choices(values, weights, k):
            return [0]
    home, away, overtime = _resolve_tie(NoScoreOvertime(), "americanfootball_nfl", 20, 20)
    assert (home, away, overtime) == (20, 20, True)

def test_simulator_rejects_fractional_and_non_finite_american_prices():
    import pytest
    from app.simulation import _valid_price

    for price in (100.5, -110.25, float("nan"), float("inf"), True, False, 0, "bad"):
        assert _valid_price(price) is False
    for price in (100, -100, 105, -110, "125"):
        assert _valid_price(price) is True


def test_simulation_market_readiness_ignores_fractional_price_quotes():
    from app.simulation import market_readiness

    event = sample_event()
    for book in event["bookmakers"]:
        for market in book["markets"]:
            for outcome in market["outcomes"]:
                outcome["price"] = 110.5
    result = market_readiness(event)
    assert result["can_simulate"] is False
    assert result["spread_books"] == 0
    assert result["total_books"] == 0


@pytest.mark.parametrize("commence_time, message", [
    ("2020-01-01T00:00:00Z", "only available before the game starts"),
    ("not-a-timestamp", "valid ISO-8601"),
    ("2099-11-20T00:00:00", "explicit timezone"),
    (None, "upcoming game start time"),
])
def test_simulation_rejects_started_or_invalidly_scheduled_games(commence_time, message):
    event = sample_event()
    event["commence_time"] = commence_time
    from app.simulation import _validate_upcoming_event
    with pytest.raises(ValueError, match=message):
        _validate_upcoming_event(event)


def test_market_readiness_blocks_started_or_invalidly_scheduled_games():
    from app.simulation import market_readiness

    event = sample_event()
    event["commence_time"] = "2020-01-01T00:00:00Z"
    readiness = market_readiness(event)
    assert readiness["has_start_time"] is False
    assert readiness["can_simulate"] is False
    assert "before the game starts" in readiness["reason"]


def test_simulation_reports_spread_cover_probabilities_from_final_trial_scores():
    result = run_game_simulation(sample_event(), seed=20261122)
    spread = result["spread_market"]
    assert spread["consensus_home_spread"] == -4.75
    assert 0 <= spread["home_cover"] <= 100
    assert 0 <= spread["away_cover"] <= 100
    assert 0 <= spread["push"] <= 100
    assert abs(spread["home_cover"] + spread["away_cover"] + spread["push"] - 100) <= 0.02
    assert spread["settlement_basis"] == "simulated_final_score"


def test_simulation_spread_cover_probabilities_are_seed_reproducible():
    event = sample_event()
    first = run_game_simulation(event, seed=42)
    second = run_game_simulation(event, seed=42)
    assert first["spread_market"] == second["spread_market"]


def test_calibration_only_model_is_not_mislabeled_as_empirical_training_fit():
    event = sample_event()
    params = {
        "sport_key": "basketball_nba",
        "family": "basketball",
        "sample_size": 0,
        "temperature": 1.2,
        "total_temperature": 1.1,
    }
    result = run_game_simulation(event, seed=99, model_parameters=params)
    assert result["data_quality"]["model_source"] == "broad_league_prior"
    assert result["margin_market"]["model_source"] == "broad_league_prior"
    assert result["margin_market"]["probability_temperature"] == 1.2
    assert result["total_market"]["probability_temperature"] == 1.1
