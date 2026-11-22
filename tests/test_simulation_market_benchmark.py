import pytest

from app.simulation_market_benchmark import score_archived_spread_total, _paired_report


def _event(spread_home=-3.5, spread_away=3.5, total=44.5):
    return {
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": spread_home, "price": -110},
                    {"name": "Away", "point": spread_away, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total, "price": -105},
                    {"name": "Under", "point": total, "price": -115},
                ]},
            ]},
            {"key": "book-b", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Away", "point": spread_away, "price": 100},
                    {"name": "Home", "point": spread_home, "price": -120},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Under", "point": total, "price": 100},
                    {"name": "Over", "point": total, "price": -120},
                ]},
            ]},
        ],
    }


def _prediction(spread=-3.5, total=44.5):
    return {
        "spread_market": {"consensus_home_spread": spread,
                          "home_cover": 52, "away_cover": 43, "push": 5},
        "total_market": {"consensus_line": total},
    }


def test_archived_spread_total_benchmark_scores_same_consensus_lines():
    result = score_archived_spread_total(
        _event(), {"home_score": 24, "away_score": 17}, _prediction()
    )
    assert result["spread"]["actual"] == "first"
    assert result["spread"]["book_count"] == 2
    assert result["spread"]["brier_score"] >= 0
    assert result["total"]["actual"] == "second"
    assert result["total"]["book_count"] == 2


def test_archived_spread_total_excludes_pushes_from_binary_market_benchmark():
    result = score_archived_spread_total(
        _event(spread_home=-3, spread_away=3, total=41),
        {"home_score": 20, "away_score": 17},
        _prediction(spread=-3, total=41),
    )
    assert result == {"spread": None, "total": None}


def test_archived_spread_total_does_not_mix_different_alt_lines():
    result = score_archived_spread_total(
        _event(spread_home=-4.5, spread_away=4.5, total=45.5),
        {"home_score": 24, "away_score": 17},
        _prediction(spread=-3.5, total=44.5),
    )
    assert result == {"spread": None, "total": None}


def test_paired_market_report_exposes_sample_and_uncertainty():
    model = [
        {"event_id": "a", "sport": "basketball_nba", "brier_score": .4, "log_loss": .7},
        {"event_id": "b", "sport": "basketball_nba", "brier_score": .3, "log_loss": .6},
    ]
    market = [
        {"event_id": "a", "sport": "basketball_nba", "brier_score": .5, "log_loss": .8, "book_count": 3},
        {"event_id": "b", "sport": "basketball_nba", "brier_score": .4, "log_loss": .7, "book_count": 2},
    ]
    report = _paired_report(model, market, "sportsbook_no_vig_consensus")
    assert report["sample_size"] == 2
    assert report["simulator_minus_market"]["brier_score"]["mean_delta"] == pytest.approx(-.1)
    assert report["by_sport"]["basketball_nba"]["sample_size"] == 2
