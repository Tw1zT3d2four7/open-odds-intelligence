import pytest

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



def test_snapshot_rejects_anonymous_events_and_skips_malformed_nested_records(tmp_path):
    db = OddsDatabase(tmp_path / "identity.sqlite3")
    valid = fixture()[0]
    anonymous = {
        "id": "",
        "bookmakers": valid["bookmakers"],
    }
    partially_malformed = {
        "id": "good-event",
        "sport_title": "Basketball",
        "home_team": "Good Home",
        "away_team": "Good Away",
        "bookmakers": [
            None,
            {"key": "bad-book", "markets": None},
            {"key": "good-book", "markets": [
                None,
                {"key": "h2h", "outcomes": [
                    None,
                    {"name": "Good Home", "price": -110},
                ]},
            ]},
        ],
    }

    db.save_snapshot(
        "basketball_nba",
        [valid, anonymous, None, partially_malformed],
        "test",
        "2026-11-19T12:00:00Z",
    )

    events = db.latest_events("basketball_nba")
    assert {event["id"] for event in events} == {"evt", "good-event"}
    assert all(event["id"] for event in events)
    good = next(event for event in events if event["id"] == "good-event")
    assert good["bookmakers"][0]["key"] == "good-book"
    assert good["bookmakers"][0]["markets"][0]["outcomes"] == [
        {"name": "Good Home", "price": -110}
    ]
    status = db.snapshot_status()
    assert status["events"] == 2



@pytest.mark.parametrize(
    "bad_point",
    [True, False, "3.5", [3.5], {"value": 3.5}, float("nan"), float("inf")],
)
def test_snapshot_discards_malformed_points_without_aborting_valid_quotes(tmp_path, bad_point):
    db = OddsDatabase(tmp_path / "bad-points.sqlite3")
    event = fixture()[0]
    event["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = bad_point

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    assert len(stored) == 1
    spreads = next(m for m in stored[0]["bookmakers"][0]["markets"] if m["key"] == "spreads")
    assert [item["name"] for item in spreads["outcomes"]] == ["Away"]


def test_snapshot_skips_unbindable_optional_quote_metadata(tmp_path):
    db = OddsDatabase(tmp_path / "bad-metadata.sqlite3")
    event = fixture()[0]
    event["bookmakers"][0]["markets"][1]["outcomes"][0]["description"] = {"unexpected": "object"}
    event["bookmakers"][0]["markets"][1]["outcomes"][1]["updated_at"] = ["not", "a", "timestamp"]

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    assert len(stored) == 1
    assert all(m["key"] != "h2h" for m in stored[0]["bookmakers"][0]["markets"])



def test_snapshot_deduplicates_identical_quote_records(tmp_path):
    db = OddsDatabase(tmp_path / "duplicate-identical.sqlite3")
    event = fixture()[0]
    duplicate = dict(event["bookmakers"][0]["markets"][0]["outcomes"][0])
    event["bookmakers"][0]["markets"][0]["outcomes"].append(duplicate)

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    spreads = next(m for m in stored[0]["bookmakers"][0]["markets"] if m["key"] == "spreads")
    assert len(spreads["outcomes"]) == 2
    assert sorted(item["name"] for item in spreads["outcomes"]) == ["Away", "Home"]


def test_snapshot_drops_conflicting_duplicate_quote_identity(tmp_path):
    db = OddsDatabase(tmp_path / "duplicate-conflict.sqlite3")
    event = fixture()[0]
    conflicting = dict(event["bookmakers"][0]["markets"][0]["outcomes"][0], price=-105)
    event["bookmakers"][0]["markets"][0]["outcomes"].append(conflicting)

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    spreads = next(m for m in stored[0]["bookmakers"][0]["markets"] if m["key"] == "spreads")
    assert [item["name"] for item in spreads["outcomes"]] == ["Away"]


def test_snapshot_drops_duplicate_quote_when_event_metadata_conflicts(tmp_path):
    db = OddsDatabase(tmp_path / "duplicate-event-conflict.sqlite3")
    first = fixture()[0]
    second = fixture()[0]
    second["home_team"] = "Different Home"

    db.save_snapshot("basketball_nba", [first, second], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    assert stored == []
    assert db.snapshot_status()["events"] == 1


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

def test_clv_spread_peer_completeness_normalizes_signed_lines_across_books(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-spread-normalized-peers.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "spread-normalized-event", "sport": "americanfootball_nfl",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "spreads", "outcome": "Home", "point": -3.5,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates(
        [candidate], (commence - timedelta(minutes=30)).isoformat()
    ) == 1

    events = [{
        "id": "spread-normalized-event", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": 3.5, "price": -120},
                {"name": "Away", "point": -3.5, "price": 100},
            ]}]},
            {"key": "peer-b", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": -3.5, "price": -125},
                {"name": "Away", "point": 3.5, "price": 105},
            ]}]},
        ],
    }]
    close_at = (commence - timedelta(minutes=1)).isoformat()
    db.save_snapshot("americanfootball_nfl", events, "test", close_at)

    report = db.clv_report("americanfootball_nfl")
    entry = report["entries"][0]
    assert entry["status"] == "closed"
    assert entry["close_at"] == close_at
    assert entry["closing_books"] == 2

