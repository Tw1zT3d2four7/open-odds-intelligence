import threading
import time

from fastapi.testclient import TestClient

from app import main


def test_scheduled_refresh_runs_when_enabled(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda **kwargs: called.set() or {"ok": True})

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


def test_discord_webhook_evaluates_stored_quotes_when_provider_polling_is_off(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "evaluate_alerts", lambda: called.set() or 0)
    refresh_called = threading.Event()
    monkeypatch.setattr(main.service, "refresh", lambda **kwargs: refresh_called.set() or {"ok": True})

    with TestClient(main.app):
        assert called.wait(timeout=1)
    assert not refresh_called.is_set()


def test_scheduled_refresh_failure_is_exposed_in_service_status(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "_last_error", None)

    def failing_refresh(**kwargs):
        called.set()
        raise RuntimeError("scheduled provider failure")

    monkeypatch.setattr(main.service, "refresh", failing_refresh)
    with TestClient(main.app):
        assert called.wait(timeout=1)
        time.sleep(0.02)
        assert "scheduled provider failure" in main.service.status()["last_error"]


def test_limits_scheduler_sleeps_only_until_persisted_cooldown_expires(monkeypatch):
    import asyncio
    import pytest

    sleeps = []
    monkeypatch.setattr(main.limits_monitor, "refresh", lambda: {"ok": True})
    monkeypatch.setattr(main.limits_monitor, "status", lambda: {
        "next_refresh_seconds": 7,
    })

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._limits_refresh_loop())
    assert sleeps == [7]
