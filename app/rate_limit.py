"""Persistent, privacy-preserving rate-limit primitives for future auth routes.

Not wired to HTTP routes yet. Uses the separate accounts database, never the
odds-history database. Caller must enforce each returned bucket dimension.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import Request

from . import config


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class SQLiteRateLimiter:
    """Atomic fixed-window counters shared by workers using the same SQLite DB."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        hash_key: str | bytes | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path or config.ACCOUNTS_DATABASE_PATH)
        configured_key = hash_key if hash_key is not None else config.RATE_LIMIT_HASH_KEY
        if isinstance(configured_key, str):
            configured_key = configured_key.encode("utf-8")
        if not isinstance(configured_key, bytes) or len(configured_key) < 32:
            raise ValueError("RATE_LIMIT_HASH_KEY must contain at least 32 bytes.")
        self.hash_key = configured_key
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(
            str(self.database_path), timeout=5.0, isolation_level=None
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            connection.close()
            raise
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_rate_limit_buckets (
                bucket_hash TEXT PRIMARY KEY,
                window_start INTEGER NOT NULL,
                request_count INTEGER NOT NULL CHECK (request_count >= 0)
            )
            """
        )
        return connection

    def _digest(self, bucket_key: str) -> str:
        if not isinstance(bucket_key, str) or not bucket_key or len(bucket_key) > 2048:
            raise ValueError("A non-empty bounded rate-limit key is required.")
        return hmac.new(
            self.hash_key, bucket_key.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def consume(
        self, bucket_key: str, *, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        """Consume one request from a fixed window, atomically across workers."""
        return self.consume_many([(bucket_key, limit, window_seconds)])

    def consume_many(
        self, buckets: list[tuple[str, int, int]] | tuple[tuple[str, int, int], ...]
    ) -> RateLimitDecision:
        """Atomically check and consume multiple IP/account buckets.

        Each entry is (bucket_key, limit, window_seconds). If any bucket is
        already limited, none of the other buckets are incremented.
        """
        if not buckets:
            raise ValueError("At least one rate-limit bucket is required.")

        now = max(0, int(self.clock()))
        normalized: dict[str, tuple[int, int, int]] = {}
        for bucket_key, limit, window_seconds in buckets:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("Rate-limit count must be a positive integer.")
            if (
                not isinstance(window_seconds, int)
                or isinstance(window_seconds, bool)
                or window_seconds < 1
            ):
                raise ValueError("Rate-limit window must be a positive integer.")
            bucket_hash = self._digest(bucket_key)
            window_start = now - (now % window_seconds)
            value = (limit, window_seconds, window_start)
            if bucket_hash in normalized and normalized[bucket_hash] != value:
                raise ValueError("Duplicate bucket keys must use identical policies.")
            normalized[bucket_hash] = value

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            counts: dict[str, int] = {}
            blocked_retries: list[int] = []
            for bucket_hash, (limit, window_seconds, window_start) in normalized.items():
                connection.execute(
                    "DELETE FROM auth_rate_limit_buckets "
                    "WHERE bucket_hash = ? AND window_start < ?",
                    (bucket_hash, window_start),
                )
                row = connection.execute(
                    "SELECT request_count FROM auth_rate_limit_buckets "
                    "WHERE bucket_hash = ? AND window_start = ?",
                    (bucket_hash, window_start),
                ).fetchone()
                count = int(row[0]) if row else 0
                counts[bucket_hash] = count
                if count >= limit:
                    blocked_retries.append(max(1, window_start + window_seconds - now))

            if blocked_retries:
                connection.execute("COMMIT")
                return RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(blocked_retries),
                )

            remaining_values = []
            for bucket_hash, (limit, _window_seconds, window_start) in normalized.items():
                count = counts[bucket_hash]
                connection.execute(
                    """
                    INSERT INTO auth_rate_limit_buckets
                        (bucket_hash, window_start, request_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(bucket_hash) DO UPDATE SET
                        window_start = excluded.window_start,
                        request_count = CASE
                            WHEN auth_rate_limit_buckets.window_start = excluded.window_start
                            THEN auth_rate_limit_buckets.request_count + 1
                            ELSE 1
                        END
                    """,
                    (bucket_hash, window_start),
                )
                remaining_values.append(limit - count - 1)
            connection.execute("COMMIT")
            return RateLimitDecision(
                allowed=True,
                remaining=max(0, min(remaining_values)),
                retry_after_seconds=0,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def request_peer_ip(
    request: Request, trusted_proxy_cidrs: tuple[str, ...] | list[str] = ()
) -> str:
    """Resolve the client IP only through explicitly trusted proxy networks.

    Forwarded headers are ignored by default. If a configured proxy chain is
    malformed, fall back to the direct TCP peer instead of trusting partial data.
    """
    peer = request.client.host if request.client else ""
    try:
        direct_peer = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"

    if not trusted_proxy_cidrs or not request.client:
        return str(direct_peer)
    try:
        trusted_networks = tuple(
            ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs
        )
    except ValueError:
        return str(direct_peer)

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return str(direct_peer)
    try:
        chain = [ipaddress.ip_address(part.strip()) for part in forwarded.split(",")]
        if not chain or len(chain) > 20:
            return str(direct_peer)
    except ValueError:
        return str(direct_peer)

    current = direct_peer
    for hop in reversed(chain):
        if not any(current in network for network in trusted_networks):
            break
        current = hop
    return str(current)


def auth_rate_limit_keys(
    request: Request,
    identifier: str | None = None,
    *,
    trusted_proxy_cidrs: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    """Build independent IP and account buckets without persisting raw PII."""
    keys = [f"auth:ip:{request_peer_ip(request, trusted_proxy_cidrs)}"]
    if identifier:
        normalized = identifier.strip().casefold()
        if normalized:
            keys.append(f"auth:identity:{normalized}")
    return tuple(keys)