def test_clv_never_uses_a_snapshot_at_or_before_the_entry_time(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 0)
    db = OddsDatabase(tmp_path / "clv-pre-entry-snapshot.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    entry_at = commence - timedelta(minutes=10)
    candidate = {
        "event_id": "pre-entry-event", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates([candidate], entry_at.isoformat()) == 1

    events = [{
        "id": "pre-entry-event", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -125}, {"name": "Away", "price": 105}]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
        ],
    }]
    # This snapshot is pregame and recent enough, but it predates the entry.
    db.save_snapshot("basketball_nba", events, "test",
                     (entry_at - timedelta(minutes=1)).isoformat())
    # Even the entry refresh itself is not a closing observation.
    db.save_snapshot("basketball_nba", events, "test", entry_at.isoformat())

    report = db.clv_report("basketball_nba")
    assert report["entries"][0]["status"] == "insufficient_close_data"
    assert report["entries"][0]["close_at"] is None
    assert report["entries"][0]["clv_percent"] is None



def test_clv_soccer_h2h_requires_three_way_market_including_draw(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 0)
    db = OddsDatabase(tmp_path / "clv-soccer-three-way.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "soccer-three-way", "sport": "soccer_epl",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": 150, "fair_probability": 0.4, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates(
        [candidate], (commence - timedelta(minutes=30)).isoformat()
    ) == 1

    # Both peer books omit Draw. Matching two-outcome sets are not a
    # complete soccer 1X2 market and must not produce a close.
    events = [{
        "id": "soccer-three-way", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 150}, {"name": "Away", "price": 180}]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 140}, {"name": "Away", "price": 190}]}]},
        ],
    }]
    db.save_snapshot("soccer_epl", events, "test",
                     (commence - timedelta(minutes=1)).isoformat())
    report = db.clv_report("soccer_epl")
    assert report["entries"][0]["status"] == "insufficient_close_data"
    assert report["entries"][0]["close_fair_probability"] is None


def test_clv_soccer_three_way_fair_probability_uses_all_three_outcomes(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-soccer-fair.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "soccer-fair", "sport": "soccer_epl",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": 150, "fair_probability": 0.4, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates(
        [candidate], (commence - timedelta(minutes=30)).isoformat()
    ) == 1
    events = [{
        "id": "soccer-fair", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 150},
                {"name": "Draw", "price": 220},
                {"name": "Away", "price": 180},
            ]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 140},
                {"name": "Draw", "price": 230},
                {"name": "Away", "price": 190},
            ]}]},
        ],
    }]
    close_at = (commence - timedelta(minutes=1)).isoformat()
    db.save_snapshot("soccer_epl", events, "test", close_at)
    report = db.clv_report("soccer_epl")
    entry = report["entries"][0]
    assert entry["status"] == "closed"
    assert entry["closing_books"] == 2

    def no_vig_home(home, draw, away):
        raw = [100 / (home + 100), 100 / (draw + 100), 100 / (away + 100)]
        return raw[0] / sum(raw)

    expected_a = no_vig_home(150, 220, 180)
    expected_b = no_vig_home(140, 230, 190)
    expected_median = (expected_a + expected_b) / 2
    assert abs(entry["close_fair_probability"] - expected_median) < 1e-6


