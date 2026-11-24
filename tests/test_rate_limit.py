import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.rate_limit import SQLiteRateLimiter, auth_rate_limit_keys


def test_fixed_window_allows_limit_then_denies_with_retry_after(tmp_path):
    now = [120.0]
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3", clock=lambda: now[0])

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
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3", clock=lambda: now[0])
    assert limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60).allowed
    assert not limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60).allowed

    now[0] = 120.0
    reset = limiter.consume("register:ip:192.0.2.11", limit=1, window_seconds=60)
    assert reset.allowed and reset.remaining == 0


def test_multiple_limiter_instances_share_atomic_counter(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    first = SQLiteRateLimiter(db, clock=lambda: 500.0)
    second = SQLiteRateLimiter(db, clock=lambda: 500.0)

    assert first.consume("reset:identity:user@example.test", limit=1, window_seconds=300).allowed
    assert not second.consume("reset:identity:user@example.test", limit=1, window_seconds=300).allowed


@pytest.mark.parametrize(
    "limit,window",
    [(0, 60), (1, 0), (-1, 60), (True, 60), (1, False)],
)
def test_invalid_limits_are_rejected(tmp_path, limit, window):
    limiter = SQLiteRateLimiter(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError):
        limiter.consume("bucket", limit=limit, window_seconds=window)


def test_bucket_key_is_not_stored_in_plaintext_and_database_is_private(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    limiter = SQLiteRateLimiter(db, clock=lambda: 10.0)
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
    app = FastAPI()

    @app.get("/")
    def inspect(request: Request):
        return {"keys": auth_rate_limit_keys(request, " User@Example.Test ")}

    response = TestClient(app).get(
        "/",
        headers={
            "x-forwarded-for": "198.51.100.25",
            "x-real-ip": "203.0.113.7",
        },
    )
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert keys[0] == "auth:ip:testclient"
    assert keys[1] == "auth:identity:user@example.test"
