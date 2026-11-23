import threading
import time

from fastapi.testclient import TestClient

from app import main


def test_scheduled_refresh_runs_when_enabled(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.service, "auto_refresh_schedule", lambda: {"the_odds_api": 0.01})
    monkeypatch.setattr(main.service, "providers_due_for_auto_refresh", lambda: ["the_odds_api"])
    monkeypatch.setattr(main.service, "mark_auto_refresh", lambda names: None)
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda **kwargs: called.set() or {
        "ok": True, "provider_results": {"nfl": {"providers_succeeded": ["the_odds_api"]}}
    })

    with TestClient(main.app):
        assert called.wait(timeout=1)


def test_scheduled_refresh_stays_disabled_by_default(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.service, "auto_refresh_schedule", lambda: {})
    monkeypatch.setattr(type(main.service), "configured", property(lambda self: True))
    monkeypatch.setattr(main.service, "refresh", lambda **kwargs: called.set() or {"ok": True})

    with TestClient(main.app):
        time.sleep(0.05)
    assert not called.is_set()


def test_discord_webhook_evaluates_stored_quotes_when_provider_polling_is_off(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(main.service, "auto_refresh_schedule", lambda: {})
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
    monkeypatch.setattr(main.service, "auto_refresh_schedule", lambda: {"the_odds_api": 0.01})
    monkeypatch.setattr(main.service, "providers_due_for_auto_refresh", lambda: ["the_odds_api"])
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


def test_provider_refresh_intervals_are_tracked_independently(tmp_path, monkeypatch):
    from app.database import OddsDatabase
    from app.service import OddsService

    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "therundown")
    monkeypatch.setattr(main.config, "ODDS_PROVIDERS_EXPLICIT", True, raising=False)
    monkeypatch.setattr(main.config, "ODDS_PROVIDERS", [
        "therundown", "the_odds_api", "retired_provider", "oddize", "oddslink"
    ], raising=False)
    monkeypatch.setattr(main.config, "THERUNDOWN_API_KEY", "Rundown-Key")
    monkeypatch.setattr(main.config, "THERUNDOWN_API_KEYS", "", raising=False)
    monkeypatch.setattr(main.config, "THE_ODDS_API_KEY", "Odds-Key")
    monkeypatch.setattr(main.config, "THE_ODDS_API_KEYS", "", raising=False)
    monkeypatch.setattr(main.config, "ODDIZE_API_KEY", "Oddize-Key", raising=False)
    monkeypatch.setattr(main.config, "ODDSLINK_API_KEY", "OddsLink-Key", raising=False)
    monkeypatch.setattr(main.config, "ODDSLINK_API_KEYS", "", raising=False)
    monkeypatch.setattr(main.config, "RUNDOWN_REFRESH_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 86400)
    monkeypatch.setattr(main.config, "ODDIZE_REFRESH_INTERVAL_SECONDS", 86400, raising=False)
    monkeypatch.setattr(main.config, "ODDSLINK_REFRESH_INTERVAL_SECONDS", 86400, raising=False)

    service = OddsService(OddsDatabase(tmp_path / "provider-intervals.sqlite3"))
    assert service.auto_refresh_schedule() == {
        "therundown": 3600,
        "the_odds_api": 86400,
        "oddize": 86400,
        "oddslink": 86400,
    }
    assert service.active_provider_names() == [
        "therundown", "the_odds_api", "oddize", "oddslink"
    ]
    assert set(service.providers_due_for_auto_refresh()) == {
        "therundown", "the_odds_api", "oddize", "oddslink"
    }
    service.mark_auto_refresh(["the_odds_api", "oddize"])
    assert set(service.providers_due_for_auto_refresh()) == {
        "therundown", "oddslink"
    }


def test_admin_automatic_refresh_kill_switch_pauses_provider_requests(monkeypatch):
    import asyncio

    called = []
    monkeypatch.setattr(main, "_admin_settings", lambda: {
        "automatic_refresh_enabled": False,
        "manual_refresh_enabled": True,
        "manual_refresh_cooldown_seconds": 900,
        "manual_refresh_daily_limit": 3,
    })
    monkeypatch.setattr(main, "_odds_refresh_interval_seconds", lambda: 1)
    monkeypatch.setattr(main.service, "providers_due_for_auto_refresh", lambda: called.append("due") or ["therundown"])
    monkeypatch.setattr(main.asyncio, "to_thread", lambda fn, *args, **kwargs: _immediate(fn, *args, **kwargs))
    async def stop_after_one_sleep(delay):
        raise asyncio.CancelledError
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._automatic_refresh_loop())
    assert called == []


def _immediate(fn, *args, **kwargs):
    return fn(*args, **kwargs)