def test_clv_sport_filter_uses_canonical_sport_key(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-sport-filter.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now + timedelta(hours=2)
    candidate = {
        "event_id": "sport-filter-event", "sport": "Soccer - English Premier League",
        "sport_key": "soccer_epl",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Book", "bookmaker_key": "book",
        "price": 150, "fair_probability": 0.4, "ev_percent": 2.0,
    }
    assert db.record_clv_candidates([candidate], now.isoformat()) == 1
    assert db.clv_report("soccer_epl")["summary"]["total"] == 1
    assert db.clv_report("Soccer - English Premier League")["summary"]["total"] == 0


def test_database_migrates_legacy_clv_display_sport_to_canonical_key(tmp_path):
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "clv-sport-migration.sqlite3"
    db = OddsDatabase(path)
    now = datetime.now(timezone.utc)
    commence = now + timedelta(hours=2)
    commence_text = commence.isoformat()
    events = [{
        "id": "legacy-sport-event", "home_team": "Home", "away_team": "Away",
        "commence_time": commence_text,
        "bookmakers": [{
            "key": "book", "title": "Book", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": 150}, {"name": "Away", "price": -170},
                    {"name": "Draw", "price": 220},
                ],
            }],
        }],
    }]
    db.save_snapshot("soccer_epl", events, "test", now.isoformat())
    legacy_candidate = {
        "event_id": "legacy-sport-event",
        "sport": "Soccer - English Premier League",
        "matchup": "Away @ Home", "commence_time": commence_text,
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Book", "bookmaker_key": "book",
        "price": 150, "fair_probability": 0.4, "ev_percent": 2.0,
    }
    assert db.record_clv_candidates([legacy_candidate], now.isoformat()) == 1

    migrated = OddsDatabase(path)
    report = migrated.clv_report("soccer_epl")
    assert report["summary"]["total"] == 1
    assert report["entries"][0]["sport"] == "soccer_epl"


def test_clv_rejects_tied_conflicting_peer_market_outcome_sets(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 0)
    db = OddsDatabase(tmp_path / "clv-conflicting-peer-sets.sqlite3")
    now = datetime.now(timezone.utc)
    commence = now - timedelta(minutes=5)
    entry_at = commence - timedelta(minutes=30)
    candidate = {
        "event_id": "conflicting-peer-sets", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry", "bookmaker_key": "entry",
        "price": -110, "fair_probability": 0.5, "ev_percent": 1.0,
    }
    assert db.record_clv_candidates([candidate], entry_at.isoformat()) == 1
    events = [{
        "id": "conflicting-peer-sets", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "peer-a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Away", "price": 100}]}]},
            {"key": "peer-b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -125}, {"name": "Away", "price": 105}]}]},
            {"key": "peer-c", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -115}, {"name": "Visitor", "price": 100}]}]},
            {"key": "peer-d", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Visitor", "price": 105}]}]},
        ],
    }]
    db.save_snapshot("basketball_nba", events, "test",
                     (commence - timedelta(minutes=1)).isoformat())
    report = db.clv_report("basketball_nba")
    assert report["entries"][0]["status"] == "insufficient_close_data"
    assert report["entries"][0]["close_fair_probability"] is None


