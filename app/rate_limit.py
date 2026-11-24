"""Persistent, privacy-preserving rate-limit primitives for future auth routes.

Not wired to HTTP routes yet. Uses the separate accounts database, never the
odds-history database. Caller must enforce each returned bucket dimension.
"""
from __future__ import annotations

import hashlib
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path or config.ACCOUNTS_DATABASE_PATH)
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

    @staticmethod
    def _digest(bucket_key: str) -> str:
        if not isinstance(bucket_key, str) or not bucket_key or len(bucket_key) > 2048:
            raise ValueError("A non-empty bounded rate-limit key is required.")
        return hashlib.sha256(bucket_key.encode("utf-8")).hexdigest()

    def consume(
        self, bucket_key: str, *, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        """Consume one request from a fixed window, atomically across workers."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Rate-limit count must be a positive integer.")
        if (
            not isinstance(window_seconds, int)
            or isinstance(window_seconds, bool)
            or window_seconds < 1
        ):
            raise ValueError("Rate-limit window must be a positive integer.")

        now = max(0, int(self.clock()))
        window_start = now - (now % window_seconds)
        bucket_hash = self._digest(bucket_key)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            if count >= limit:
                connection.execute("COMMIT")
                return RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(1, window_start + window_seconds - now),
                )
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
            connection.execute("COMMIT")
            return RateLimitDecision(
                allowed=True,
                remaining=max(0, limit - count - 1),
                retry_after_seconds=0,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def request_peer_ip(request: Request) -> str:
    """Return the direct TCP peer only; never trust forwarded headers implicitly."""
    peer = request.client.host if request.client else ""
    try:
        return str(ipaddress.ip_address(peer))
    except ValueError:
        return "unknown"


def auth_rate_limit_keys(request: Request, identifier: str | None = None) -> tuple[str, ...]:
    """Build independent IP and account buckets without persisting raw PII."""
    keys = [f"auth:ip:{request_peer_ip(request)}"]
    if identifier:
        normalized = identifier.strip().casefold()
        if normalized:
            keys.append(f"auth:identity:{normalized}")
    return tuple(keys)
