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



def test_account_group_status_aggregates_quota_without_exposing_credentials():
    pool = ProviderKeyPool(
        "example",
        ["free-account|FirstCaseKey,free-account|SecondCaseKey"],
    )
    lease = pool.acquire()
    pool.record_usage(lease, {
        "credits_remaining": 42,
        "period_resets_at": "2026-12-01T00:00:00Z",
        "api_key": "NeverExposeThisValue",
    })

    status = pool.status()
    assert len(status["account_groups"]) == 1
    group = status["account_groups"][0]
    assert group["account_group"] == "free-account"
    assert group["configured_keys"] == 2
    assert group["available_keys"] == 2
    assert group["usage"]["credits_remaining"] == 42
    assert "api_key" not in group["usage"]
    assert "NeverExposeThisValue" not in repr(status)



def test_service_never_persists_raw_provider_credentials_from_usage_payload(tmp_path):
    from app.database import OddsDatabase
    from app.service import OddsService

    database = OddsDatabase(tmp_path / "usage-sanitization.sqlite3")
    service = OddsService(database=database)
    pool = ProviderKeyPool("example", ["free-account|CaseSensitiveKey"])
    lease = pool.acquire()
    usage = {
        "credits_remaining": 17,
        "usage_observed_at": "2026-11-26T00:00:00Z",
        "api_key": "NeverPersistThisKey",
        "access_token": "NeverPersistThisToken",
        "authorization": "Bearer NeverPersistThisAuthorization",
        "password": "NeverPersistThisPassword",
    }

    service._record_provider_key_usage("example", pool, lease, usage)
    service._remember_provider_usage(usage, "example", lease.fingerprint)

    per_key = database.get_state(f"provider_key_usage:example:{lease.fingerprint}")
    latest = database.get_state("provider_usage")
    assert "NeverPersistThisKey" not in per_key
    assert "NeverPersistThisToken" not in per_key
    assert "NeverPersistThisKey" not in latest
    assert "NeverPersistThisToken" not in latest
    assert "NeverPersistThisAuthorization" not in latest
    assert "NeverPersistThisPassword" not in latest
    assert "credits_remaining" in latest
