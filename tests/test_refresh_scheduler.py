import threading
import time

from fastapi.testclient import TestClient

from app import main


def test_scheduled_refresh_runs_when_enabled(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda: called.set() or {"ok": True})

    with TestClient(main.app):
        assert called.wait(timeout=1)


def test_scheduled_refresh_stays_disabled_by_default(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda: called.set() or {"ok": True})

    with TestClient(main.app):
        time.sleep(0.05)
    assert not called.is_set()


def test_discord_webhook_enables_automatic_refresh_when_general_refresh_is_off(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda: called.set() or {"ok": True})

    with TestClient(main.app):
        assert called.wait(timeout=1)


def test_scheduled_refresh_failure_is_exposed_in_service_status(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "_last_error", None)

    def failing_refresh():
        called.set()
        raise RuntimeError("scheduled provider failure")

    monkeypatch.setattr(main.service, "refresh", failing_refresh)
    with TestClient(main.app):
        assert called.wait(timeout=1)
        time.sleep(0.02)
        assert "scheduled provider failure" in main.service.status()["last_error"]
