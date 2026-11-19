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
