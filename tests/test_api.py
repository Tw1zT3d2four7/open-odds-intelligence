from fastapi.testclient import TestClient

from app import main


def test_manual_refresh_endpoint_passes_sport_and_returns_alert_diagnostics(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "")
    received = {}

    def fake_refresh(sport=None):
        received["sport"] = sport
        return {
            "ok": True,
            "events": 12,
            "alerts_enabled": True,
            "alerts_evaluated": 8,
            "alerts_qualified": 3,
            "alerts_sent": 2,
            "alert_error": None,
        }

    monkeypatch.setattr(main.service, "refresh", fake_refresh)
    with TestClient(main.app) as client:
        response = client.post("/api/refresh?sport=basketball_nba")

    assert response.status_code == 200
    assert received["sport"] == "basketball_nba"
    assert response.json()["alerts_sent"] == 2
    assert response.json()["alerts_qualified"] == 3


def test_health_endpoint_exposes_alert_delivery_diagnostics(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(main.service, "status", lambda: {
        "alerts_enabled": True,
        "alerts_sent": 5,
        "last_alert_evaluated": 11,
        "last_alert_qualified": 4,
        "last_alert_sent": 3,
        "last_alert_error": "temporary failure",
    })

    with TestClient(main.app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["last_alert_evaluated"] == 11
    assert payload["last_alert_qualified"] == 4
    assert payload["last_alert_sent"] == 3
    assert payload["last_alert_error"] == "temporary failure"


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
    def fake_analysis(events, min_ev, min_consensus_books):
        received.update(events=events, min_ev=min_ev, min_consensus_books=min_consensus_books)
        return {"opportunities": [{"event_id": "event"}], "arbitrage": []}
    monkeypatch.setattr(main.service, "events", fake_events)
    monkeypatch.setattr(main, "analyze_markets", fake_analysis)
    with TestClient(main.app) as client:
        response = client.get("/api/opportunities?sport=basketball_nba&min_ev=2.5&min_consensus_books=3")
    assert response.status_code == 200
    assert received == {
        "sport": "basketball_nba", "events": [{"id": "event"}],
        "min_ev": 2.5, "min_consensus_books": 3,
    }
    assert response.json()["opportunities"] == [{"event_id": "event"}]


def test_arbitrage_endpoint_requests_arbitrage_analysis(monkeypatch):
    received = {}
    monkeypatch.setattr(main.service, "events", lambda sport=None: [{"id": sport}])
    def fake_analysis(events, min_ev):
        received.update(events=events, min_ev=min_ev)
        return {"opportunities": [], "arbitrage": [{"event_id": "event", "arb_percent": 1.2}]}
    monkeypatch.setattr(main, "analyze_markets", fake_analysis)
    with TestClient(main.app) as client:
        response = client.get("/api/arbitrage?sport=baseball_mlb")
    assert response.status_code == 200
    assert received == {"events": [{"id": "baseball_mlb"}], "min_ev": 101}
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



def test_optional_basic_auth_protects_dashboard_and_api_but_allows_health(monkeypatch):
    import base64

    monkeypatch.setattr(main.config, "AUTH_USERNAME", "operator")
    monkeypatch.setattr(main.config, "AUTH_PASSWORD", "CaseSensitive-Secret")
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/odds").status_code == 401
        assert client.post("/api/refresh").status_code == 401
        assert client.get("/api/health").status_code == 200
        token = base64.b64encode(b"operator:CaseSensitive-Secret").decode()
        headers = {"Authorization": f"Basic {token}"}
        assert client.get("/", headers=headers).status_code == 200
        assert client.get("/api/odds", headers=headers).status_code == 200
        wrong = base64.b64encode(b"operator:casesensitive-secret").decode()
        assert client.get("/", headers={"Authorization": f"Basic {wrong}"}).status_code == 401


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
    monkeypatch.setattr(main.config, "ODDS_SPORTS", ["basketball_nba"])
    called = []
    monkeypatch.setattr(main.service, "refresh", lambda sport=None: called.append(sport))
    with TestClient(main.app) as client:
        response = client.post("/api/refresh?sport=americanfootball_nfl")
    assert response.status_code == 400
    assert "configured league" in response.json()["detail"]
    assert called == []


def test_manual_refresh_without_sport_still_refreshes_all_configured_leagues(monkeypatch):
    called = []
    monkeypatch.setattr(main.service, "refresh", lambda sport=None: called.append(sport) or {"ok": True})
    with TestClient(main.app) as client:
        response = client.post("/api/refresh")
    assert response.status_code == 200
    assert called == [None]


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