def test_clv_spread_entry_deduplicates_opposite_signed_points(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-spread-entry-sign.sqlite3")
    now = datetime.now(timezone.utc)
    candidate = {
        "event_id": "spread-entry-sign", "sport": "americanfootball_nfl",
        "matchup": "Away @ Home", "commence_time": (now + timedelta(hours=2)).isoformat(),
        "market": "spreads", "outcome": "Home", "point": -3.5,
        "bookmaker": "Book", "bookmaker_key": "book",
        "price": -110, "fair_probability": 0.5, "ev_percent": 2.0,
    }
    assert db.record_clv_candidates([candidate], now.isoformat()) == 1
    reversed_convention = {**candidate, "point": 3.5, "price": -105}
    assert db.record_clv_candidates([reversed_convention], (now + timedelta(minutes=1)).isoformat()) == 0
    report = db.clv_report("americanfootball_nfl")
    assert report["summary"]["total"] == 1
    assert report["entries"][0]["point"] == -3.5


def test_odds_database_uses_wal_for_concurrent_refresh_work(tmp_path):
    db = OddsDatabase(tmp_path / "wal.sqlite3")
    with db.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_snapshot_rejects_boolean_fractional_and_string_prices(tmp_path):
    db = OddsDatabase(tmp_path / "strict-prices.sqlite3")
    events = [{
        "id": "strict-price-event", "sport_title": "Basketball",
        "commence_time": "2026-11-20T00:00:00Z",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [{
            "key": "book", "title": "Book", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home", "price": True},
                    {"name": "Away", "price": 100.5},
                    {"name": "Draw", "price": "220"},
                    {"name": "Valid", "price": -110},
                ],
            }],
        }],
    }]
    db.save_snapshot("basketball_nba", events, "test", "2026-11-19T12:00:00Z")
    saved = db.latest_events("basketball_nba")
    outcomes = saved[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert [(item["name"], item["price"]) for item in outcomes] == [("Valid", -110)]


@pytest.mark.parametrize("invalid_price", [-99, -50, -1, 0, 1, 50, 99])
def test_snapshot_rejects_invalid_american_price_range(tmp_path, invalid_price):
    db = OddsDatabase(tmp_path / f"invalid-price-{invalid_price}.sqlite3")
    event = fixture()[0]
    event["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = invalid_price

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    h2h = next(m for m in stored[0]["bookmakers"][0]["markets"] if m["key"] == "h2h")
    assert [outcome["name"] for outcome in h2h["outcomes"]] == ["Away"]


@pytest.mark.parametrize("valid_price", [-100, 100])
def test_snapshot_accepts_american_odds_boundary_prices(tmp_path, valid_price):
    db = OddsDatabase(tmp_path / f"valid-price-{valid_price}.sqlite3")
    event = fixture()[0]
    event["bookmakers"][0]["markets"][1]["outcomes"][0]["price"] = valid_price

    db.save_snapshot("basketball_nba", [event], "test", "2026-11-19T12:00:00Z")

    stored = db.latest_events("basketball_nba")
    h2h = next(m for m in stored[0]["bookmakers"][0]["markets"] if m["key"] == "h2h")
    home = next(outcome for outcome in h2h["outcomes"] if outcome["name"] == "Home")
    assert home["price"] == valid_price


def test_snapshot_rejects_non_scalar_event_ids_but_accepts_integer_zero(tmp_path):
    db = OddsDatabase(tmp_path / "malformed-event-identities.sqlite3")
    valid = fixture()[0]
    zero_id = fixture()[0]
    zero_id["id"] = 0
    malformed = []
    for event_id in (True, False, [], ["nested"], {"unexpected": "object"}):
        event = fixture()[0]
        event["id"] = event_id
        malformed.append(event)

    db.save_snapshot(
        "basketball_nba", [valid, zero_id, *malformed],
        "test", "2026-11-19T12:00:00Z",
    )

    assert {event["id"] for event in db.latest_events("basketball_nba")} == {"evt", "0"}
    assert db.snapshot_status()["events"] == 2


def test_clv_candidate_ingestion_rejects_invalid_american_prices(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-invalid-entry.sqlite3")
    now = datetime.now(timezone.utc)
    candidate = {
        "event_id": "clv-invalid", "sport": "basketball_nba",
        "commence_time": (now + timedelta(hours=1)).isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry Book", "bookmaker_key": "entry",
        "price": -99, "fair_probability": 0.5, "ev_percent": 2.0,
    }
    assert db.record_clv_candidates(
        [candidate, None, dict(candidate, price=110.5), dict(candidate, price=True)],
        now.isoformat(),
    ) == 0
    assert db.clv_report("basketball_nba")["summary"]["total"] == 0


def test_clv_skips_corrupt_legacy_closing_prices_without_crashing(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import config

    monkeypatch.setattr(config, "CLV_CLOSE_GRACE_MINUTES", 0)
    db = OddsDatabase(tmp_path / "clv-corrupt-close.sqlite3")
    now = datetime.now(timezone.utc)
    entry_at = now - timedelta(minutes=40)
    close_at = now - timedelta(minutes=10)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "clv-corrupt", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry Book", "bookmaker_key": "entry",
        "price": -105, "fair_probability": 0.5, "ev_percent": 2.38,
    }
    assert db.record_clv_candidates([candidate], entry_at.isoformat()) == 1
    closing = [{
        "id": "clv-corrupt", "sport_title": "Basketball",
        "home_team": "Home", "away_team": "Away",
        "commence_time": commence.isoformat(),
        "bookmakers": [
            {"key": "entry", "title": "Entry", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -120}, {"name": "Away", "price": 100},
            ]}]},
            {"key": "peer-b", "title": "Peer B", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -130}, {"name": "Away", "price": 110},
            ]}]},
            {"key": "peer-c", "title": "Peer C", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": -125}, {"name": "Away", "price": 105},
            ]}]},
        ],
    }]
    db.save_snapshot("basketball_nba", closing, "test", close_at.isoformat())
    with db.connect() as conn:
        conn.execute(
            "UPDATE quotes SET price=99 WHERE event_id=? AND bookmaker_key=?",
            ("clv-corrupt", "peer-b"),
        )

    report = db.clv_report("basketball_nba")
    assert report["summary"]["total"] == 1
    assert report["entries"][0]["status"] == "insufficient_close_data"
    assert report["entries"][0]["clv_percent"] is None


