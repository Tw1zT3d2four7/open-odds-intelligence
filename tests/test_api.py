import json
import pytest

from fastapi.testclient import TestClient

from app import main


def _api_independent_models(sport_key, home="Home", away="Away"):
    from app.team_model import fit_team_strength_models, fit_independent_outcome_models
    from app.simulation import _family
    rows = []
    for index in range(8):
        rows.append({
            "sport_key": sport_key, "home_team": home, "away_team": away,
            "commence_time": f"2026-01-{index + 1:02d}T00:00:00Z",
            "home_score": 100 + index + (5 if index % 2 == 0 else 0),
            "away_score": 90 + index,
        })
    family = _family(sport_key)
    if family in ("tennis", "mma"):
        fitted = fit_independent_outcome_models(rows, min_team_matches=4)["models"][sport_key]
        key = "independent_outcome_model"
    else:
        fitted = fit_team_strength_models(rows, min_team_games=4)["models"][sport_key]
        key = "independent_team_model"
    return {sport_key: {
        "sport_key": sport_key, "family": family,
        "sample_size": 8, key: fitted,
    }}


def test_manual_refresh_endpoint_requires_admin_and_returns_result(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "CaseSensitive-Admin")
    monkeypatch.setattr(main, "_reserve_manual_refresh_slot", lambda: {"allowed": True})
    received = {}

    def fake_refresh(sport=None):
        received["sport"] = sport
        return {"ok": True, "events": 12}

    monkeypatch.setattr(main.service, "refresh", fake_refresh)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:CaseSensitive-Admin").decode()}
    with TestClient(main.app) as client:
        denied = client.post("/api/refresh?sport=basketball_nba")
        response = client.post("/api/refresh?sport=basketball_nba", headers=headers)

    assert denied.status_code == 401
    assert response.status_code == 200
    assert received["sport"] == "basketball_nba"
    assert response.json()["events"] == 12


