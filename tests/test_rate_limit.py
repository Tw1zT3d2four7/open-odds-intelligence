import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.rate_limit import SQLiteRateLimiter, auth_rate_limit_keys


def test_fixed_window_allows_limit_then_denies_with_retry_after(tmp_path):
    now = [120.0]
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3", hash_key="test-rate-limit-hash-key-at-least-32-bytes", clock=lambda: now[0])

    first = limiter.consume("login:ip:192.0.2.10", limit=2, window_seconds=60)
    second = limiter.consume("login:ip:192.0.2.10", limit=2, window_seconds=60)
    third = limiter.consume("login:ip:192.0.2.10", limit=2, window_seconds=60)

    assert first.allowed and first.remaining == 1
    assert second.allowed and second.remaining == 0
    assert not third.allowed
    assert third.remaining == 0
    assert third.retry_after_seconds == 60


def test_new_window_resets_counter(tmp_path):
    now = [119.0]
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3", hash_key="test-rate-limit-hash-key-at-least-32-bytes", clock=lambda: now[0])
    assert limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60).allowed
    assert not limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60).allowed

    now[0] = 120.0
    reset = limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60)
    assert reset.allowed and reset.remaining == 0


def test_multiple_limiter_instances_share_atomic_counter(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    first = SQLiteRateLimiter(db, hash_key="test-rate-limit-hash-key-at-least-32-bytes", clock=lambda: 500.0)
    second = SQLiteRateLimiter(db, hash_key="test-rate-limit-hash-key-at-least-32-bytes", clock=lambda: 500.0)

    assert first.consume("reset:identity:user@example.test", limit=1, window_seconds=300).allowed
    assert not second.consume("reset:identity:user@example.test", limit=1, window_seconds=300).allowed


@pytest.mark.parametrize(
    "limit,window",
    [(0, 60), (1, 0), (-1, 60), (True, 60), (1, False)],
)
def test_invalid_limits_are_rejected(tmp_path, limit, window):
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3", hash_key="test-rate-limit-hash-key-at-least-32-bytes")
    with pytest.raises(ValueError):
        limiter.consume("bucket", limit=limit, window_seconds=window)


def test_bucket_key_is_not_stored_in_plaintext_and_database_is_private(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    limiter = SQLiteRateLimiter(db, hash_key="test-rate-limit-hash-key-at-least-32-bytes", clock=lambda: 10.0)
    limiter.consume("login:identity:private-user@example.test", limit=3, window_seconds=60)

    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT bucket_hash FROM auth_rate_limit_buckets"
        ).fetchone()
    finally:
        connection.close()

    assert row and "private-user" not in row[0]
    assert db.stat().st_mode & 0o777 == 0o600


def test_auth_rate_limit_keys_use_direct_peer_and_separate_identity_bucket():
    request = _request("192.0.2.20", "198.51.100.25")
    keys = auth_rate_limit_keys(request, " User@Example.Test ")
    assert keys[0] == "auth:ip:192.0.2.20"
    assert keys[1] == "auth:identity:user@example.test"



def _request(peer, forwarded_for=None):
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    from starlette.requests import Request

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 44321),
        "server": ("example.test", 443),
    })


def test_forwarded_for_is_ignored_unless_direct_peer_is_trusted():
    request = _request("192.0.2.20", "198.51.100.30")
    assert auth_rate_limit_keys(request)[0] == "auth:ip:192.0.2.20"
    assert auth_rate_limit_keys(
        request, trusted_proxy_cidrs=("10.0.0.0/8",)
    )[0] == "auth:ip:192.0.2.20"


def test_trusted_proxy_chain_resolves_first_untrusted_client_hop():
    request = _request("10.0.0.3", "198.51.100.30, 10.0.0.2")
    assert auth_rate_limit_keys(
        request, trusted_proxy_cidrs=("10.0.0.0/8",)
    )[0] == "auth:ip:198.51.100.30"


def test_untrusted_proxy_hop_stops_chain_walk():
    request = _request("10.0.0.3", "198.51.100.30, 203.0.113.7")
    assert auth_rate_limit_keys(
        request, trusted_proxy_cidrs=("10.0.0.0/8",)
    )[0] == "auth:ip:203.0.113.7"


@pytest.mark.parametrize(
    "forwarded",
    ["not-an-ip", "198.51.100.1, malformed", "198.51.100.1,"],
)
def test_malformed_forwarded_chain_falls_back_to_direct_peer(forwarded):
    request = _request("10.0.0.3", forwarded)
    assert auth_rate_limit_keys(
        request, trusted_proxy_cidrs=("10.0.0.0/8",)
    )[0] == "auth:ip:10.0.0.3"


def test_invalid_trusted_proxy_network_fails_closed_to_direct_peer():
    request = _request("10.0.0.3", "198.51.100.30")
    assert auth_rate_limit_keys(
        request, trusted_proxy_cidrs=("not-a-cidr",)
    )[0] == "auth:ip:10.0.0.3"



def test_missing_or_short_hash_secret_prevents_rate_limiter_initialization(tmp_path):
    with pytest.raises(ValueError, match="RATE_LIMIT_HASH_KEY"):
        SQLiteRateLimiter(tmp_path / "accounts.sqlite3", hash_key="too-short")



def test_concurrent_requests_cannot_exceed_fixed_window_limit(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    limiter = SQLiteRateLimiter(
        db,
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
        clock=lambda: 900.0,
    )

    def consume(_):
        return limiter.consume(
            "login:ip:192.0.2.99", limit=4, window_seconds=60
        ).allowed

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(consume, range(12)))

    assert sum(results) == 4
