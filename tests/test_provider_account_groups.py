from app.providers.key_pool import ProviderKeyPool


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
