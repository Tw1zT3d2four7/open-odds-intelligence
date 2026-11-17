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
