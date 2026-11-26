from app.providers.key_pool import NoAvailableKeys, ProviderKeyPool


def test_grouped_provider_keys_keep_account_groups_and_share_quota():
    pool = ProviderKeyPool(
        "example",
        ["primary|AbC-Key,primary|Def-Key\nsecondary|GhI-Key"],
    )

    first = pool.acquire()
    second = pool.acquire()
    third = pool.acquire()

    assert [first.account_group, second.account_group, third.account_group] == [
        "primary", "primary", "secondary"
    ]
    assert [first.key, second.key, third.key] == ["AbC-Key", "Def-Key", "GhI-Key"]

    assert pool.mark_failure(first, status_code=429, message="quota exceeded") == "cooldown"
    assert pool.status()["keys"][0]["status"] == "quota_cooldown"
    assert pool.status()["keys"][1]["status"] == "quota_cooldown"
    assert pool.status()["keys"][2]["status"] == "available"


def test_ungrouped_provider_keys_keep_legacy_shared_default_group():
    pool = ProviderKeyPool("example", ["One-Key,Two-Key"])
    assert {item["account_group"] for item in pool.status()["keys"]} == {"default"}



def test_account_group_polling_filter_never_leases_premium_or_unknown_groups():
    pool = ProviderKeyPool(
        "example",
        ["free-account|Free-Key,premium-account|Premium-Key,unknown|Other-Key"],
    )

    first = pool.acquire(allowed_account_groups={"free-account"})
    second = pool.acquire(allowed_account_groups={"free-account"})
    assert first.account_group == second.account_group == "free-account"
    assert first.key == second.key == "Free-Key"
    assert {item["account_group"]: item["status"] for item in pool.status()["keys"]} == {
        "free-account": "available",
        "premium-account": "available",
        "unknown": "available",
    }

    try:
        pool.acquire(allowed_account_groups={"not-configured"})
    except NoAvailableKeys:
        pass
    else:
        raise AssertionError("An ineligible account group must never be leased")



def test_score_sync_cannot_poll_premium_only_account_when_premium_polling_is_off(tmp_path, monkeypatch):
    from app import config
    from app.database import OddsDatabase
    from app.service import OddsService

    service = OddsService(database=OddsDatabase(tmp_path / "odds.sqlite3"))
    service._provider_key_pools["therundown"] = ProviderKeyPool(
        "therundown", ["premium-account|PremiumCaseKey"]
    )
    monkeypatch.setattr(
        config, "ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES",
        {"therundown": {"premium-account": "premium"}},
    )
    monkeypatch.setattr(config, "ODDS_PREMIUM_PROVIDER_POLLING_ENABLED", False)
    monkeypatch.setattr(config, "SIMULATION_RESULTS_SYNC_ENABLED", True)
    monkeypatch.setattr(service, "active_provider_names", lambda: ["therundown"])

    result = service.sync_simulation_results()

    assert result["skipped"] is True
    assert result["reason"] == "disabled"



def test_account_access_class_configuration_parses_comma_and_newline_entries(monkeypatch):
    import importlib
    from app import config

    with monkeypatch.context() as scoped:
        scoped.setenv(
            "ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES",
            "The_Odds_API|FREE=free\nThe_Odds_API|Pro=premium",
        )
        importlib.reload(config)
        assert config.ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES == {
            "the_odds_api": {"free": "free", "pro": "premium"}
        }

    # Restore module-level config values to match the restored process environment.
    importlib.reload(config)