def test_public_health_endpoint_does_not_expose_provider_or_api_diagnostics(monkeypatch):
    monkeypatch.setattr(main.service, "status", lambda: {
        "provider": "therundown",
        "providers": ["therundown", "oddize"],
        "provider_keys": {"therundown": {"keys": [{"key_id": "secret-fingerprint"}]}},
        "provider_usage": {"datapoints_remaining": 12},
        "auto_refresh_schedule": {"therundown": 14400},
        "last_updated": "2026-11-22T12:00:00Z",
        "sports": ["basketball_nba"],
        "data_freshness": "current",
        "sport_freshness": [{"sport": "basketball_nba", "freshness": "current"}],
        "live_data_available": True,
    })
    with TestClient(main.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["sports"] == ["basketball_nba"]
    assert payload["data_freshness"] == "current"
    assert "providers" not in payload
    assert "provider_keys" not in payload
    assert "provider_usage" not in payload
    assert "auto_refresh_schedule" not in payload


def test_movement_history_endpoint_returns_all_observed_lines(monkeypatch):
    received = {}

    def fake_history(event_id, market, outcome, bookmaker, limit):
        received.update(event_id=event_id, market=market, outcome=outcome,
                        bookmaker=bookmaker, limit=limit)
        return [
            {"fetched_at": "2026-11-17T12:00:00Z", "point": -3.5, "price": -110},
            {"fetched_at": "2026-11-17T12:05:00Z", "point": -4.0, "price": -105},
        ]

    monkeypatch.setattr(main.service, "movement_history", fake_history)
    with TestClient(main.app) as client:
        response = client.get(
            "/api/movement/history?event_id=evt&market=spreads&outcome=Home&bookmaker=book-a"
        )

    assert response.status_code == 200
    assert received == {
        "event_id": "evt", "market": "spreads", "outcome": "Home",
        "bookmaker": "book-a", "limit": 500,
    }
    assert [quote["point"] for quote in response.json()["quotes"]] == [-3.5, -4.0]


def test_dashboard_renders_synthetic_minus_one_builder_inputs_and_outputs():
    with TestClient(main.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    html = response.text
    for element_id in (
        "calc-synthetic", "synMlTeam", "synMlOpp", "synHcapTeam",
        "synHcapOpp", "synFairOdds", "synCoverProb", "synPushProb",
        "synLossProb", "synError",
    ):
        assert f'id="{element_id}"' in html
    assert "const fair=1+loss/cover;" in html
    assert 'if(!Number.isInteger(v))throw Error("American odds must be a whole number.")' in html
    assert 'if(!Number.isFinite(raw))throw Error("American odds exceed the supported numeric range.")' in html
    assert 'const finiteResult=(v,label)=>{if(!Number.isFinite(v))throw Error(label+" exceeds the supported numeric range.");return v;}' in html
    assert 'o.title=g.reason||""' in html
    assert ' · NOT READY' in html
    assert 'if(v>-100&&v<100)throw Error("American odds must be +100 or greater, or -100 or lower.")' in html
    assert 'if(f<0||f>1)throw Error("Kelly fraction must be between 0 and 1.")' in html


def test_market_board_endpoint_applies_sport_filter(monkeypatch):
    received = {}
    def fake_events(sport=None):
        received["sport"] = sport
        return []
    monkeypatch.setattr(main.service, "events", fake_events)
    with TestClient(main.app) as client:
        response = client.get("/api/market-board?sport=icehockey_nhl")
    assert response.status_code == 200
    assert received["sport"] == "icehockey_nhl"
    assert response.json()["quotes"] == []


def test_opportunities_endpoint_passes_screening_controls(monkeypatch):
    received = {}
    def fake_events(sport=None):
        received["sport"] = sport
        return [{"id": "event"}]
    def fake_analysis(events, min_ev, min_consensus_books, include_arbitrage=True):
        received.update(events=events, min_ev=min_ev, min_consensus_books=min_consensus_books,
                        include_arbitrage=include_arbitrage)
        return {"opportunities": [{"event_id": "event"}], "arbitrage": []}
    monkeypatch.setattr(main.service, "events", fake_events)
    monkeypatch.setattr(main, "analyze_markets", fake_analysis)
    with TestClient(main.app) as client:
        response = client.get("/api/opportunities?sport=basketball_nba&min_ev=2.5&min_consensus_books=3")
    assert response.status_code == 200
    assert received == {
        "sport": "basketball_nba", "events": [{"id": "event"}],
        "min_ev": 2.5, "min_consensus_books": 3, "include_arbitrage": False,
    }
    assert response.json()["opportunities"] == [{"event_id": "event"}]


def test_arbitrage_endpoint_requests_arbitrage_analysis(monkeypatch):
    received = {}
    monkeypatch.setattr(main.service, "events", lambda sport=None: [{"id": sport}])
    def fake_analysis(events, min_ev, include_arbitrage=True):
        received.update(events=events, min_ev=min_ev, include_arbitrage=include_arbitrage)
        return {"opportunities": [], "arbitrage": [{"event_id": "event", "arb_percent": 1.2}]}
    monkeypatch.setattr(main, "analyze_markets", fake_analysis)
    with TestClient(main.app) as client:
        response = client.get("/api/arbitrage?sport=baseball_mlb")
    assert response.status_code == 200
    assert received == {"events": [{"id": "baseball_mlb"}], "min_ev": 101, "include_arbitrage": True}
    assert response.json()["arbitrage"][0]["arb_percent"] == 1.2


def test_clv_endpoint_passes_sport_and_bounded_limit(monkeypatch):
    received = {}
    def fake_report(sport, limit):
        received.update(sport=sport, limit=limit)
        return {"entries": [], "summary": {"total": 0}}
    monkeypatch.setattr(main.service, "clv_report", fake_report)
    with TestClient(main.app) as client:
        response = client.get("/api/clv?sport=americanfootball_nfl&limit=1200")
    assert response.status_code == 200
    assert received == {"sport": "americanfootball_nfl", "limit": 1200}
    assert response.json()["summary"]["total"] == 0


def test_clv_endpoint_rejects_out_of_range_limit():
    with TestClient(main.app) as client:
        response = client.get("/api/clv?limit=9000")
    assert response.status_code == 422


def test_limits_endpoints_expose_observations_and_changes(monkeypatch):
    monkeypatch.setattr(main.limits_monitor, "current", lambda sport, limit: [{"sport": sport, "limit": limit}])
    monkeypatch.setattr(main.limits_monitor, "status", lambda: {"enabled": True})
    monkeypatch.setattr(main.limits_monitor, "changes", lambda sport, direction, limit: [
        {"sport": sport, "direction": direction, "limit": limit}
    ])
    monkeypatch.setattr(main.limits_monitor, "history", lambda event_id, bookmaker, market, outcome, point, limit: [
        {"event_id": event_id, "bookmaker": bookmaker, "market": market, "outcome": outcome,
         "point": point, "limit": limit}
    ])
    with TestClient(main.app) as client:
        current = client.get("/api/limits?sport=americanfootball_nfl&limit=25")
        changes = client.get("/api/limits/changes?sport=americanfootball_nfl&direction=all&limit=30")
        history = client.get(
            "/api/limits/history?event_id=e1&bookmaker=circa&market=spreads&outcome=Home&point=-2.5&limit=40"
        )
    assert current.status_code == changes.status_code == history.status_code == 200
    assert current.json()["limits"] == [{"sport": "americanfootball_nfl", "limit": 25}]
    assert changes.json()["changes"] == [{"sport": "americanfootball_nfl", "direction": "all", "limit": 30}]
    assert history.json()["observations"][0]["point"] == -2.5


def test_limits_changes_rejects_invalid_direction():
    with TestClient(main.app) as client:
        response = client.get("/api/limits/changes?direction=up")
    assert response.status_code == 422


def test_player_props_endpoint_preserves_player_identity(monkeypatch):
    event = {
        "id": "event-1", "sport_title": "Basketball", "commence_time": "2026-11-20T00:00:00Z",
        "away_team": "Away", "home_team": "Home",
        "bookmakers": [{
            "key": "book", "title": "Book",
            "markets": [{
                "key": "player_points",
                "outcomes": [
                    {"name": "Over", "description": "Player A", "point": 20.5, "price": -110},
                    {"name": "Over", "description": "Player B", "point": 20.5, "price": -105},
                ],
            }],
        }],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {"player_props": {"enabled": True}})
    with TestClient(main.app) as client:
        response = client.get("/api/player-props?sport=basketball_nba")
    assert response.status_code == 200
    quotes = response.json()["quotes"]
    assert len(quotes) == 2
    assert {row["player"] for row in quotes} == {"Player A", "Player B"}


def test_simulation_game_list_is_scoped_to_selected_configured_league(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {
        "id": "nba-game", "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [{"markets": [{"key": "spreads"}, {"key": "totals"}]}],
    }
    received = {}
    def fake_events(sport=None):
        received["sport"] = sport
        return [event]
    monkeypatch.setattr(main.service, "events", fake_events)
    with TestClient(main.app) as client:
        response = client.get("/api/simulation/games?sport=basketball_nba")
        invalid = client.get("/api/simulation/games?sport=americanfootball_nfl")
    assert response.status_code == 200
    assert received["sport"] == "basketball_nba"
    assert response.json()["games"][0]["event_id"] == "nba-game"
    assert invalid.status_code == 400



def test_simulation_game_list_disables_games_when_league_snapshot_is_stale(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {
        "id": "nba-game", "sport_key": "basketball_nba",
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2026-11-20T00:00:00Z",
        "bookmakers": [
            {"key": "book-a", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": -3.5}, {"name": "Away", "point": 3.5}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 220.5}, {"name": "Under", "point": 220.5}]},
            ]},
        ],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": "basketball_nba", "freshness": "stale", "age_seconds": 21601}]
    })
    with TestClient(main.app) as client:
        response = client.get("/api/simulation/games?sport=basketball_nba")

    assert response.status_code == 200
    game = response.json()["games"][0]
    assert game["spread_books"] == 1
    assert game["total_books"] == 1
    assert game["snapshot_freshness"] == "stale"
    assert game["snapshot_age_seconds"] == 21601
    assert game["can_simulate"] is False
    assert "refresh this league" in game["reason"]


def test_simulation_game_list_marks_unavailable_snapshot_not_ready(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {
        "id": "nba-game", "home_team": "Home", "away_team": "Away",
        "bookmakers": [{"key": "book", "markets": [
            {"key": "spreads", "outcomes": [
                {"name": "Home", "point": -3.5}, {"name": "Away", "point": 3.5}]},
            {"key": "totals", "outcomes": [
                {"name": "Over", "point": 220.5}, {"name": "Under", "point": 220.5}]},
        ]}],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {"sport_freshness": []})
    with TestClient(main.app) as client:
        response = client.get("/api/simulation/games?sport=basketball_nba")
    game = response.json()["games"][0]
    assert game["snapshot_freshness"] == "unavailable"
    assert game["can_simulate"] is False


def test_simulation_endpoint_runs_only_the_requested_event(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    selected = {
        "id": "chosen-game", "sport_key": "basketball_nba",
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": -3.5}, {"name": "Away", "point": 3.5}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 220.5}, {"name": "Under", "point": 220.5}]},
            ]}
        ],
    }
    other = dict(selected, id="not-selected")
    monkeypatch.setattr(main.service, "events", lambda sport=None: [selected, other])
    monkeypatch.setattr(main, "load_fitted_models", lambda path: _api_independent_models("basketball_nba"))
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": "basketball_nba", "freshness": "current", "age_seconds": 60}]
    })
    with TestClient(main.app) as client:
        response = client.post("/api/simulation/run?sport=basketball_nba&event_id=chosen-game")
        missing = client.post("/api/simulation/run?sport=basketball_nba&event_id=missing")
    assert response.status_code == 200
    assert response.json()["event_id"] == "chosen-game"
    assert response.json()["trials"] == 10_000
    assert missing.status_code == 404



