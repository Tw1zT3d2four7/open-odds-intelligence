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


def test_discord_loop_evaluates_stored_quotes_without_provider_refresh(monkeypatch):
    calls = []
    sleep_count = 0

    async def stop_after_one_cycle(delay):
        nonlocal sleep_count
        sleep_count += 1
        calls.append(("sleep", delay))
        if sleep_count > 1:
            raise asyncio.CancelledError

    async def fake_to_thread(function, *args, **kwargs):
        calls.append(("thread", function.__name__))
        return 0

    monkeypatch.setattr(main.config, "DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_cycle)
    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    try:
        asyncio.run(main._discord_alert_loop())
    except asyncio.CancelledError:
        pass

    assert calls == [("sleep", 300), ("thread", "evaluate_alerts")]
