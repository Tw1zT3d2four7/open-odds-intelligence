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
    assert "spread line" in result["reason"]
    assert "total line" in result["reason"]


def test_market_readiness_accepts_usable_spread_and_total_lines():
    from app.simulation import market_readiness

    result = market_readiness(sample_event())
    assert result == {
        "can_simulate": True,
        "has_spread": True,
        "has_total": True,
        "reason": None,
    }


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
        book["markets"][0]["outcomes"][0]["name"] = "Home Team"
        book["markets"][0]["outcomes"][1]["name"] = "Away Team"
    try:
        run_game_simulation(event)
    except ValueError as exc:
        assert "spread line matching" in str(exc)
    else:
        raise AssertionError("Expected unknown spread team labels to be rejected")
