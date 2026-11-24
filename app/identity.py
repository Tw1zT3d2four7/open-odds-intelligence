"""Isolated identity storage for subscriber and administrator accounts.

This module is intentionally not wired into HTTP routes yet. Account creation,
authentication, and session operations are server-side primitives; administrator
provisioning must only be invoked by a private operator-controlled workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
import re
import secrets
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
import pyotp

from . import config


_SCHEMA_VERSION = 3
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_database_file()
        self.initialize()

    def _secure_database_file(self) -> None:
        """Create or tighten the database file before SQLite can write secrets."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
                0o600,
            )
        except FileExistsError:
            # O_NONBLOCK prevents a malicious or accidental FIFO path from
            # hanging startup. O_NOFOLLOW rejects symlink substitution.
            descriptor = os.open(
                self.path, os.O_RDONLY | os.O_NONBLOCK | nofollow
            )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Identity database path must be a regular file.")
            # Tighten permissions before SQLite opens the file or migrates it.
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _record_audit_event(
        self,
        db: sqlite3.Connection,
        event_type: str,
        identity_id: str,
        occurred_at: str,
        metadata: dict | None = None,
        actor_identity_id: str | None = None,
    ) -> None:
        """Write a secret-free, attributable audit event in the caller's transaction."""
        db.execute(
            """INSERT INTO security_audit_events(
                id, actor_identity_id, event_type, subject_identity_id,
                occurred_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_urlsafe(18),
                actor_identity_id,
                event_type,
                identity_id,
                occurred_at,
                json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
            ),
        )

    def _resolve_audit_actor(
        self, db: sqlite3.Connection, subject_identity_id: str,
        actor_identity_id: str | None,
    ) -> str:
        """Allow self-service events or require an active administrator actor."""
        if actor_identity_id is None or actor_identity_id == subject_identity_id:
            return subject_identity_id
        actor = db.execute(
            """SELECT account_type, is_active FROM identities WHERE id=?""",
            (actor_identity_id,),
        ).fetchone()
        if (
            actor is None
            or actor["account_type"] != "administrator"
            or not actor["is_active"]
        ):
            raise ValueError("An active administrator is required for cross-account actions.")
        return actor_identity_id

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
                if current < 2:
                    db.execute("ALTER TABLE administrator_mfa ADD COLUMN last_totp_counter INTEGER")
                    db.execute("""CREATE TABLE administrator_mfa_recovery_codes (
                        id TEXT PRIMARY KEY,
                        identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                        code_hash TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        consumed_at TEXT
                    )""")
                    db.execute("CREATE INDEX idx_admin_mfa_recovery_identity ON administrator_mfa_recovery_codes(identity_id)")
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (2, _timestamp(_now())),
                    )
                if current < 3:
                    db.execute("ALTER TABLE administrator_mfa ADD COLUMN mfa_login_verified_at TEXT")
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (3, _timestamp(_now())),
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

    def provision_initial_administrator(self, email: str, password: str) -> str:
        """Atomically create the first and only bootstrap administrator."""
        normalized_email = _normalize_email(email)
        password_hash = _password_hash(password)
        now = _timestamp(_now())
        identity_id = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) AS count FROM identities WHERE account_type='administrator'"
            ).fetchone()["count"]
            if count:
                db.rollback()
                raise ValueError("An administrator already exists; initial bootstrap is closed.")
            db.execute(
                """INSERT INTO identities(
                    id, email, account_type, password_hash, created_at, updated_at,
                    password_changed_at
                ) VALUES (?, ?, 'administrator', ?, ?, ?, ?)""",
                (identity_id, normalized_email, password_hash, now, now, now),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'administrator.initially_provisioned', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, now),
            )
            db.commit()
        return identity_id

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
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO identities(
                    id, email, account_type, password_hash, created_at, updated_at,
                    password_changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (identity_id, normalized_email, account_type, password_hash, now, now, now),
            )
            event_type = (
                "subscriber.registered"
                if account_type == "subscriber"
                else "administrator.provisioned"
            )
            self._record_audit_event(
                db,
                event_type,
                identity_id,
                now,
                actor_identity_id=identity_id if account_type == "subscriber" else None,
            )
            db.commit()
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
                """SELECT id, password_hash, email_verified_at FROM identities
                   WHERE email = ? AND account_type = ? AND is_active = 1""",
                (normalized_email, account_type),
            ).fetchone()
            # Subscriber credentials are not sufficient until the address is
            # verified. Administrators use mandatory MFA as their second factor.
            if (
                row is not None
                and account_type == "subscriber"
                and row["email_verified_at"] is None
            ):
                row = None
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

    def _issue_challenge(
        self,
        email: str,
        table: str,
        lifetime: timedelta,
        account_type: str | None = None,
    ) -> str | None:
        """Issue a one-time challenge; only its SHA-256 digest is persisted."""
        if table not in {"email_verification_challenges", "password_reset_challenges"}:
            raise ValueError("Unsupported challenge type.")
        maximum = timedelta(hours=48) if table == "email_verification_challenges" else timedelta(hours=1)
        if lifetime <= timedelta(0) or lifetime > maximum:
            raise ValueError("Challenge lifetime is outside the permitted range.")
        try:
            normalized_email = _normalize_email(email)
        except ValueError:
            return None
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            identity_query = "SELECT id FROM identities WHERE email=? AND is_active=1"
            params: tuple = (normalized_email,)
            if account_type is not None:
                identity_query += " AND account_type=?"
                params = (normalized_email, account_type)
            identity = db.execute(identity_query, params).fetchone()
            if identity is None:
                db.commit()
                return None
            # A newly issued challenge invalidates any earlier outstanding link.
            db.execute(
                f"UPDATE {table} SET consumed_at=? WHERE identity_id=? AND consumed_at IS NULL",
                (_timestamp(now), identity["id"]),
            )
            db.execute(
                f"""INSERT INTO {table}(
                    id, identity_id, token_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    secrets.token_urlsafe(18), identity["id"], _token_digest(token),
                    _timestamp(now), _timestamp(now + lifetime),
                ),
            )
            db.commit()
        return token

    def create_email_verification_challenge(
        self, email: str, lifetime: timedelta = timedelta(hours=24)
    ) -> str | None:
        """Create a verification link for an active subscriber only."""
        return self._issue_challenge(
            email, "email_verification_challenges", lifetime, "subscriber"
        )

    def consume_email_verification_challenge(self, token: str) -> bool:
        """Consume a valid verification link exactly once."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            challenge = db.execute(
                """SELECT c.id, c.identity_id FROM email_verification_challenges c
                   JOIN identities i ON i.id=c.identity_id
                   WHERE c.token_hash=? AND c.consumed_at IS NULL
                     AND c.expires_at>? AND i.is_active=1
                     AND i.account_type='subscriber'""",
                (_token_digest(token), now),
            ).fetchone()
            if challenge is None:
                db.rollback()
                return False
            db.execute(
                "UPDATE email_verification_challenges SET consumed_at=? WHERE id=?",
                (now, challenge["id"]),
            )
            db.execute(
                "UPDATE identities SET email_verified_at=?, updated_at=? WHERE id=?",
                (now, now, challenge["identity_id"]),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'subscriber.email_verified', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), challenge["identity_id"], now),
            )
            db.commit()
        return True

    def create_password_reset_challenge(
        self, email: str, lifetime: timedelta = timedelta(minutes=30)
    ) -> str | None:
        """Create a short-lived password reset link for an active identity.

        HTTP handlers must return the same response whether this returns a token
        or None, so account existence is never disclosed to unauthenticated users.
        """
        return self._issue_challenge(email, "password_reset_challenges", lifetime)

    def consume_password_reset_challenge(self, token: str, new_password: str) -> bool:
        """Atomically consume a reset link, change the password, and revoke sessions."""
        password_hash = _password_hash(new_password)
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            challenge = db.execute(
                """SELECT c.id, c.identity_id FROM password_reset_challenges c
                   JOIN identities i ON i.id=c.identity_id
                   WHERE c.token_hash=? AND c.consumed_at IS NULL
                     AND c.expires_at>? AND i.is_active=1""",
                (_token_digest(token), now),
            ).fetchone()
            if challenge is None:
                db.rollback()
                return False
            db.execute(
                "UPDATE password_reset_challenges SET consumed_at=? WHERE id=?",
                (now, challenge["id"]),
            )
            db.execute(
                """UPDATE identities SET password_hash=?, password_changed_at=?,
                   updated_at=? WHERE id=?""",
                (password_hash, now, now, challenge["identity_id"]),
            )
            db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (now, challenge["identity_id"]),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'identity.password_reset', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), challenge["identity_id"], now),
            )
            db.commit()
        return True


    def list_security_audit_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
        actor_identity_id: str | None = None,
        subject_identity_id: str | None = None,
    ) -> dict:
        """Return a bounded, paginated audit view without credential material.

        This is a storage primitive for the future administrator-only audit API;
        it is not exposed over HTTP and does not return identity email addresses.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Audit page size must be between 1 and 500.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Audit offset must be a non-negative integer.")
        filters = []
        values: list[str | int] = []
        for column, value in (
            ("event_type", event_type),
            ("actor_identity_id", actor_identity_id),
            ("subject_identity_id", subject_identity_id),
        ):
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ValueError("Audit filters must be non-empty bounded strings.")
                filters.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) AS count FROM security_audit_events" + where,
                values,
            ).fetchone()["count"]
            rows = db.execute(
                """SELECT id, actor_identity_id, event_type, subject_identity_id,
                          occurred_at, metadata_json
                   FROM security_audit_events"""
                + where
                + " ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        events = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                raise RuntimeError("Audit event metadata is invalid.") from None
            if not isinstance(metadata, dict):
                raise RuntimeError("Audit event metadata is invalid.")
            events.append({
                "id": row["id"],
                "actor_identity_id": row["actor_identity_id"],
                "event_type": row["event_type"],
                "subject_identity_id": row["subject_identity_id"],
                "occurred_at": row["occurred_at"],
                "metadata": metadata,
            })
        return {
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _mfa_cipher(self) -> Fernet:
        key = config.MFA_ENCRYPTION_KEY
        if not key:
            raise RuntimeError("MFA_ENCRYPTION_KEY must be configured before administrator MFA.")
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("MFA_ENCRYPTION_KEY must be a valid Fernet key.") from exc

    def start_administrator_mfa_enrollment(self, identity_id: str) -> str:
        """Create a pending encrypted TOTP seed and return its provisioning URI once."""
        cipher = self._mfa_cipher()
        secret = pyotp.random_base32()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            identity = db.execute(
                "SELECT email FROM identities WHERE id=? AND account_type='administrator' AND is_active=1",
                (identity_id,),
            ).fetchone()
            if identity is None:
                db.rollback()
                raise ValueError("An active administrator identity is required.")
            existing_mfa = db.execute(
                "SELECT enrolled_at FROM administrator_mfa WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
            if existing_mfa and existing_mfa["enrolled_at"] is not None:
                db.rollback()
                raise ValueError("Administrator MFA is already enrolled; use the verified recovery process.")
            db.execute(
                """INSERT INTO administrator_mfa(
                    identity_id, encrypted_secret, enrolled_at, last_verified_at,
                    last_totp_counter, mfa_login_verified_at
                ) VALUES (?, ?, NULL, NULL, NULL, NULL)
                ON CONFLICT(identity_id) DO UPDATE SET
                    encrypted_secret=excluded.encrypted_secret,
                    enrolled_at=NULL, last_verified_at=NULL, last_totp_counter=NULL,
                    mfa_login_verified_at=NULL""",
                (identity_id, cipher.encrypt(secret.encode("utf-8"))),
            )
            db.execute(
                "DELETE FROM administrator_mfa_recovery_codes WHERE identity_id=?",
                (identity_id,),
            )
            db.commit()
        return pyotp.TOTP(secret).provisioning_uri(name=identity["email"], issuer_name=config.APP_NAME)

    def confirm_administrator_mfa_enrollment(self, identity_id: str, code: str) -> list[str]:
        """Confirm pending TOTP enrollment; return single-use recovery codes exactly once."""
        if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
            raise ValueError("A valid six-digit authenticator code is required.")
        cipher = self._mfa_cipher()
        recovery_codes = [secrets.token_urlsafe(9) for _ in range(10)]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now_dt = _now()
            now = _timestamp(now_dt)
            row = db.execute(
                """SELECT m.encrypted_secret FROM administrator_mfa m
                   JOIN identities i ON i.id=m.identity_id
                   WHERE m.identity_id=? AND m.enrolled_at IS NULL
                     AND i.account_type='administrator' AND i.is_active=1""",
                (identity_id,),
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("No pending administrator MFA enrollment exists.")
            try:
                secret = cipher.decrypt(row["encrypted_secret"]).decode("utf-8")
            except (InvalidToken, ValueError, TypeError) as exc:
                db.rollback()
                raise RuntimeError("Administrator MFA seed could not be decrypted.") from exc
            totp = pyotp.TOTP(secret)
            if not totp.verify(code, for_time=now_dt, valid_window=1):
                db.rollback()
                raise ValueError("Authenticator code is invalid.")
            current_counter = totp.timecode(now_dt)
            matched_counter = next(
                counter for counter in (current_counter - 1, current_counter, current_counter + 1)
                if totp.verify(code, for_time=counter * totp.interval)
            )
            db.execute(
                """UPDATE administrator_mfa
                   SET enrolled_at=?, last_verified_at=?, last_totp_counter=?
                   WHERE identity_id=?""",
                (now, now, matched_counter, identity_id),
            )
            db.executemany(
                """INSERT INTO administrator_mfa_recovery_codes(
                    id, identity_id, code_hash, created_at
                ) VALUES (?, ?, ?, ?)""",
                [(secrets.token_urlsafe(18), identity_id, _token_digest(item), now) for item in recovery_codes],
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'administrator.mfa_enrolled', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, now),
            )
            db.commit()
        return recovery_codes

    def verify_administrator_mfa(self, identity_id: str, code: str) -> bool:
        """Verify a fresh TOTP time-step; previously accepted codes cannot replay."""
        if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
            return False
        cipher = self._mfa_cipher()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now_dt = _now()
            now = _timestamp(now_dt)
            row = db.execute(
                """SELECT m.encrypted_secret, m.last_totp_counter
                   FROM administrator_mfa m JOIN identities i ON i.id=m.identity_id
                   WHERE m.identity_id=? AND m.enrolled_at IS NOT NULL
                     AND i.account_type='administrator' AND i.is_active=1""",
                (identity_id,),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            try:
                secret = cipher.decrypt(row["encrypted_secret"]).decode("utf-8")
            except (InvalidToken, ValueError, TypeError) as exc:
                db.rollback()
                raise RuntimeError("Administrator MFA seed could not be decrypted.") from exc
            totp = pyotp.TOTP(secret)
            current_counter = totp.timecode(now_dt)
            previous_counter = row["last_totp_counter"] if row["last_totp_counter"] is not None else -1
            matched_counter = next(
                (
                    counter for counter in (current_counter - 1, current_counter, current_counter + 1)
                    if counter > previous_counter and totp.verify(code, for_time=counter * totp.interval)
                ),
                None,
            )
            if matched_counter is None:
                db.rollback()
                return False
            db.execute(
                "UPDATE administrator_mfa SET last_totp_counter=?, last_verified_at=?, mfa_login_verified_at=? WHERE identity_id=?",
                (matched_counter, now, now, identity_id),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'administrator.mfa_verified', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, now),
            )
            db.commit()
        return True

    def consume_administrator_recovery_code(self, identity_id: str, code: str) -> bool:
        """Atomically consume one hashed, single-use administrator recovery code."""
        if not isinstance(code, str) or not code or len(code) > 128:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            row = db.execute(
                """SELECT r.id FROM administrator_mfa_recovery_codes r
                   JOIN identities i ON i.id=r.identity_id
                   JOIN administrator_mfa m ON m.identity_id=i.id
                   WHERE r.identity_id=? AND r.code_hash=? AND r.consumed_at IS NULL
                     AND i.account_type='administrator' AND i.is_active=1
                     AND m.enrolled_at IS NOT NULL""",
                (identity_id, _token_digest(code)),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            db.execute("UPDATE administrator_mfa_recovery_codes SET consumed_at=? WHERE id=?", (now, row["id"]))
            db.execute(
                "UPDATE administrator_mfa SET mfa_login_verified_at=? WHERE identity_id=?",
                (now, identity_id),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'administrator.mfa_recovery_used', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, now),
            )
            db.commit()
        return True

    def create_session(
        self,
        identity_id: str,
        lifetime: timedelta = timedelta(hours=12),
    ) -> tuple[str, str]:
        """Return raw opaque token once and its server-side session ID."""
        if lifetime <= timedelta(0) or lifetime > timedelta(days=30):
            raise ValueError("Session lifetime must be greater than zero and at most 30 days.")
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_urlsafe(18)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            now_text = _timestamp(now)
            identity = db.execute(
                """SELECT is_active, account_type, email_verified_at
                   FROM identities WHERE id=?""",
                (identity_id,),
            ).fetchone()
            if identity is None or not identity["is_active"]:
                db.rollback()
                raise ValueError("An active identity is required to create a session.")
            if (
                identity["account_type"] == "subscriber"
                and identity["email_verified_at"] is None
            ):
                db.rollback()
                raise ValueError("A verified subscriber email is required to create a session.")
            if identity["account_type"] == "administrator":
                mfa = db.execute(
                    "SELECT mfa_login_verified_at FROM administrator_mfa WHERE identity_id=? AND enrolled_at IS NOT NULL",
                    (identity_id,),
                ).fetchone()
                verified_at = datetime.fromisoformat(mfa["mfa_login_verified_at"]) if mfa and mfa["mfa_login_verified_at"] else None
                if verified_at is None or now - verified_at > timedelta(minutes=5) or verified_at > now:
                    db.rollback()
                    raise ValueError("A recent, unused administrator MFA verification is required.")
                # Consume the one-time MFA proof in the same transaction as session creation.
                db.execute(
                    "UPDATE administrator_mfa SET mfa_login_verified_at=NULL WHERE identity_id=?",
                    (identity_id,),
                )
            db.execute(
                """INSERT INTO sessions(
                    id, identity_id, token_hash, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id, identity_id, _token_digest(token),
                    now_text, now_text, _timestamp(now + lifetime),
                ),
            )
            self._record_audit_event(
                db,
                f"{identity['account_type']}.session_created",
                identity_id,
                now_text,
                actor_identity_id=identity_id,
            )
            db.commit()
        return token, session_id

    def get_session(self, token: str) -> dict | None:
        """Resolve an active session without exposing its stored token digest."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        with self.connect() as db:
            # Serialize session resolution with revocation/deactivation writes.
            # Otherwise a revoke could commit after SELECT but before the
            # last-seen update, allowing one stale request to pass.
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            row = db.execute(
                """SELECT s.id AS session_id, s.identity_id, s.expires_at,
                          s.last_seen_at, i.email, i.account_type
                   FROM sessions s JOIN identities i ON i.id=s.identity_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL
                     AND s.expires_at>? AND i.is_active=1
                     AND (i.account_type='administrator'
                          OR i.email_verified_at IS NOT NULL)""",
                (_token_digest(token), now),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            idle_cutoff = _timestamp(
                _now() - timedelta(seconds=config.SESSION_IDLE_TIMEOUT_SECONDS)
            )
            if row["last_seen_at"] <= idle_cutoff:
                db.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (now, row["session_id"]),
                )
                db.commit()
                return None
            db.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=? AND revoked_at IS NULL",
                (now, row["session_id"]),
            )
            db.commit()
        result = dict(row)
        result.pop("last_seen_at", None)
        return result

    def revoke_session(
        self, token: str, *, actor_identity_id: str | None = None
    ) -> bool:
        """Revoke a session by its raw token; safe to call repeatedly."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT identity_id FROM sessions
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (_token_digest(token),),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            try:
                audit_actor = self._resolve_audit_actor(
                    db, row["identity_id"], actor_identity_id
                )
            except ValueError:
                db.rollback()
                raise
            now = _timestamp(_now())
            db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (now, _token_digest(token)),
            )
            self._record_audit_event(
                db,
                "identity.session_revoked",
                row["identity_id"],
                now,
                actor_identity_id=audit_actor,
            )
            db.commit()
        return True

    def revoke_all_sessions(
        self, identity_id: str, *, actor_identity_id: str | None = None
    ) -> int:
        """Revoke every active session, e.g. after password reset or suspension."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                audit_actor = self._resolve_audit_actor(
                    db, identity_id, actor_identity_id
                )
            except ValueError:
                db.rollback()
                raise
            now = _timestamp(_now())
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (now, identity_id),
            )
            revoked_count = cursor.rowcount
            if revoked_count:
                self._record_audit_event(
                    db,
                    "identity.sessions_revoked",
                    identity_id,
                    now,
                    {"revoked_count": revoked_count},
                    actor_identity_id=audit_actor,
                )
            db.commit()
        return revoked_count