def test_simulation_endpoint_blocks_stale_league_snapshot(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {
        "id": "chosen-game", "sport_key": "basketball_nba",
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": "basketball_nba", "freshness": "stale", "age_seconds": 21601}]
    })
    with TestClient(main.app) as client:
        response = client.post("/api/simulation/run?sport=basketball_nba&event_id=chosen-game")
    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert "21601 seconds old" in response.json()["detail"]


def test_simulation_endpoint_blocks_when_league_snapshot_is_unavailable(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {"id": "chosen-game", "sport_key": "basketball_nba",
             "commence_time": "2099-11-20T00:00:00Z"}
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {"sport_freshness": []})
    with TestClient(main.app) as client:
        response = client.post("/api/simulation/run?sport=basketball_nba&event_id=chosen-game")
    assert response.status_code == 409
    assert "unavailable" in response.json()["detail"]


def test_simulation_freshness_lookup_is_league_specific(monkeypatch):
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [
            {"sport": "americanfootball_nfl", "freshness": "stale"},
            {"sport": "basketball_nba", "freshness": "current", "age_seconds": 30},
        ]
    })
    assert main._simulation_snapshot_freshness("basketball_nba")["freshness"] == "current"
    assert main._simulation_snapshot_freshness("icehockey_nhl") is None



