from app.simulation import run_game_simulation


def sample_event():
    return {
        "id": "nba-1",
        "sport_key": "basketball_nba",
        "commence_time": "2026-11-20T00:00:00Z",
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
