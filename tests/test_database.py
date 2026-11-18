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



def test_movement_returns_one_open_to_latest_summary_after_many_snapshots(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    prices = [-120, -110, -115, -105]
    for index, price in enumerate(prices):
        current = fixture()
        current[0]["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = price
        db.save_snapshot(
            "basketball_nba", current, "test",
            f"2026-11-17T12:{index * 5:02d}:00Z",
        )

    rows = [row for row in db.movement("basketball_nba")
            if row["market"] == "h2h" and row["outcome"] == "Home"]
    assert len(rows) == 1
    movement = rows[0]
    assert movement["opening_price"] == -120
    assert movement["previous_price"] == -115
    assert movement["price"] == -105
    assert movement["observations"] == 4
    assert movement["price_changes"] == 3
    assert movement["line_changes"] == 0
    assert movement["move_count"] == 3
    assert movement["first_seen"] == "2026-11-17T12:00:00Z"
    assert movement["updated_at"] == "2026-11-17T12:15:00Z"


def test_movement_aggregates_single_line_transitions_without_ladder_confusion(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    for index, point in enumerate([-3.5, -4.0, -4.5]):
        current = fixture()
        current[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = point
        current[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -110 + index * 5
        db.save_snapshot(
            "basketball_nba", current, "test",
            f"2026-11-17T12:{index * 5:02d}:00Z",
        )

    rows = [row for row in db.movement("basketball_nba")
            if row["market"] == "spreads" and row["outcome"] == "Home"]
    assert len(rows) == 1
    assert rows[0]["opening_point"] == -3.5
    assert rows[0]["point"] == -4.5
    assert rows[0]["line_changes"] == 2
    assert rows[0]["price_changes"] == 2
    assert rows[0]["change_type"] == "line_and_price"


def test_movement_ignores_single_snapshot_quotes(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    db.save_snapshot("basketball_nba", fixture(), "test", "2026-11-17T12:00:00Z")
    assert db.movement() == []


def test_alert_fingerprint_is_persistently_deduplicated_and_retryable(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    assert db.claim_alert("fingerprint-1", "ev", "test alert")
    assert not db.claim_alert("fingerprint-1", "ev", "test alert")
    assert db.alert_status()["sent"] == 1
    db.release_alert("fingerprint-1")
    assert db.claim_alert("fingerprint-1", "ev", "test alert")


def test_movement_keeps_alternate_totals_lines_separate(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    old = fixture()
    old[0]["bookmakers"][0]["markets"].append({
        "key": "totals",
        "outcomes": [
            {"name": "Over", "price": -110, "point": 44.5},
            {"name": "Under", "price": -110, "point": 44.5},
            {"name": "Over", "price": 105, "point": 45.5},
            {"name": "Under", "price": -125, "point": 45.5},
        ],
    })
    db.save_snapshot("basketball_nba", old, "test", "2026-11-17T12:00:00Z")

    newer = fixture()
    newer[0]["bookmakers"][0]["markets"].append({
        "key": "totals",
        "outcomes": [
            {"name": "Over", "price": -105, "point": 44.5},
            {"name": "Under", "price": -115, "point": 44.5},
            {"name": "Over", "price": 110, "point": 45.5},
            {"name": "Under", "price": -130, "point": 45.5},
        ],
    })
    db.save_snapshot("basketball_nba", newer, "test", "2026-11-17T12:05:00Z")

    over = [row for row in db.movement("basketball_nba")
            if row["market"] == "totals" and row["outcome"] == "Over"]
    assert len(over) == 2
    assert {row["point"] for row in over} == {44.5, 45.5}
    assert all(row["previous_point"] == row["point"] for row in over)
    assert all(row["change_type"] == "price" for row in over)


def test_movement_retains_full_timeline_and_opening_baseline(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    first = fixture()
    first[0]["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = -120
    db.save_snapshot("basketball_nba", first, "test", "2026-11-17T12:00:00Z")

    second = fixture()
    second[0]["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = -110
    db.save_snapshot("basketball_nba", second, "test", "2026-11-17T12:05:00Z")

    third = fixture()
    third[0]["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = -110
    db.save_snapshot("basketball_nba", third, "test", "2026-11-17T12:10:00Z")

    rows = [x for x in db.movement("basketball_nba")
            if x["market"] == "h2h" and x["outcome"] == "Home"]
    assert len(rows) == 1
    assert rows[0]["opening_price"] == -120
    assert rows[0]["price"] == -110
    assert rows[0]["observations"] == 3
    assert rows[0]["implied_probability_change_from_open"] < 0
    assert rows[0]["direction"] == "drifting"


def test_movement_history_includes_line_transitions_in_chronological_order(tmp_path):
    db = OddsDatabase(tmp_path / "history.sqlite3")
    for index, point in enumerate([-3.5, -4.0, -4.5]):
        current = fixture()
        current[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = point
        current[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -110 + index * 5
        db.save_snapshot(
            "basketball_nba", current, "test",
            f"2026-11-17T12:{index * 5:02d}:00Z",
        )

    history = db.movement_history("evt", "spreads", "Home", "book-a")
    assert [row["point"] for row in history] == [-3.5, -4.0, -4.5]
    assert [row["price"] for row in history] == [-110, -105, -100]


def test_source_bookmaker_timestamp_is_preserved_in_latest_history_and_movement(tmp_path):
    db = OddsDatabase(tmp_path / "timestamps.sqlite3")
    first = fixture()
    first[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-17T11:58:00Z"
    db.save_snapshot("basketball_nba", first, "test", "2026-11-17T12:00:00Z")
    second = fixture()
    second[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -105
    second[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["updated_at"] = "2026-11-17T12:04:00Z"
    db.save_snapshot("basketball_nba", second, "test", "2026-11-17T12:05:00Z")
    markets = db.latest_events("basketball_nba")[0]["bookmakers"][0]["markets"]
    spreads = next(market for market in markets if market["key"] == "spreads")
    outcome = next(item for item in spreads["outcomes"] if item["name"] == "Home")
    assert outcome["updated_at"] == "2026-11-17T12:04:00Z"
    history = db.movement_history("evt", "spreads", "Home", "book-a")
    assert history[-1]["source_updated_at"] == "2026-11-17T12:04:00Z"
    movement = next(x for x in db.movement("basketball_nba")
                    if x["market"] == "spreads" and x["outcome"] == "Home")
    assert movement["timestamp_basis"] == "sportsbook"
    assert movement["source_updated_at"] == "2026-11-17T12:04:00Z"
    assert movement["updated_at"] == "2026-11-17T12:05:00Z"


def test_legacy_quote_table_gets_timestamp_column_migration(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite3"
    db = OddsDatabase(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE quotes DROP COLUMN source_updated_at")
    migrated = OddsDatabase(path)
    with migrated.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quotes)")}
    assert "source_updated_at" in columns



def test_clv_tracks_first_entry_and_resolves_against_last_pregame_peer_consensus(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv.sqlite3")
    now = datetime.now(timezone.utc)
    entry_at = (now - timedelta(minutes=40)).isoformat()
    close_at = (now - timedelta(minutes=10)).isoformat()
    commence = (now - timedelta(minutes=5)).isoformat()

    candidate = {
        "event_id": "clv-event", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence,
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry Book", "bookmaker_key": "entry",
        "price": -105, "fair_probability": 0.50, "ev_percent": 2.38,
    }
    assert db.record_clv_candidates([candidate], entry_at) == 1
    # A later refresh must not overwrite the original entry quote.
    later = dict(candidate, price=-120, fair_probability=0.52, ev_percent=3.0)
    assert db.record_clv_candidates([later], entry_at) == 0

    closing_events = [{
        "id": "clv-event", "sport_title": "Basketball",
        "home_team": "Home", "away_team": "Away",
        "commence_time": commence,
        "bookmakers": [
            {"key": "entry", "title": "Entry Book", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
            {"key": "peer-b", "title": "Peer B", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -130}, {"name": "Away", "price": 110}]}]},
            {"key": "peer-c", "title": "Peer C", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -125}, {"name": "Away", "price": 105}]}]},
        ],
    }]
    db.save_snapshot("basketball_nba", closing_events, "test", close_at)

    # A post-start quote must never be treated as the closing market.
    post_start = [{
        "id": "clv-event", "sport_title": "Basketball",
        "home_team": "Home", "away_team": "Away",
        "commence_time": commence,
        "bookmakers": [
            {"key": "peer-b", "title": "Peer B", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -300}, {"name": "Away", "price": 250}]}]},
            {"key": "peer-c", "title": "Peer C", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -310}, {"name": "Away", "price": 260}]}]},
        ],
    }]
    db.save_snapshot("basketball_nba", post_start, "test",
                     (now + timedelta(minutes=1)).isoformat())

    report = db.clv_report("basketball_nba")
    assert report["summary"]["total"] == 1
    assert report["summary"]["closed"] == 1
    entry = report["entries"][0]
    assert entry["status"] == "closed"
    assert entry["entry_price"] == -105
    assert entry["close_at"] == close_at
    assert entry["closing_books"] == 2
    assert entry["close_fair_probability"] > 0.5
    assert entry["clv_percent"] > 0


def test_clv_does_not_record_quotes_after_commence_time(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-after-start.sqlite3")
    now = datetime.now(timezone.utc)
    candidate = {
        "event_id": "live-event", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": (now - timedelta(minutes=1)).isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Book", "bookmaker_key": "book",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates([candidate], now.isoformat()) == 0



def test_clv_rejects_stale_pregame_snapshot_as_closing_price(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_MAX_CLOSE_AGE_MINUTES", 120)
    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 0)
    db = OddsDatabase(tmp_path / "clv-stale.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(hours=1)
    candidate = {
        "event_id": "stale-event", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    db.record_clv_candidates([candidate], (commence - timedelta(hours=1)).isoformat())
    events = [{
        "id": "stale-event", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -125}, {"name": "Away", "price": 105}]}]},
        ],
    }]
    db.save_snapshot("basketball_nba", events, "test",
                     (commence - timedelta(hours=3)).isoformat())
    report = db.clv_report("basketball_nba")
    assert report["entries"][0]["status"] == "insufficient_close_data"
    assert report["entries"][0]["clv_percent"] is None



def test_player_prop_descriptions_survive_snapshot_and_history(tmp_path):
    db = OddsDatabase(tmp_path / "player-props.sqlite3")
    events = [{
        "id": "props-event", "sport_title": "Basketball",
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [{
            "key": "book-a", "title": "Book A", "markets": [{
                "key": "player_points", "outcomes": [
                    {"name": "Over", "description": "Player One", "point": 24.5, "price": -110},
                    {"name": "Under", "description": "Player One", "point": 24.5, "price": -110},
                    {"name": "Over", "description": "Player Two", "point": 19.5, "price": -105},
                    {"name": "Under", "description": "Player Two", "point": 19.5, "price": -115},
                ],
            }],
        }],
    }]
    db.save_snapshot("basketball_nba", events, "test", "2026-11-18T12:00:00Z")
    stored = db.latest_events("basketball_nba")[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert {item["description"] for item in stored} == {"Player One", "Player Two"}
    history = db.history("props-event", "player_points", "Over", "book-a", 24.5,
                         outcome_description="Player One")
    assert len(history) == 1
    assert history[0]["outcome_description"] == "Player One"


def test_clv_waits_for_delayed_pregame_snapshot_after_kickoff(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 180)
    db = OddsDatabase(tmp_path / "clv-delayed-close.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=10)
    candidate = {
        "event_id": "delayed-event", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    db.record_clv_candidates([candidate], (commence - timedelta(minutes=30)).isoformat())
    first = db.clv_report("basketball_nba")
    assert first["entries"][0]["status"] == "pending"

    events = [{
        "id": "delayed-event", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "entry", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -110}, {"name": "Away", "price": -110}]}]},
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -125}, {"name": "Away", "price": 105}]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
        ],
    }]
    close_at = (commence - timedelta(minutes=1)).isoformat()
    db.save_snapshot("basketball_nba", events, "test", close_at)
    second = db.clv_report("basketball_nba")
    assert second["entries"][0]["status"] == "closed"
    assert second["entries"][0]["close_at"] == close_at


def test_clv_spread_selection_matches_same_side_across_signed_line_conventions(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-spread-side.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "spread-event", "sport": "americanfootball_nfl",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "spreads", "outcome": "Home", "point": -3.5,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    db.record_clv_candidates([candidate], (commence - timedelta(minutes=30)).isoformat())
    events = [{
        "id": "spread-event", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "entry", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": -3.5, "price": -110},
                {"name": "Away", "point": 3.5, "price": -110}]}]},
            {"key": "peer-a", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": 3.5, "price": -120},
                {"name": "Away", "point": -3.5, "price": 100}]}]},
            {"key": "peer-b", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": 3.5, "price": -125},
                {"name": "Away", "point": -3.5, "price": 105}]}]},
        ],
    }]
    db.save_snapshot("americanfootball_nfl", events, "test",
                     (commence - timedelta(minutes=1)).isoformat())
    report = db.clv_report("americanfootball_nfl")
    assert report["entries"][0]["status"] == "closed"
    assert report["entries"][0]["closing_books"] == 2