def test_optional_basic_auth_protects_dashboard_and_admin_controls_separately(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "subscriber")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "Subscriber-Secret")
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "CaseSensitive-Admin")
    subscriber = {"Authorization": "Basic " + base64.b64encode(b"subscriber:Subscriber-Secret").decode()}
    admin = {"Authorization": "Basic " + base64.b64encode(b"admin:CaseSensitive-Admin").decode()}
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/odds").status_code == 401
        assert client.get("/api/health").status_code == 200
        assert client.get("/", headers=subscriber).status_code == 200
        assert client.get("/api/odds", headers=subscriber).status_code == 200
        assert client.get("/admin", headers=subscriber).status_code == 401
        assert client.get("/api/admin/control", headers=subscriber).status_code == 401
        assert client.post("/api/refresh?sport=basketball_nba", headers=subscriber).status_code == 401
        assert client.get("/admin", headers=admin).status_code == 200
        assert client.get("/api/admin/control", headers=admin).status_code == 200


def test_authentication_fails_closed_when_only_one_credential_is_set(monkeypatch):
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "")
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 503
        assert client.get("/api/odds").status_code == 503
        # Health must remain available so Docker does not restart-loop on a
        # recoverable application configuration error.
        assert client.get("/api/health").status_code == 200


def test_manual_refresh_rejects_unconfigured_sport_without_provider_call(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    called = []
    monkeypatch.setattr(main.service, "refresh", lambda sport=None: called.append(sport))
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    with TestClient(main.app) as client:
        response = client.post("/api/refresh?sport=americanfootball_nfl", headers=headers)
    assert response.status_code == 400
    assert "configured league" in response.json()["detail"]
    assert called == []


def test_manual_refresh_without_sport_is_rejected_to_protect_provider_quotas(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "secret")
    called = []
    monkeypatch.setattr(main.service, "refresh", lambda sport=None: called.append(sport) or {"ok": True})
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    with TestClient(main.app) as client:
        response = client.post("/api/refresh", headers=headers)
    assert response.status_code == 400
    assert "one configured league" in response.json()["detail"]
    assert called == []


def test_simulation_endpoint_blocks_started_game_even_with_current_snapshot(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    event = {
        "id": "started-game", "sport_key": "basketball_nba",
        "commence_time": "2020-01-01T00:00:00Z",
        "home_team": "Home", "away_team": "Away",
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": "basketball_nba", "freshness": "current", "age_seconds": 30}]
    })
    with TestClient(main.app) as client:
        response = client.post("/api/simulation/run?sport=basketball_nba&event_id=started-game")
    assert response.status_code == 422
    assert "before the game starts" in response.json()["detail"]


def test_dashboard_exposes_overtime_total_adjustment_diagnostics():
    dashboard = main.WEB.read_text(encoding="utf-8")
    assert "expected_overtime_total_adjustment" in dashboard
    assert "regulation_total_anchor" in dashboard
    assert "expected OT points budgeted" in dashboard


def test_dashboard_has_separate_draw_card_for_moneyline_only_simulations():
    dashboard = main.WEB.read_text(encoding="utf-8")
    assert 'id="simDrawCard"' in dashboard
    assert 'id="simDrawProbability"' in dashboard
    assert 'data.win_probabilities.tie<=0' in dashboard


@pytest.mark.parametrize("sport_key,home,away", [
    ("tennis_atp", "Player Alpha", "Player Beta"),
    ("tennis_wta", "Player Gamma", "Player Delta"),
    ("mma_mixed_martial_arts", "Fighter Alpha", "Fighter Beta"),
])
def test_moneyline_only_sports_run_through_simulation_api(monkeypatch, sport_key, home, away):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", [sport_key])
    event = {
        "id": "outcome-only-api-event",
        "sport_key": sport_key,
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": home,
        "away_team": away,
        "bookmakers": [{
            "key": "book-a",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": -140},
                {"name": away, "price": 120},
            ]}],
        }],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main, "load_fitted_models", lambda path: _api_independent_models(sport_key, home, away))
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": sport_key, "freshness": "current", "age_seconds": 30}]
    })
    with TestClient(main.app) as client:
        games = client.get(f"/api/simulation/games?sport={sport_key}")
        result = client.post(
            f"/api/simulation/run?sport={sport_key}&event_id=outcome-only-api-event"
        )

    assert games.status_code == 200
    game = games.json()["games"][0]
    assert game["can_simulate"] is True
    assert game["simulation_mode"] == "independent_outcome_only"
    assert game["independent_model_available"] is True
    assert result.status_code == 200
    assert result.json()["simulation_type"] == "independent_outcome_monte_carlo"
    assert result.json()["data_quality"]["model_source"] == "independent_historical_elo"
    assert result.json()["market_edge_analysis"]["moneyline"]["book_count"] == 1
    assert "home" in result.json()["market_edge_analysis"]["moneyline"]["sides"]


