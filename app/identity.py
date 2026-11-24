"""Isolated identity storage for subscriber and administrator accounts.

This module is intentionally not wired into HTTP routes yet. Account creation,
authentication, and session operations are server-side primitives; administrator
provisioning must only be invoked by a private operator-controlled workflow.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import config


_SCHEMA_VERSION = 1
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("identity-lookup-dummy-password")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ACCOUNT_TYPES = frozenset({"subscriber", "administrator"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    normalized = str(email or "").strip().casefold()
    if len(normalized) > 254 or not _EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("A valid email address is required.")
    return normalized


def _password_hash(password: str) -> str:
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    if len(password) > 1024:
        raise ValueError("Password is too long.")
    return _PASSWORD_HASHER.hash(password)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class IdentityStore:
    """SQLite-backed identities and opaque, revocable server-side sessions."""

    def __init__(self, path: str | Path | None = None):
        configured_path = path if path is not None else config.ACCOUNTS_DATABASE_PATH
        self.path = Path(configured_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        """Yield a configured connection and always close it after use."""
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Apply additive, versioned schema migrations without touching odds data."""
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                """)
                current = db.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()["version"]
                if current > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Identity database schema {current} is newer than supported "
                        f"schema {_SCHEMA_VERSION}."
                    )
                if current < 1:
                    statements = (
                        """CREATE TABLE identities (
                            id TEXT PRIMARY KEY,
                            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                            account_type TEXT NOT NULL
                                CHECK(account_type IN ('subscriber', 'administrator')),
                            password_hash TEXT NOT NULL,
                            is_active INTEGER NOT NULL DEFAULT 1
                                CHECK(is_active IN (0, 1)),
                            email_verified_at TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            password_changed_at TEXT NOT NULL
                        )""",
                        """CREATE TABLE sessions (
                            id TEXT PRIMARY KEY,
                            identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                            token_hash TEXT NOT NULL UNIQUE,
                            created_at TEXT NOT NULL,
                            last_seen_at TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            revoked_at TEXT
                        )""",
                        "CREATE INDEX idx_sessions_identity ON sessions(identity_id)",
                        "CREATE INDEX idx_sessions_expiry ON sessions(expires_at)",
                        """CREATE TABLE email_verification_challenges (
                            id TEXT PRIMARY KEY,
                            identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                            token_hash TEXT NOT NULL UNIQUE,
                            created_at TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            consumed_at TEXT
                        )""",
                        """CREATE TABLE password_reset_challenges (
                            id TEXT PRIMARY KEY,
                            identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                            token_hash TEXT NOT NULL UNIQUE,
                            created_at TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            consumed_at TEXT
                        )""",
                        """CREATE TABLE administrator_mfa (
                            identity_id TEXT PRIMARY KEY REFERENCES identities(id) ON DELETE CASCADE,
                            encrypted_secret BLOB,
                            enrolled_at TEXT,
                            last_verified_at TEXT
                        )""",
                        """CREATE TABLE security_audit_events (
                            id TEXT PRIMARY KEY,
                            actor_identity_id TEXT REFERENCES identities(id) ON DELETE SET NULL,
                            event_type TEXT NOT NULL,
                            subject_identity_id TEXT REFERENCES identities(id) ON DELETE SET NULL,
                            occurred_at TEXT NOT NULL,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        )""",
                        "CREATE INDEX idx_security_audit_time ON security_audit_events(occurred_at)",
                    )
                    for statement in statements:
                        db.execute(statement)
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (1, _timestamp(_now())),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        # Identity material is sensitive. Restrict the main DB and SQLite sidecars.
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                candidate.chmod(0o600)
            except FileNotFoundError:
                pass

    def create_subscriber(self, email: str, password: str) -> str:
        """Create an unverified subscriber. Public routes must call this only."""
        return self._create_identity(email, password, "subscriber")

    def provision_administrator(self, email: str, password: str) -> str:
        """Privileged bootstrap primitive; never expose through public HTTP routes."""
        return self._create_identity(email, password, "administrator")

    def _create_identity(self, email: str, password: str, account_type: str) -> str:
        if account_type not in _ACCOUNT_TYPES:
            raise ValueError("Unsupported account type.")
        normalized_email = _normalize_email(email)
        password_hash = _password_hash(password)
        now = _timestamp(_now())
        identity_id = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute(
                """INSERT INTO identities(
                    id, email, account_type, password_hash, created_at, updated_at,
                    password_changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (identity_id, normalized_email, account_type, password_hash, now, now, now),
            )
        return identity_id

    def authenticate(self, email: str, password: str, account_type: str) -> str | None:
        """Verify credentials for exactly one account class; MFA is a later admin gate."""
        if account_type not in _ACCOUNT_TYPES:
            raise ValueError("Unsupported account type.")
        if not isinstance(password, str) or len(password) > 1024:
            return None
        try:
            normalized_email = _normalize_email(email)
        except ValueError:
            try:
                _PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
                pass
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT id, password_hash FROM identities
                   WHERE email = ? AND account_type = ? AND is_active = 1""",
                (normalized_email, account_type),
            ).fetchone()
        if row is None:
            try:
                _PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
                pass
            return None
        try:
            valid = _PASSWORD_HASHER.verify(row["password_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
            return None
        if not valid:
            return None
        if _PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            replacement = _PASSWORD_HASHER.hash(password)
            with self.connect() as db:
                db.execute(
                    "UPDATE identities SET password_hash=?, updated_at=? WHERE id=?",
                    (replacement, _timestamp(_now()), row["id"]),
                )
        return row["id"]

    def create_session(
        self,
        identity_id: str,
        lifetime: timedelta = timedelta(hours=12),
    ) -> tuple[str, str]:
        """Return raw opaque token once and its server-side session ID."""
        if lifetime <= timedelta(0) or lifetime > timedelta(days=30):
            raise ValueError("Session lifetime must be greater than zero and at most 30 days.")
        now = _now()
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_urlsafe(18)
        with self.connect() as db:
            identity = db.execute(
                "SELECT is_active FROM identities WHERE id=?", (identity_id,)
            ).fetchone()
            if identity is None or not identity["is_active"]:
                raise ValueError("An active identity is required to create a session.")
            db.execute(
                """INSERT INTO sessions(
                    id, identity_id, token_hash, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id, identity_id, _token_digest(token),
                    _timestamp(now), _timestamp(now), _timestamp(now + lifetime),
                ),
            )
        return token, session_id

    def get_session(self, token: str) -> dict | None:
        """Resolve an active session without exposing its stored token digest."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        now = _timestamp(_now())
        with self.connect() as db:
            row = db.execute(
                """SELECT s.id AS session_id, s.identity_id, s.expires_at,
                          s.last_seen_at, i.email, i.account_type
                   FROM sessions s JOIN identities i ON i.id=s.identity_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL
                     AND s.expires_at>? AND i.is_active=1""",
                (_token_digest(token), now),
            ).fetchone()
            if row is None:
                return None
            idle_cutoff = _timestamp(
                _now() - timedelta(seconds=config.SESSION_IDLE_TIMEOUT_SECONDS)
            )
            if row["last_seen_at"] <= idle_cutoff:
                db.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (now, row["session_id"]),
                )
                return None
            db.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?",
                (now, row["session_id"]),
            )
        result = dict(row)
        result.pop("last_seen_at", None)
        return result

    def revoke_session(self, token: str) -> bool:
        """Revoke a session by its raw token; safe to call repeatedly."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (_timestamp(_now()), _token_digest(token)),
            )
        return cursor.rowcount == 1

    def revoke_all_sessions(self, identity_id: str) -> int:
        """Revoke every active session, e.g. after password reset or suspension."""
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (_timestamp(_now()), identity_id),
            )
        return cursor.rowcount
