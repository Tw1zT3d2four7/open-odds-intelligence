import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.identity import IdentityStore
from app.rate_limit import (
    SQLiteRateLimiter,
    auth_rate_limit_keys,
    enforce_rate_limit,
)


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
    [(0, 60), (1, 0), (-1, 60), (True, 60), (1, False), (1, 30 * 24 * 60 * 60 + 1)],
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



def test_multi_bucket_consumption_is_atomic_when_identity_bucket_is_limited(tmp_path):
    now = [600.0]
    limiter = SQLiteRateLimiter(
        tmp_path / "accounts.sqlite3",
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
        clock=lambda: now[0],
    )
    ip = "login:ip:192.0.2.45"
    user_a = "login:identity:a@example.test"
    user_b = "login:identity:b@example.test"

    assert limiter.consume_many([(ip, 2, 60), (user_a, 1, 60)]).allowed
    denied = limiter.consume_many([(ip, 2, 60), (user_a, 1, 60)])
    assert not denied.allowed

    # The denied attempt must not consume the still-available IP bucket.
    assert limiter.consume_many([(ip, 2, 60), (user_b, 1, 60)]).allowed


def test_multi_bucket_rejects_empty_or_conflicting_duplicate_buckets(tmp_path):
    limiter = SQLiteRateLimiter(
        tmp_path / "accounts.sqlite3",
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
    )
    with pytest.raises(ValueError):
        limiter.consume_many([])
    with pytest.raises(ValueError, match="Duplicate bucket"):
        limiter.consume_many([("same", 2, 60), ("same", 3, 60)])



def test_http_rate_limit_returns_429_and_retry_after_header(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    limiter = SQLiteRateLimiter(
        tmp_path / "accounts.sqlite3",
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
        clock=lambda: 120.0,
    )
    app = FastAPI()

    @app.post("/login")
    def login():
        enforce_rate_limit(
            limiter,
            ["login:ip:192.0.2.50", "login:identity:user@example.test"],
            limit=1,
            window_seconds=60,
        )
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/login").status_code == 200
    denied = client.post("/login")
    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "60"
    assert denied.json()["detail"] == "Too many requests. Please try again later."


def test_rate_limit_storage_failure_fails_closed_without_leaking_details():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class BrokenLimiter:
        def consume_many(self, _buckets):
            raise RuntimeError("private database path and error")

    app = FastAPI()

    @app.post("/login")
    def login():
        enforce_rate_limit(BrokenLimiter(), ["login:ip:192.0.2.50"], limit=1, window_seconds=60)
        return {"ok": True}

    response = TestClient(app).post("/login")
    assert response.status_code == 503
    assert "private database path" not in response.text



def test_rate_limiter_restricts_identity_database_wal_sidecars(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    IdentityStore(db)
    limiter = SQLiteRateLimiter(
        db,
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
        clock=lambda: 120.0,
    )
    limiter.consume("admin-login:ip:192.0.2.60", limit=2, window_seconds=60)

    assert db.stat().st_mode & 0o777 == 0o600
    for sidecar in (tmp_path / "accounts.sqlite3-wal", tmp_path / "accounts.sqlite3-shm"):
        if sidecar.exists():
            assert sidecar.stat().st_mode & 0o777 == 0o600



def test_stale_rate_limit_buckets_are_pruned_after_retention_window(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    now = [100.0]
    limiter = SQLiteRateLimiter(
        db,
        hash_key="test-rate-limit-hash-key-at-least-32-bytes",
        clock=lambda: now[0],
    )
    limiter.consume("old:identity:former@example.test", limit=2, window_seconds=60)

    now[0] += 91 * 24 * 60 * 60
    limiter.consume("new:identity:current@example.test", limit=2, window_seconds=60)

    connection = sqlite3.connect(db)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM auth_rate_limit_buckets"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1



@pytest.mark.parametrize("preexisting", [False, True])
def test_rate_limiter_secures_database_before_sqlite_opens(tmp_path, monkeypatch, preexisting):
    from app import rate_limit as rate_limit_module

    db = tmp_path / "accounts.sqlite3"
    if preexisting:
        db.write_bytes(b"")
        db.chmod(0o644)

    real_connect = sqlite3.connect
    observed_modes = []

    def inspect_connect(database, *args, **kwargs):
        if str(database) == str(db):
            observed_modes.append(db.stat().st_mode & 0o777)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(rate_limit_module.sqlite3, "connect", inspect_connect)
    limiter = SQLiteRateLimiter(
        db, hash_key="test-rate-limit-hash-key-at-least-32-bytes"
    )
    limiter.consume("permission-check", limit=1, window_seconds=60)

    assert observed_modes
    assert observed_modes[0] == 0o600
    assert db.stat().st_mode & 0o777 == 0o600


def test_rate_limiter_rejects_fifo_without_blocking(tmp_path):
    import os

    db = tmp_path / "accounts.sqlite3"
    os.mkfifo(db)
    limiter = SQLiteRateLimiter(
        db, hash_key="test-rate-limit-hash-key-at-least-32-bytes"
    )

    with pytest.raises(ValueError, match="regular file"):
        limiter.consume("fifo-check", limit=1, window_seconds=60)


def test_rate_limiter_rejects_symlink_database_path(tmp_path):
    target = tmp_path / "real.sqlite3"
    target.write_bytes(b"")
    link = tmp_path / "accounts.sqlite3"
    link.symlink_to(target)
    limiter = SQLiteRateLimiter(
        link, hash_key="test-rate-limit-hash-key-at-least-32-bytes"
    )

    with pytest.raises(OSError):
        limiter.consume("symlink-check", limit=1, window_seconds=60)