def test_clv_uses_latest_pregame_observation_time_not_insert_order(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-out-of-order.sqlite3")
    now = datetime.now(timezone.utc)
    entry_at = now - timedelta(minutes=60)
    latest_close_at = now - timedelta(minutes=10)
    older_close_at = now - timedelta(minutes=20)
    commence = now - timedelta(minutes=5)
    candidate = {
        "event_id": "clv-out-of-order", "sport": "basketball_nba",
        "matchup": "Away @ Home", "commence_time": commence.isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry Book", "bookmaker_key": "entry",
        "price": -105, "fair_probability": 0.5, "ev_percent": 2.38,
    }
    assert db.record_clv_candidates([candidate], entry_at.isoformat()) == 1

    def closing_snapshot(home_b, away_b, home_c, away_c):
        return [{
            "id": "clv-out-of-order", "sport_title": "Basketball",
            "home_team": "Home", "away_team": "Away",
            "commence_time": commence.isoformat(),
            "bookmakers": [
                {"key": "entry", "title": "Entry", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": -120}, {"name": "Away", "price": 100},
                ]}]},
                {"key": "peer-b", "title": "Peer B", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": home_b}, {"name": "Away", "price": away_b},
                ]}]},
                {"key": "peer-c", "title": "Peer C", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": home_c}, {"name": "Away", "price": away_c},
                ]}]},
            ],
        }]

    # Insert the newer observation first, then insert an older fetched_at
    # timestamp with a larger SQLite ID. The later market observation must win.
    db.save_snapshot(
        "basketball_nba", closing_snapshot(-150, 130, -145, 125),
        "test", latest_close_at.isoformat(),
    )
    db.save_snapshot(
        "basketball_nba", closing_snapshot(-130, 110, -125, 105),
        "test", older_close_at.isoformat(),
    )

    report = db.clv_report("basketball_nba")
    entry = report["entries"][0]
    assert entry["status"] == "closed"
    assert entry["close_at"] == latest_close_at.isoformat()


