import pytest

from app.database import OddsDatabase
from app.simulation_validation import parse_settlements_csv, run_historical_backtest


def _archived_event():
    return {
        "id": "nba-hist-1", "sport_key": "basketball_nba",
        "sport_title": "NBA", "commence_time": "2026-01-02T00:00:00Z",
        "home_team": "Home Club", "away_team": "Away Club",
        "bookmakers": [{
            "key": "book-a", "title": "Book A", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home Club", "point": -4.5, "price": -110},
                    {"name": "Away Club", "point": 4.5, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 220.5, "price": -110},
                    {"name": "Under", "point": 220.5, "price": -110},
                ]},
            ],
        }],
    }


def _settlement():
    return {
        "event_id": "nba-hist-1", "sport_key": "basketball_nba",
        "commence_time": "2026-01-02T00:00:00Z",
        "home_team": "Home Club", "away_team": "Away Club",
        "home_score": 112, "away_score": 105, "status": "final",
        "source": "verified-test-fixture",
    }


def test_historical_backtest_uses_latest_pregame_snapshot_and_is_reproducible(tmp_path):
    database = OddsDatabase(tmp_path / "history.sqlite3")
    event = _archived_event()
    database.save_snapshot("basketball_nba", [event], provider="fixture",
                           fetched_at="2026-01-01T12:00:00+00:00")
    # A later, post-start snapshot must never leak into the pregame prediction.
    changed = _archived_event()
    changed["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = -30.5
    changed["bookmakers"][0]["markets"][0]["outcomes"][1]["point"] = 30.5
    database.save_snapshot("basketball_nba", [changed], provider="fixture",
                           fetched_at="2026-01-02T00:05:00+00:00")

    first = run_historical_backtest(database, [_settlement()])
    second = run_historical_backtest(database, [_settlement()])
    assert first == second
    assert first["events_scored"] == 1
    assert first["events_skipped"] == 0
    assert first["overall"]["sample_size"] == 1
    assert first["market_prior_baseline"]["sample_size"] == 1
    assert first["model_minus_baseline"]["brier_score"] == 0
    assert first["status"] == "DESCRIPTIVE_ONLY"
    assert first["paired_uncertainty"]["sample_size"] == 1
    assert first["paired_uncertainty"]["metrics"]["brier_score"]["ci_95"] is None


def test_historical_backtest_skips_events_without_pregame_quotes(tmp_path):
    database = OddsDatabase(tmp_path / "empty.sqlite3")
    report = run_historical_backtest(database, [_settlement()])
    assert report["events_scored"] == 0
    assert report["events_skipped"] == 1
    assert "strictly before" in report["skipped"][0]["reason"]


def test_historical_backtest_skips_team_identity_mismatch(tmp_path):
    database = OddsDatabase(tmp_path / "history.sqlite3")
    database.save_snapshot("basketball_nba", [_archived_event()], provider="fixture",
                           fetched_at="2026-01-01T12:00:00+00:00")
    settlement = _settlement()
    settlement["home_team"] = "Different Home"
    report = run_historical_backtest(database, [settlement])
    assert report["events_scored"] == 0
    assert "do not match" in report["skipped"][0]["reason"]


def test_backtest_cli_csv_input_contract():
    csv = (
        "event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,status,source\n"
        "g1,basketball_nba,2026-01-01T00:00:00Z,A,B,100,99,final,verified-feed\n"
    )
    assert parse_settlements_csv(csv)[0]["event_id"] == "g1"


def test_fit_league_models_uses_training_only_and_reports_sample_size(tmp_path):
    from app.simulation_validation import fit_league_models

    database = OddsDatabase(tmp_path / "training.sqlite3")
    settlements = []
    for index, (home_score, away_score) in enumerate(((112, 105), (98, 101), (120, 109))):
        event = _archived_event()
        event["id"] = f"nba-train-{index}"
        event["commence_time"] = f"2026-01-0{index + 2}T00:00:00Z"
        database.save_snapshot("basketball_nba", [event], provider="fixture",
                               fetched_at=f"2026-01-0{index + 1}T12:00:00+00:00")
        settlements.append({
            "event_id": event["id"], "sport_key": "basketball_nba",
            "commence_time": event["commence_time"], "home_team": "Home Club",
            "away_team": "Away Club", "home_score": home_score,
            "away_score": away_score, "status": "final", "source": "fixture",
        })
    fit = fit_league_models(database, settlements, min_sample_size=3)
    model = fit["models"]["basketball_nba"]
    assert model["sample_size"] == 3
    assert model["family"] == "basketball"
    assert 0.1 <= model["margin_sd"] <= 50
    assert -0.95 <= model["margin_total_corr"] <= 0.95


def test_fit_league_models_does_not_emit_under_sampled_parameters(tmp_path):
    from app.simulation_validation import fit_league_models
    database = OddsDatabase(tmp_path / "training.sqlite3")
    fit = fit_league_models(database, [], min_sample_size=50)
    assert fit["models"] == {}
    assert fit["training_events_used"] == 0

def test_historical_backtest_skips_rescheduled_event_timestamp_mismatch(tmp_path):
    database = OddsDatabase(tmp_path / "history.sqlite3")
    database.save_snapshot("basketball_nba", [_archived_event()], provider="fixture",
                           fetched_at="2026-01-01T12:00:00+00:00")
    settlement = _settlement()
    settlement["commence_time"] = "2026-01-03T00:00:00Z"
    report = run_historical_backtest(database, [settlement])
    assert report["events_scored"] == 0
    assert "start time does not match" in report["skipped"][0]["reason"]

def test_pregame_replay_selects_latest_snapshot_id_when_timestamps_match(tmp_path):
    from app.simulation import _collect_market_inputs
    from app.simulation_validation import _load_latest_pregame_event
    database = OddsDatabase(tmp_path / "history.sqlite3")
    first = _archived_event()
    second = _archived_event()
    second["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = -6.5
    second["bookmakers"][0]["markets"][0]["outcomes"][1]["point"] = 6.5
    timestamp = "2026-01-01T12:00:00+00:00"
    database.save_snapshot("basketball_nba", [first], provider="fixture", fetched_at=timestamp)
    database.save_snapshot("basketball_nba", [second], provider="fixture", fetched_at=timestamp)
    event, reason = _load_latest_pregame_event(database, _settlement())
    assert reason is None
    assert _collect_market_inputs(event)["spread_margins"] == [6.5]


def test_paired_bootstrap_is_deterministic_and_preserves_event_pairing():
    from app.simulation_validation import paired_bootstrap_comparison

    model = []
    baseline = []
    for event_id, improvement in (("g1", -0.10), ("g2", -0.05), ("g3", 0.02), ("g4", -0.08)):
        model.append({
            "event_id": event_id, "brier_score": 0.2 + improvement,
            "log_loss": 0.5 + improvement, "total_brier_score": 0.3 + improvement,
            "total_log_loss": 0.6 + improvement, "home_score_error": 4 + improvement,
            "away_score_error": 5 + improvement, "total_score_error": 6 + improvement,
            "margin_error": 3 + improvement,
        })
        baseline.append({
            "event_id": event_id, "brier_score": 0.2,
            "log_loss": 0.5, "total_brier_score": 0.3,
            "total_log_loss": 0.6, "home_score_error": 4,
            "away_score_error": 5, "total_score_error": 6, "margin_error": 3,
        })
    first = paired_bootstrap_comparison(model, baseline, iterations=500, seed=7)
    second = paired_bootstrap_comparison(model, baseline, iterations=500, seed=7)
    assert first == second
    assert first["sample_size"] == 4
    assert first["metrics"]["brier_score"]["mean_delta"] < 0
    assert first["metrics"]["brier_score"]["ci_95"][0] <= first["metrics"]["brier_score"]["ci_95"][1]
    assert first["negative_delta_is_better"] is True


def test_paired_bootstrap_rejects_unpaired_event_sets():
    from app.simulation_validation import paired_bootstrap_comparison

    with pytest.raises(ValueError, match="identical event IDs"):
        paired_bootstrap_comparison([{"event_id": "a"}], [{"event_id": "b"}])



def test_paired_bootstrap_reports_separate_league_intervals():
    from app.simulation_validation import paired_bootstrap_comparison

    model = []
    baseline = []
    for sport, event_id, delta in (
        ("basketball_nba", "nba-1", -0.1),
        ("basketball_nba", "nba-2", -0.2),
        ("icehockey_nhl", "nhl-1", 0.1),
        ("icehockey_nhl", "nhl-2", 0.2),
    ):
        row = {"event_id": event_id, "sport": sport}
        base = {"event_id": event_id, "sport": sport}
        for key in ("brier_score", "log_loss", "total_brier_score", "total_log_loss",
                    "home_score_error", "away_score_error", "total_score_error", "margin_error"):
            row[key] = 1.0 + delta
            base[key] = 1.0
        model.append(row)
        baseline.append(base)
    result = paired_bootstrap_comparison(model, baseline, iterations=200, seed=9)
    assert set(result["per_sport"]) == {"basketball_nba", "icehockey_nhl"}
    assert result["per_sport"]["basketball_nba"]["sample_size"] == 2
    assert result["per_sport"]["icehockey_nhl"]["sample_size"] == 2
