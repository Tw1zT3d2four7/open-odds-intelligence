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