@pytest.mark.parametrize("sport_key,total_line,spread_line", [
    ("basketball_nba", 220.5, 3.5),
    ("basketball_ncaab", 145.5, 4.5),
    ("basketball_wnba", 160.5, 3.5),
    ("americanfootball_nfl", 44.5, 3.0),
    ("americanfootball_ncaaf", 52.5, 6.5),
    ("baseball_mlb", 8.5, 1.5),
    ("icehockey_nhl", 5.5, 1.5),
    ("soccer_epl", 2.5, 0.5),
    ("soccer_mls", 2.5, 0.5),
    ("soccer_usa_mls", 2.5, 0.5),
])
def test_score_based_simulator_runs_through_api_for_each_supported_family(
    monkeypatch, sport_key, total_line, spread_line
):
    monkeypatch.setattr(main.config, "ODDS_SPORTS", [sport_key])
    event = {
        "id": f"score-family-{sport_key}",
        "sport_key": sport_key,
        "commence_time": "2099-11-20T00:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [{
            "key": "book-a",
            "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Home", "point": -spread_line, "price": -110},
                    {"name": "Away", "point": spread_line, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total_line, "price": -110},
                    {"name": "Under", "point": total_line, "price": -110},
                ]},
            ],
        }],
    }
    monkeypatch.setattr(main.service, "events", lambda sport=None: [event])
    monkeypatch.setattr(main, "load_fitted_models", lambda path: _api_independent_models(sport_key))
    monkeypatch.setattr(main.service, "status", lambda: {
        "sport_freshness": [{"sport": sport_key, "freshness": "current", "age_seconds": 30}]
    })
    with TestClient(main.app) as client:
        games = client.get(f"/api/simulation/games?sport={sport_key}")
        result = client.post(
            f"/api/simulation/run?sport={sport_key}&event_id={event['id']}&seed=123"
        )

    assert games.status_code == 200
    game = games.json()["games"][0]
    assert game["can_simulate"] is True
    assert game["spread_books"] == 1
    assert game["total_books"] == 1
    assert result.status_code == 200
    payload = result.json()
    assert payload["event_id"] == event["id"]
    assert payload["sport"] == sport_key
    assert payload["trials"] == 10_000
    assert set(payload["projected_score"]) == {"home", "away"}
    assert set(payload["win_probabilities"]) == {"home", "away", "tie"}
    assert set(payload["simulation_ranges"]) == {
        "home_score", "away_score", "home_margin", "game_total", "interpretation"
    }
    assert payload["data_quality"]["spread_books"] == 1
    assert payload["data_quality"]["total_books"] == 1