def test_clv_candidate_ingestion_skips_malformed_metadata_and_nonfinite_values(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = OddsDatabase(tmp_path / "clv-candidate-validation.sqlite3")
    now = datetime.now(timezone.utc)
    base = {
        "event_id": "candidate-event", "sport": "basketball_nba",
        "matchup": "Away @ Home",
        "commence_time": (now + timedelta(hours=1)).isoformat(),
        "market": "h2h", "outcome": "Home", "point": None,
        "bookmaker": "Entry Book", "bookmaker_key": "entry",
        "price": -105, "fair_probability": 0.52, "ev_percent": 2.38,
    }
    malformed = [
        None,
        dict(base, event_id=[]),
        dict(base, market=[]),
        dict(base, outcome={}),
        dict(base, bookmaker_key=["entry"]),
        dict(base, point=[]),
        dict(base, fair_probability=True),
        dict(base, fair_probability=float("nan")),
        dict(base, fair_probability=1.0),
        dict(base, ev_percent=False),
        dict(base, ev_percent=float("inf")),
        dict(base, commence_time="not-a-timestamp"),
    ]
    assert db.record_clv_candidates(malformed, now.isoformat()) == 0
    assert db.record_clv_candidates([base], now.isoformat()) == 1
    # The configured CLV threshold may be negative; finite negative EV is
    # valid candidate data when an operator deliberately lowers that gate.
    assert db.record_clv_candidates(
        [dict(base, event_id="negative-ev", ev_percent=-0.5)], now.isoformat()
    ) == 1
    assert db.record_clv_candidates(None, now.isoformat()) == 0
    assert db.record_clv_candidates([base], "invalid-observed-time") == 0
    assert db.clv_report("basketball_nba")["summary"]["total"] == 2


def test_latest_events_paginates_before_loading_quote_details(tmp_path):
    db = OddsDatabase(tmp_path / "paged.sqlite3")
    nba = []
    for index in range(4):
        event = fixture()[0]
        event["id"] = f"nba-{index}"
        event["commence_time"] = f"2026-11-18T0{index}:00:00Z"
        nba.append(event)
    ncaaf = []
    for index in range(3):
        event = fixture()[0]
        event["id"] = f"ncaaf-{index}"
        event["commence_time"] = f"2026-11-18T1{index}:00:00Z"
        ncaaf.append(event)
    db.save_snapshot("basketball_nba", nba, "test", "2026-11-17T12:00:00Z")
    db.save_snapshot("americanfootball_ncaaf", ncaaf, "test", "2026-11-17T12:00:00Z")

    first = db.latest_events(limit=3, offset=0)
    second = db.latest_events(limit=3, offset=3)
    all_ids = [event["id"] for event in first + second]

    assert len(first) == 3
    assert len(second) == 3
    assert len(set(all_ids)) == 6
    assert set(all_ids).issubset({
        "nba-0", "nba-1", "nba-2", "nba-3",
        "ncaaf-0", "ncaaf-1", "ncaaf-2",
    })


def test_latest_events_paginates_selected_sport(tmp_path):
    db = OddsDatabase(tmp_path / "sport-paged.sqlite3")
    events = []
    for index in range(4):
        event = fixture()[0]
        event["id"] = f"nba-{index}"
        event["commence_time"] = f"2026-11-18T0{index}:00:00Z"
        events.append(event)
    db.save_snapshot("basketball_nba", events, "test", "2026-11-17T12:00:00Z")

    first = db.latest_events("basketball_nba", limit=2, offset=0)
    second = db.latest_events("basketball_nba", limit=2, offset=2)

    assert [event["id"] for event in first] == ["nba-0", "nba-1"]
    assert [event["id"] for event in second] == ["nba-2", "nba-3"]



def test_snapshot_status_counts_distinct_books_in_latest_snapshot_per_sport(tmp_path):
    db = OddsDatabase(tmp_path / "book-count.sqlite3")
    first = fixture()
    first[0]["bookmakers"].append({
        "key": "book-b", "title": "Book B",
        "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": -115}, {"name": "Away", "price": 105}
        ]}],
    })
    db.save_snapshot("basketball_nba", first, "test", "2026-11-17T12:00:00Z")
    newer = fixture()
    db.save_snapshot("basketball_nba", newer, "test", "2026-11-17T12:05:00Z")
    status = db.snapshot_status()
    assert status["bookmakers"] == 1
