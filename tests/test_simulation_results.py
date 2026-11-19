from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import pytest

from app.database import OddsDatabase
from app.providers.therundown import TheRundownProvider


def _score_payload(status="STATUS_FINAL", home_score=2, away_score=1):
    return {"events": [{
        "event_id": "rundown-event-1",
        "event_date": "2026-01-02T23:30:00Z",
        "teams": [
            {"team_id": 10, "name": "Home City Home Club", "mascot": "Home Club", "is_home": True},
            {"team_id": 11, "name": "Away City Away Club", "mascot": "Away Club", "is_away": True},
        ],
        "score": {
            "event_status": status, "score_home": home_score, "score_away": away_score,
            "team_id_home": 10, "team_id_away": 11, "updated_at": "2026-01-03T01:00:00Z",
        },
    }]}


def test_therundown_scores_only_returns_verified_final_scores_and_zero_price_request(monkeypatch):
    provider = TheRundownProvider("test-key", utc_offset_minutes=-300)
    requested = []
    monkeypatch.setattr(provider, "_request", lambda url: requested.append(url) or _score_payload(home_score=0, away_score=1))
    rows = provider.fetch_scores("soccer_epl", ["2026-01-02"])
    assert len(rows) == 1
    assert rows[0]["event_id"] == "rundown-event-1"
    assert rows[0]["home_score"] == 0
    assert rows[0]["away_score"] == 1
    assert rows[0]["home_team"] == "Home City Home Club"
    query = parse_qs(urlparse(requested[0]).query)
    assert query["affiliate_ids"] == ["0"]
    assert query["offset"] == ["-300"]


def test_therundown_scores_only_ignores_non_final_and_incomplete_scores(monkeypatch):
    provider = TheRundownProvider("test-key")
    monkeypatch.setattr(provider, "_request", lambda url: _score_payload(status="STATUS_IN_PROGRESS"))
    assert provider.fetch_scores("soccer_epl", ["2026-01-02"]) == []
    monkeypatch.setattr(provider, "_request", lambda url: _score_payload(home_score=None))
    assert provider.fetch_scores("soccer_epl", ["2026-01-02"]) == []
    monkeypatch.setattr(provider, "_request", lambda url: _score_payload(home_score=1.5))
    assert provider.fetch_scores("soccer_epl", ["2026-01-02"]) == []


def test_therundown_scores_only_rejects_invalid_dates():
    provider = TheRundownProvider("test-key")
    with pytest.raises(Exception, match="Invalid score lookup date"):
        provider.fetch_scores("soccer_epl", ["yesterday"])


def _odds_event():
    return {
        "id": "rundown-event-1", "sport_title": "EPL",
        "commence_time": "2026-01-02T23:30:00Z",
        "home_team": "Home City Home Club", "away_team": "Away City Away Club",
        "bookmakers": [{"key": "book", "title": "Book", "markets": [{
            "key": "totals", "outcomes": [
                {"name": "Over", "point": 2.5, "price": -110},
                {"name": "Under", "point": 2.5, "price": -110},
            ],
        }]}],
    }


def _result():
    return {
        "event_id": "rundown-event-1", "sport_key": "soccer_epl",
        "commence_time": "2026-01-02T23:30:00Z",
        "home_team": "Home City Home Club", "away_team": "Away City Away Club",
        "home_score": 0, "away_score": 1, "status": "final",
        "source": "therundown", "source_record_id": "rundown-event-1",
        "source_updated_at": "2026-01-03T01:00:00Z",
    }


def test_database_stores_only_matching_final_scores_once(tmp_path):
    database = OddsDatabase(tmp_path / "results.sqlite3")
    database.save_snapshot("soccer_epl", [_odds_event()], provider="fixture",
                           fetched_at="2026-01-02T12:00:00+00:00")
    first = database.save_simulation_settlements([_result()])
    second = database.save_simulation_settlements([_result()])
    assert first["inserted"] == 1
    assert second["duplicates"] == 1
    saved = database.simulation_settlements()
    assert len(saved) == 1
    assert saved[0]["home_score"] == 0


def test_database_rejects_result_team_and_start_mismatches(tmp_path):
    database = OddsDatabase(tmp_path / "results.sqlite3")
    database.save_snapshot("soccer_epl", [_odds_event()], provider="fixture",
                           fetched_at="2026-01-02T12:00:00+00:00")
    wrong_team = _result()
    wrong_team["home_team"] = "Unrelated Team"
    wrong_time = _result()
    wrong_time["commence_time"] = "2026-01-04T23:30:00Z"
    saved = database.save_simulation_settlements([wrong_team, wrong_time])
    assert saved["inserted"] == 0
    assert len(saved["rejected"]) == 2


def test_pending_result_dates_use_archive_and_provider_offset(tmp_path):
    database = OddsDatabase(tmp_path / "results.sqlite3")
    database.save_snapshot("soccer_epl", [_odds_event()], provider="fixture",
                           fetched_at="2026-01-02T12:00:00+00:00")
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    pending = database.pending_simulation_result_dates(["soccer_epl"], now=now,
                                                        lookback_days=7, offset_minutes=120)
    assert pending == [{"sport": "soccer_epl", "event_date": "2026-01-03", "pending_events": 1}]
    database.save_simulation_settlements([_result()])
    assert database.pending_simulation_result_dates(["soccer_epl"], now=now,
                                                    lookback_days=7, offset_minutes=120) == []