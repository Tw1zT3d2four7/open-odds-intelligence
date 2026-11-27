from types import SimpleNamespace

from app import config
from app.service import OddsService


def _service(configured):
    pools = {
        name: SimpleNamespace(configured=name in configured)
        for name in ("therundown", "the_odds_api", "oddize", "oddslink")
    }
    return SimpleNamespace(_provider_key_pools=pools)


def test_default_none_provider_auto_selects_only_providers_with_loaded_keys(monkeypatch):
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", False)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", [])
    monkeypatch.setattr(config, "ODDS_PROVIDER", "none")
    service = _service({"therundown", "oddslink"})
    assert OddsService.active_provider_names(service) == ["therundown", "oddslink"]


def test_blank_legacy_provider_auto_selects_configured_keys(monkeypatch):
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", False)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", [])
    monkeypatch.setattr(config, "ODDS_PROVIDER", "")
    service = _service({"the_odds_api"})
    assert OddsService.active_provider_names(service) == ["the_odds_api"]


def test_explicit_provider_list_remains_authoritative(monkeypatch):
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", True)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", ["oddize"])
    monkeypatch.setattr(config, "ODDS_PROVIDER", "none")
    service = _service({"therundown", "oddize", "oddslink"})
    assert OddsService.active_provider_names(service) == ["oddize"]


def test_unknown_legacy_provider_does_not_enable_everything(monkeypatch):
    monkeypatch.setattr(config, "ODDS_PROVIDERS_EXPLICIT", False)
    monkeypatch.setattr(config, "ODDS_PROVIDERS", [])
    monkeypatch.setattr(config, "ODDS_PROVIDER", "unsupported")
    service = _service({"therundown", "oddize"})
    assert OddsService.active_provider_names(service) == []
