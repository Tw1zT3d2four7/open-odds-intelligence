import pytest

from app.providers.key_pool import (
    NoAvailableKeys,
    ProviderKeyPool,
    parse_api_keys,
)


def test_parse_keys_preserves_case_and_deduplicates():
    assert parse_api_keys(
        "AbC-Key, second-Key\nThird-Key",
        "AbC-Key, fourth-Key",
    ) == ["AbC-Key", "second-Key", "Third-Key", "fourth-Key"]


def test_round_robin_cycles_keys_without_exposing_credentials():
    pool = ProviderKeyPool("sample", ["FirstSecret", "SecondSecret"])
    first = pool.acquire()
    second = pool.acquire()
    third = pool.acquire()

    assert [first.key, second.key, third.key] == [
        "FirstSecret", "SecondSecret", "FirstSecret"
    ]
    status = pool.status()
    rendered = repr(status)
    assert "FirstSecret" not in rendered
    assert "SecondSecret" not in rendered
    assert status["configured_keys"] == 2


def test_quota_failure_cools_only_the_affected_key_and_rotates():
    now = [100.0]
    pool = ProviderKeyPool("sample", ["account-a|KeyA", "account-b|KeyB"], clock=lambda: now[0])
    first = pool.acquire()
    assert first.key == "KeyA"

    assert pool.mark_failure(
        first, status_code=429, message="quota reached",
        retry_after_seconds=300,
    ) == "cooldown"

    second = pool.acquire()
    assert second.key == "KeyB"
    assert pool.status()["available_keys"] == 1

    now[0] += 301
    assert pool.acquire().key == "KeyA"


def test_authentication_failure_disables_only_bad_key():
    pool = ProviderKeyPool("sample", ["account-a|BadKey", "account-b|GoodKey"])
    bad = pool.acquire()
    assert pool.mark_failure(
        bad, status_code=401, message="invalid credential"
    ) == "disabled"

    assert pool.acquire().key == "GoodKey"
    assert pool.status()["available_keys"] == 1


def test_all_keys_in_cooldown_reports_next_retry():
    now = [50.0]
    pool = ProviderKeyPool("sample", ["OnlyKey"], clock=lambda: now[0])
    lease = pool.acquire()
    pool.mark_failure(lease, status_code=429, retry_after_seconds=90)

    with pytest.raises(NoAvailableKeys) as caught:
        pool.acquire()
    assert caught.value.next_available_in == 90


def test_non_quota_server_error_does_not_disable_or_cooldown_key():
    pool = ProviderKeyPool("sample", ["KeyA"])
    lease = pool.acquire()
    assert pool.mark_failure(
        lease, status_code=503, message="temporarily unavailable"
    ) == "unchanged"
    assert pool.acquire().key == "KeyA"


def test_keys_from_same_account_share_quota_cooldown():
    now = [100.0]
    pool = ProviderKeyPool(
        "sample", ["same-account|KeyA", "same-account|KeyB"],
        clock=lambda: now[0],
    )
    first = pool.acquire()
    pool.mark_failure(first, status_code=429, retry_after_seconds=120)
    assert pool.status()["available_keys"] == 0
    with pytest.raises(NoAvailableKeys):
        pool.acquire()


def test_monthly_credit_exhaustion_401_is_cooldown_not_invalid_key_disable():
    now = [100.0]
    pool = ProviderKeyPool("odds_api", ["KeyA"], clock=lambda: now[0])
    lease = pool.acquire()
    assert pool.mark_failure(
        lease, status_code=401, message="OUT_OF_USAGE_CREDITS"
    ) == "cooldown"
    key = pool.status()["keys"][0]
    assert key["status"] == "quota_cooldown"
    assert key["cooldown_seconds"] >= 86400
