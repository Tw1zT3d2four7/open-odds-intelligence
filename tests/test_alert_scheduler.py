import asyncio

from app import main


def test_provider_poll_interval_is_independent_of_discord_interval(monkeypatch):
    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "therundown")
    monkeypatch.setattr(main.config, "RUNDOWN_REFRESH_INTERVAL_SECONDS", 14400)
    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 300)
    assert main._odds_refresh_interval_seconds() == 14400

    monkeypatch.setattr(main.config, "ODDS_PROVIDER", "the_odds_api")
    monkeypatch.setattr(main.config, "ODDS_REFRESH_INTERVAL_SECONDS", 0)
    assert main._odds_refresh_interval_seconds() == 0


def test_discord_loop_checks_stored_quotes_without_provider_refresh(monkeypatch):
    calls = []

    async def stop_after_one_sleep(delay):
        calls.append(("sleep", delay))
        raise asyncio.CancelledError

    async def fake_to_thread(function, *args, **kwargs):
        calls.append(("thread", function.__name__))
        return 0

    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_sleep)
    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    try:
        asyncio.run(main._discord_alert_loop())
    except asyncio.CancelledError:
        pass

    assert calls == [("sleep", 300)]


def test_discord_loop_dispatches_alert_evaluation_not_odds_refresh(monkeypatch):
    calls = []

    async def one_cycle_sleep(delay):
        if calls:
            raise asyncio.CancelledError

    async def fake_to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(main.asyncio, "sleep", one_cycle_sleep)
    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    try:
        asyncio.run(main._discord_alert_loop())
    except asyncio.CancelledError:
        pass

    assert calls == ["evaluate_alerts"]
