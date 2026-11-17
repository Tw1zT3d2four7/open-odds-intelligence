from app.database import OddsDatabase


def fixture():
    return [{"id":"evt","sport_title":"Basketball","commence_time":"2026-11-18T00:00:00Z",
        "home_team":"Home","away_team":"Away","bookmakers":[{"key":"book-a","title":"Book A",
        "markets":[{"key":"spreads","outcomes":[{"name":"Home","price":-110,"point":-3.5},
        {"name":"Away","price":-110,"point":3.5}]},{"key":"h2h","outcomes":[
        {"name":"Home","price":-120},{"name":"Away","price":100}]}]}]}]


def test_snapshot_round_trip_and_points(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    events = db.latest_events("basketball_nba")
    assert len(events) == 1
    spread = next(m for m in events[0]["bookmakers"][0]["markets"] if m["key"] == "spreads")
    home = next(o for o in spread["outcomes"] if o["name"] == "Home")
    assert home["point"] == -3.5
    assert db.snapshot_status()["snapshots"] == 1


def test_history_keeps_each_refresh_and_filters_line(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    newer = fixture()
    newer[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -105
    db.save_snapshot("basketball_nba", newer, "test", "2026-11-17T12:05:00Z")
    history = db.history("evt", "spreads", "Home", "book-a", -3.5)
    assert [item["price"] for item in history] == [-105, -110]
    assert db.history("evt", "spreads", "Home", "book-a", -2.5) == []


def test_current_view_uses_latest_snapshot(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    newer = fixture()
    newer[0]["home_team"] = "New Home"
    db.save_snapshot("basketball_nba", newer, "test", "2026-11-17T12:05:00Z")
    assert db.latest_events()[0]["home_team"] == "New Home"
    assert len(db.history("evt", "h2h", "Home", "book-a")) == 2


def test_movement_compares_latest_two_quotes_and_detects_line_change(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    newer = fixture()
    newer[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -105
    newer[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = -4.5
    db.save_snapshot("basketball_nba", newer, "test", "2026-11-17T12:05:00Z")

    movement = db.movement("basketball_nba")
    home_spread = next(x for x in movement if x["market"] == "spreads" and x["outcome"] == "Home")
    assert home_spread["previous_price"] == -110
    assert home_spread["price"] == -105
    assert home_spread["previous_point"] == -3.5
    assert home_spread["point"] == -4.5
    assert home_spread["change_type"] == "line_and_price"
    assert home_spread["implied_probability_change"] < 0


def test_movement_ignores_single_snapshot_quotes(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    assert db.movement() == []
