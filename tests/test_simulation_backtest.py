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
    assert first["status"] == "DESCRIPTIVE_ONLY"


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