def test_all_bookmaker_quotes_endpoint_returns_every_book_and_respects_sport_filter(monkeypatch):
    received = {}
    events = [{
        "id": "evt", "sport_title": "Football", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "book-a", "title": "Book A", "markets": [{
                "key": "h2h", "outcomes": [{"name": "Home", "price": -110}, {"name": "Away", "price": -110}]
            }]},
            {"key": "book-b", "title": "Book B", "markets": [{
                "key": "h2h", "outcomes": [{"name": "Home", "price": 105}, {"name": "Away", "price": -125}]
            }]},
        ],
    }]
    def fake_events(sport=None):
        received["sport"] = sport
        return events
    monkeypatch.setattr(main.service, "events", fake_events)
    with TestClient(main.app) as client:
        response = client.get("/api/bookmaker-board?sport=americanfootball_nfl")
    assert response.status_code == 200
    assert received["sport"] == "americanfootball_nfl"
    quotes = response.json()["quotes"]
    assert len(quotes) == 4
    assert {quote["bookmaker_key"] for quote in quotes} == {"book-a", "book-b"}


def test_admin_refresh_endpoint_limits_provider_selection_and_targets_one_league(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    monkeypatch.setattr(main.service, "active_provider_names", lambda: ["therundown", "oddize"])
    monkeypatch.setattr(main, "_reserve_manual_refresh_slot", lambda: {"allowed": True, "used_today": 1, "remaining_today": 2})
    received = {}
    def fake_refresh(sport=None, providers=None):
        received.update(sport=sport, providers=providers)
        return {"ok": True, "events": 3}
    monkeypatch.setattr(main.service, "refresh", fake_refresh)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    with TestClient(main.app) as client:
        bad = client.post("/api/admin/refresh?sport=basketball_nba&providers=unknown", headers=headers)
        good = client.post("/api/admin/refresh?sport=basketball_nba&providers=therundown&providers=oddize", headers=headers)
    assert bad.status_code == 400
    assert good.status_code == 200
    assert received == {"sport": "basketball_nba", "providers": ["therundown", "oddize"]}


def test_admin_settings_are_validated_and_persisted(monkeypatch):
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "secret")
    stored = {}
    monkeypatch.setattr(main.service.db, "get_state", lambda key: stored.get(key))
    monkeypatch.setattr(main.service.db, "set_state", lambda key, value: stored.__setitem__(key, value))
    import base64
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    with TestClient(main.app) as client:
        response = client.post("/api/admin/settings", headers=headers, json={
            "automatic_refresh_enabled": False,
            "manual_refresh_enabled": True,
            "manual_refresh_cooldown_seconds": 1800,
            "manual_refresh_daily_limit": 2,
            "provider_auto_refresh_enabled": {"therundown": False},
        })
        invalid = client.post("/api/admin/settings", headers=headers, json={
            "manual_refresh_daily_limit": 1000,
        })
    assert response.status_code == 200
    assert response.json()["settings"]["automatic_refresh_enabled"] is False
    assert json.loads(stored["admin_control_settings"])["manual_refresh_daily_limit"] == 2
    assert json.loads(stored["admin_control_settings"])["provider_auto_refresh_enabled"]["therundown"] is False
    assert invalid.status_code == 422


def test_admin_routes_fail_closed_when_admin_credentials_are_missing(monkeypatch):
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "")
    with TestClient(main.app) as client:
        assert client.get("/admin").status_code == 503
        assert client.get("/api/admin/control").status_code == 503
        assert client.post("/api/refresh?sport=basketball_nba").status_code == 503
        assert client.post("/api/limits/refresh").status_code == 503
        assert client.post("/api/simulation/games/refresh?sport=basketball_nba").status_code == 503


def test_public_odds_endpoint_returns_quotes_without_provider_status(monkeypatch):
    monkeypatch.setattr(main.service, "events", lambda sport=None: [{"id": "event", "bookmakers": [{"key": "book", "markets": [{"key": "h2h", "outcomes": [{"name": "Home", "price": -110, "source_provider": "private-provider", "size_source_provider": "private-provider"}]}]}]}])
    monkeypatch.setattr(main.service, "status", lambda: {
        "providers": ["therundown"],
        "provider_keys": {"therundown": {"keys": [{"key_id": "private"}]}},
        "provider_usage": {"datapoints_remaining": 99},
    })
    with TestClient(main.app) as client:
        response = client.get("/api/odds")
    assert response.status_code == 200
    assert response.json()["events"][0]["id"] == "event"
    outcome = response.json()["events"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert "source_provider" not in outcome
    assert "size_source_provider" not in outcome


def test_admin_page_and_brand_logo_are_served(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "secret")
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    with TestClient(main.app) as client:
        page = client.get("/admin", headers=headers)
        logo = client.get("/assets/odds-intelligence.svg")
        public_page = client.get("/")
    assert page.status_code == 200
    assert "Administrator control center" in page.text
    assert logo.status_code == 200
    assert "<svg" in logo.text
    assert "odds-intelligence.svg" in public_page.text


def test_admin_credentials_must_differ_from_subscriber_credentials(monkeypatch):
    import base64
    monkeypatch.setattr(main.config, "AUTH_USERNAME", "shared")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "shared-secret")
    monkeypatch.setattr(main.config, "ADMIN_USERNAME", "shared")
    monkeypatch.setattr(main.config, "ADMIN_PASSWORD", "shared-secret")
    headers = {"Authorization": "Basic " + base64.b64encode(b"shared:shared-secret").decode()}
    with TestClient(main.app) as client:
        response = client.get("/admin", headers=headers)
    assert response.status_code == 503
    assert "different" in response.json()["detail"]
