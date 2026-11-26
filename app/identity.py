"""SQLite identity storage for subscriber and administrator accounts.

The isolated identity router uses these server-side primitives. Administrator
provisioning remains restricted to a private operator-controlled workflow.
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
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
import pyotp

from . import config


_SCHEMA_VERSION = 15
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("identity-lookup-dummy-password")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ACCOUNT_TYPES = frozenset({"subscriber", "administrator"})
_SUBSCRIBER_STATUS_REASON_CODES = frozenset({
    "support_request", "policy_violation", "security_concern", "billing_issue", "other",
})
_ENTITLEMENT_REASON_CODES = frozenset({
    "support_request", "trial_grant", "promotional_access", "billing_adjustment", "other",
})
_COMPLIMENTARY_REASON_CODES = frozenset({
    "giveaway_winner", "promotional_campaign", "sponsorship", "service_recovery", "other",
})
_ENTITLEMENT_KEYS = frozenset({
    "dashboard", "odds", "market_limits", "market_board", "simulation",
    "player_props", "clv", "market_movement", "history",
    "opportunities", "arbitrage",
})


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
            """SELECT i.account_type, i.is_active, m.enrolled_at
               FROM identities i
               LEFT JOIN administrator_mfa m ON m.identity_id=i.id
               WHERE i.id=?""",
            (actor_identity_id,),
        ).fetchone()
        if (
            actor is None
            or actor["account_type"] != "administrator"
            or not actor["is_active"]
            or actor["enrolled_at"] is None
        ):
            raise ValueError(
                "An active MFA-enrolled administrator is required for cross-account actions."
            )
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
                if current < 4:
                    db.execute("""CREATE TABLE email_delivery_events (
                        id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL CHECK(provider IN ('resend')),
                        identity_id TEXT REFERENCES identities(id) ON DELETE SET NULL,
                        purpose TEXT NOT NULL CHECK(purpose IN ('verification', 'password_reset')),
                        status TEXT NOT NULL CHECK(status IN ('sent', 'failed', 'skipped')),
                        provider_message_id TEXT,
                        failure_code TEXT,
                        attempted_at TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX idx_email_delivery_identity ON email_delivery_events(identity_id, attempted_at)")
                    db.execute("CREATE INDEX idx_email_delivery_time ON email_delivery_events(attempted_at)")
                    db.execute("""CREATE TABLE email_provider_circuit (
                        provider TEXT PRIMARY KEY CHECK(provider IN ('resend')),
                        consecutive_failures INTEGER NOT NULL DEFAULT 0
                            CHECK(consecutive_failures >= 0),
                        opened_until TEXT,
                        last_failure_at TEXT,
                        last_success_at TEXT,
                        last_error_code TEXT,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (4, _timestamp(_now())),
                    )
                if current < 5:
                    db.execute("ALTER TABLE email_provider_circuit ADD COLUMN probe_until TEXT")
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (5, _timestamp(_now())),
                    )
                if current < 6:
                    db.execute("""CREATE TABLE transactional_email_queue (
                        id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL CHECK(provider IN ('resend')),
                        identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                        purpose TEXT NOT NULL CHECK(purpose IN ('verification', 'password_reset')),
                        encrypted_payload BLOB NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL DEFAULT 'pending'
                            CHECK(state IN ('pending', 'leased', 'sent', 'failed', 'expired')),
                        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                        max_attempts INTEGER NOT NULL DEFAULT 8 CHECK(max_attempts BETWEEN 1 AND 12),
                        next_attempt_at TEXT NOT NULL,
                        lease_token_hash TEXT,
                        lease_expires_at TEXT,
                        challenge_expires_at TEXT NOT NULL,
                        idempotency_expires_at TEXT NOT NULL,
                        last_failure_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        sent_at TEXT
                    )""")
                    db.execute(
                        "CREATE INDEX idx_email_queue_due ON transactional_email_queue(state, next_attempt_at, lease_expires_at)"
                    )
                    db.execute(
                        "CREATE INDEX idx_email_queue_identity ON transactional_email_queue(identity_id, created_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (6, _timestamp(_now())),
                    )
                if current < 7:
                    db.execute("ALTER TABLE transactional_email_queue ADD COLUMN challenge_token_hash TEXT")
                    db.execute(
                        "CREATE INDEX idx_email_queue_challenge ON transactional_email_queue(purpose, identity_id, challenge_token_hash)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (7, _timestamp(_now())),
                    )
                if current < 8:
                    db.execute("""CREATE TABLE subscriber_entitlements (
                        id TEXT PRIMARY KEY,
                        identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                        entitlement_key TEXT NOT NULL,
                        starts_at TEXT NOT NULL,
                        expires_at TEXT,
                        granted_by TEXT REFERENCES identities(id) ON DELETE SET NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(identity_id, entitlement_key)
                    )""")
                    db.execute(
                        "CREATE INDEX idx_subscriber_entitlements_active ON subscriber_entitlements(identity_id, entitlement_key, starts_at, expires_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (8, _timestamp(_now())),
                    )
                if current < 9:
                    db.execute("""CREATE TABLE billing_customers (
                        identity_id TEXT PRIMARY KEY REFERENCES identities(id) ON DELETE CASCADE,
                        stripe_customer_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE billing_subscriptions (
                        id TEXT PRIMARY KEY,
                        identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                        stripe_subscription_id TEXT NOT NULL UNIQUE,
                        stripe_customer_id TEXT NOT NULL REFERENCES billing_customers(stripe_customer_id) ON DELETE RESTRICT,
                        stripe_price_id TEXT,
                        plan_code TEXT,
                        billing_interval TEXT CHECK(
                            billing_interval IS NULL OR billing_interval IN ('month', 'year')
                        ),
                        status TEXT NOT NULL CHECK(status IN (
                            'incomplete', 'incomplete_expired', 'trialing', 'active',
                            'past_due', 'canceled', 'unpaid', 'paused'
                        )),
                        current_period_start TEXT,
                        current_period_end TEXT,
                        cancel_at_period_end INTEGER NOT NULL DEFAULT 0
                            CHECK(cancel_at_period_end IN (0, 1)),
                        canceled_at TEXT,
                        ended_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute(
                        "CREATE INDEX idx_billing_subscriptions_identity ON billing_subscriptions(identity_id, status, updated_at)"
                    )
                    db.execute("""CREATE TABLE billing_webhook_events (
                        stripe_event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        livemode INTEGER NOT NULL CHECK(livemode IN (0, 1)),
                        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                        processing_state TEXT NOT NULL DEFAULT 'received'
                            CHECK(processing_state IN ('received', 'processing', 'processed', 'failed')),
                        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                        received_at TEXT NOT NULL,
                        processed_at TEXT,
                        last_error_code TEXT
                    )""")
                    db.execute(
                        "CREATE INDEX idx_billing_webhook_pending ON billing_webhook_events(processing_state, received_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (9, _timestamp(_now())),
                    )
                if current < 10:
                    db.execute("ALTER TABLE billing_webhook_events ADD COLUMN lease_token_hash TEXT")
                    db.execute("ALTER TABLE billing_webhook_events ADD COLUMN lease_expires_at TEXT")
                    db.execute("ALTER TABLE billing_webhook_events ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                    db.execute(
                        "UPDATE billing_webhook_events SET updated_at=received_at WHERE updated_at=''"
                    )
                    db.execute(
                        "CREATE INDEX idx_billing_webhook_lease ON billing_webhook_events(processing_state, lease_expires_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (10, _timestamp(_now())),
                    )
                if current < 11:
                    db.execute("ALTER TABLE billing_webhook_events ADD COLUMN encrypted_payload TEXT")
                    # Pre-v11 inbox rows have no recoverable event body. Mark them
                    # retryable and lease-free; a Stripe redelivery will safely attach
                    # the newly verified encrypted body before processing.
                    db.execute(
                        """UPDATE billing_webhook_events
                           SET processing_state='failed',
                               last_error_code='payload_unavailable',
                               lease_token_hash=NULL, lease_expires_at=NULL,
                               updated_at=?
                           WHERE encrypted_payload IS NULL
                             AND processing_state != 'processed'""",
                        (_timestamp(_now()),),
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (11, _timestamp(_now())),
                    )
                if current < 12:
                    db.execute(
                        "ALTER TABLE billing_subscriptions ADD COLUMN last_event_created_at TEXT"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (12, _timestamp(_now())),
                    )
                if current < 13:
                    db.execute("""CREATE TABLE billing_invoices (
                        id TEXT PRIMARY KEY,
                        identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                        stripe_invoice_id TEXT NOT NULL UNIQUE,
                        stripe_customer_id TEXT NOT NULL REFERENCES billing_customers(stripe_customer_id) ON DELETE RESTRICT,
                        stripe_subscription_id TEXT REFERENCES billing_subscriptions(stripe_subscription_id) ON DELETE RESTRICT,
                        status TEXT NOT NULL CHECK(status IN (
                            'draft', 'open', 'paid', 'uncollectible', 'void'
                        )),
                        amount_due INTEGER CHECK(amount_due IS NULL OR amount_due >= 0),
                        amount_paid INTEGER CHECK(amount_paid IS NULL OR amount_paid >= 0),
                        currency TEXT,
                        period_start TEXT,
                        period_end TEXT,
                        last_event_created_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute(
                        "CREATE INDEX idx_billing_invoices_identity ON billing_invoices(identity_id, status, updated_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (13, _timestamp(_now())),
                    )
                if current < 14:
                    db.execute(
                        "ALTER TABLE billing_invoices ADD COLUMN payment_failed_at TEXT"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (14, _timestamp(_now())),
                    )
                if current < 15:
                    db.execute("""CREATE TABLE billing_checkout_attempts (
                        identity_id TEXT PRIMARY KEY REFERENCES identities(id) ON DELETE CASCADE,
                        billing_interval TEXT NOT NULL CHECK(
                            billing_interval IN ('month', 'year')
                        ),
                        idempotency_key_hash TEXT NOT NULL
                            CHECK(length(idempotency_key_hash) = 64),
                        attempt_token TEXT NOT NULL UNIQUE,
                        checkout_session_id TEXT UNIQUE,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute(
                        "CREATE INDEX idx_billing_checkout_expiry ON billing_checkout_attempts(expires_at)"
                    )
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (15, _timestamp(_now())),
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

    def record_stripe_webhook_event(
        self, event_id: str, event_type: str, livemode: bool, payload: bytes
    ) -> dict:
        """Persist a fingerprint and Fernet-encrypted verified event for durable processing."""
        if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
            raise ValueError("A valid Stripe event ID is required.")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            raise ValueError("A valid Stripe event type is required.")
        if type(livemode) is not bool:
            raise ValueError("Stripe livemode must be a Boolean.")
        if not isinstance(payload, bytes) or not payload or len(payload) > 1_048_576:
            raise ValueError("Stripe webhook payload must be between 1 byte and 1 MiB.")
        key = config.STRIPE_WEBHOOK_ENCRYPTION_KEY
        if not isinstance(key, str) or not key:
            raise ValueError("Stripe webhook encryption is not configured.")
        try:
            encrypted_payload = Fernet(key.encode("ascii")).encrypt(payload).decode("ascii")
        except (ValueError, UnicodeEncodeError):
            raise ValueError("Stripe webhook encryption configuration is invalid.") from None
        digest = hashlib.sha256(payload).hexdigest()
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """INSERT OR IGNORE INTO billing_webhook_events(
                    stripe_event_id, event_type, livemode, payload_sha256,
                    processing_state, attempt_count, received_at, updated_at,
                    encrypted_payload
                ) VALUES (?, ?, ?, ?, 'received', 0, ?, ?, ?)""",
                (event_id, event_type, int(livemode), digest, now, now, encrypted_payload),
            )
            row = db.execute(
                """SELECT event_type, livemode, payload_sha256, processing_state,
                          attempt_count, encrypted_payload
                   FROM billing_webhook_events WHERE stripe_event_id=?""",
                (event_id,),
            ).fetchone()
            if (
                row is None
                or row["event_type"] != event_type
                or row["livemode"] != int(livemode)
                or not secrets.compare_digest(row["payload_sha256"], digest)
            ):
                db.rollback()
                raise ValueError("Stripe event ID was reused with different event content.")
            if row["encrypted_payload"] is None:
                db.execute(
                    "UPDATE billing_webhook_events SET encrypted_payload=?, updated_at=? "
                    "WHERE stripe_event_id=? AND encrypted_payload IS NULL",
                    (encrypted_payload, now, event_id),
                )
            db.commit()
        return {
            "created": cursor.rowcount == 1,
            "processing_state": row["processing_state"],
            "attempt_count": row["attempt_count"],
        }

    def get_stripe_webhook_event_payload(self, event_id: str, lease_token: str) -> bytes | None:
        """Decrypt an event only for its current, unexpired processing lease."""
        if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
            return None
        if not isinstance(lease_token, str) or not lease_token:
            return None
        now = _timestamp(_now())
        with self.connect() as db:
            row = db.execute(
                """SELECT encrypted_payload FROM billing_webhook_events
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (event_id, _token_digest(lease_token), now),
            ).fetchone()
        if row is None or not row["encrypted_payload"]:
            return None
        key = config.STRIPE_WEBHOOK_ENCRYPTION_KEY
        if not isinstance(key, str) or not key:
            raise RuntimeError("Stripe webhook encryption is not configured.")
        try:
            return Fernet(key.encode("ascii")).decrypt(row["encrypted_payload"].encode("ascii"))
        except (InvalidToken, ValueError, UnicodeEncodeError):
            raise RuntimeError("Stored Stripe webhook event cannot be decrypted.") from None

    def claim_stripe_webhook_event(
        self, event_id: str, *, lease_seconds: int = 60, max_attempts: int = 8
    ) -> str | None:
        """Claim an event once; expired leases can be recovered after worker failure."""
        if not isinstance(lease_seconds, int) or not 15 <= lease_seconds <= 600:
            raise ValueError("Stripe webhook lease must be between 15 and 600 seconds.")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 12:
            raise ValueError("Stripe webhook attempts must be between 1 and 12.")
        now_dt = _now()
        now = _timestamp(now_dt)
        expires = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        token = secrets.token_urlsafe(32)
        token_hash = _token_digest(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT processing_state, attempt_count, lease_expires_at
                   FROM billing_webhook_events WHERE stripe_event_id=?""",
                (event_id,),
            ).fetchone()
            if row is None or row["processing_state"] == "processed":
                db.rollback()
                return None
            if row["attempt_count"] >= max_attempts:
                db.rollback()
                return None
            if (
                row["processing_state"] == "processing"
                and row["lease_expires_at"]
                and row["lease_expires_at"] > now
            ):
                db.rollback()
                return None
            db.execute(
                """UPDATE billing_webhook_events
                   SET processing_state='processing', attempt_count=attempt_count+1,
                       lease_token_hash=?, lease_expires_at=?, updated_at=?
                   WHERE stripe_event_id=?""",
                (token_hash, expires, now, event_id),
            )
            db.commit()
        return token

    def complete_stripe_webhook_event(self, event_id: str, lease_token: str) -> bool:
        """Complete only the currently leased attempt; stale workers cannot win."""
        if not isinstance(lease_token, str) or not lease_token:
            return False
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE billing_webhook_events
                   SET processing_state='processed', processed_at=?, updated_at=?,
                       lease_token_hash=NULL, lease_expires_at=NULL, last_error_code=NULL
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (now, now, event_id, _token_digest(lease_token), now),
            )
            db.commit()
        return cursor.rowcount == 1

    def fail_stripe_webhook_event(
        self, event_id: str, lease_token: str, error_code: str
    ) -> bool:
        """Release a failed attempt with a sanitized machine-readable code."""
        if not isinstance(lease_token, str) or not lease_token:
            return False
        if (
            not isinstance(error_code, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_code)
        ):
            raise ValueError("Stripe webhook failure code is invalid.")
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE billing_webhook_events
                   SET processing_state='failed', updated_at=?, last_error_code=?,
                       lease_token_hash=NULL, lease_expires_at=NULL
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (now, error_code.lower(), event_id, _token_digest(lease_token), now),
            )
            db.commit()
        return cursor.rowcount == 1

    def process_stripe_subscription_event(self, event_id: str, lease_token: str) -> str:
        """Apply a verified subscription snapshot and finish its inbox lease atomically.

        Only Stripe subscription lifecycle events are interpreted here. Invoice events
        remain recorded for a later reconciliation pass; subscription state alone never
        grants application entitlements.
        """
        if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
            return "invalid"
        if not isinstance(lease_token, str) or not lease_token:
            return "invalid"
        now_dt = _now()
        now = _timestamp(now_dt)
        key = config.STRIPE_WEBHOOK_ENCRYPTION_KEY
        if not isinstance(key, str) or not key:
            raise RuntimeError("Stripe webhook encryption is not configured.")
        try:
            cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise RuntimeError("Stripe webhook encryption configuration is invalid.") from None

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inbox = db.execute(
                """SELECT event_type, livemode, encrypted_payload
                   FROM billing_webhook_events
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (event_id, _token_digest(lease_token), now),
            ).fetchone()
            if inbox is None:
                db.rollback()
                return "lease_lost"
            if not inbox["encrypted_payload"]:
                db.rollback()
                raise RuntimeError("Stripe webhook event payload is unavailable.")
            try:
                raw = cipher.decrypt(inbox["encrypted_payload"].encode("ascii"))
                event = json.loads(raw)
            except (InvalidToken, ValueError, UnicodeEncodeError, json.JSONDecodeError):
                db.rollback()
                raise RuntimeError("Stored Stripe webhook event cannot be decoded.") from None

            event_type = inbox["event_type"]
            if event_type not in {
                "customer.subscription.created",
                "customer.subscription.updated",
                "customer.subscription.deleted",
            }:
                completed = db.execute(
                    """UPDATE billing_webhook_events
                       SET processing_state='processed', processed_at=?, updated_at=?,
                           lease_token_hash=NULL, lease_expires_at=NULL,
                           last_error_code=NULL
                       WHERE stripe_event_id=? AND processing_state='processing'
                         AND lease_token_hash=? AND lease_expires_at>?""",
                    (now, now, event_id, _token_digest(lease_token), now),
                )
                if completed.rowcount != 1:
                    db.rollback()
                    return "lease_lost"
                db.commit()
                return "ignored"

            try:
                event_created = int(event["created"])
                obj = event["data"]["object"]
                subscription_id = obj["id"]
                customer_id = obj["customer"]
                status = "canceled" if event_type.endswith(".deleted") else obj["status"]
            except (KeyError, TypeError, ValueError, OverflowError):
                db.rollback()
                raise ValueError("Stripe subscription event shape is invalid.") from None
            allowed_statuses = {
                "incomplete", "incomplete_expired", "trialing", "active",
                "past_due", "canceled", "unpaid", "paused",
            }
            if (
                not isinstance(subscription_id, str) or not subscription_id
                or len(subscription_id) > 255
                or not isinstance(customer_id, str) or not customer_id
                or len(customer_id) > 255
                or not isinstance(status, str) or status not in allowed_statuses
                or event_created < 0
            ):
                db.rollback()
                raise ValueError("Stripe subscription event contains invalid identifiers or status.")

            customer = db.execute(
                "SELECT identity_id FROM billing_customers WHERE stripe_customer_id=?",
                (customer_id,),
            ).fetchone()
            if customer is None:
                db.rollback()
                raise LookupError("Stripe customer is not linked to a local subscriber.")

            occurred_at = _timestamp(datetime.fromtimestamp(event_created, tz=timezone.utc))
            existing = db.execute(
                """SELECT identity_id, stripe_customer_id, last_event_created_at
                   FROM billing_subscriptions WHERE stripe_subscription_id=?""",
                (subscription_id,),
            ).fetchone()
            if existing is not None and (
                existing["identity_id"] != customer["identity_id"]
                or existing["stripe_customer_id"] != customer_id
            ):
                db.rollback()
                raise ValueError("Stripe subscription customer mapping conflicts with local data.")

            if existing is not None and existing["last_event_created_at"]:
                if existing["last_event_created_at"] >= occurred_at:
                    result = "stale"
                else:
                    result = "updated"
            else:
                result = "created" if existing is None else "updated"

            if result != "stale":
                items_container = obj.get("items", {})
                items = items_container.get("data", []) if isinstance(items_container, dict) else []
                first_item = items[0] if isinstance(items, list) and items else {}
                price = first_item.get("price", {}) if isinstance(first_item, dict) else {}
                price_id = price.get("id") if isinstance(price, dict) else None
                recurring = price.get("recurring", {}) if isinstance(price, dict) else {}
                interval = recurring.get("interval") if isinstance(recurring, dict) else None
                if interval not in {"month", "year"}:
                    interval = None
                if not isinstance(price_id, str):
                    price_id = None

                def period_timestamp(value):
                    if value is None:
                        return None
                    if not isinstance(value, int) or value < 0:
                        raise ValueError("Stripe subscription period is invalid.")
                    return _timestamp(datetime.fromtimestamp(value, tz=timezone.utc))

                try:
                    period_start = period_timestamp(obj.get("current_period_start"))
                    period_end = period_timestamp(obj.get("current_period_end"))
                    canceled_at = period_timestamp(obj.get("canceled_at"))
                    ended_at = period_timestamp(obj.get("ended_at"))
                except (ValueError, OverflowError, OSError):
                    db.rollback()
                    raise ValueError("Stripe subscription period is invalid.") from None
                cancel_at_period_end = obj.get("cancel_at_period_end", False)
                if type(cancel_at_period_end) is not bool:
                    db.rollback()
                    raise ValueError("Stripe cancellation flag is invalid.")
                if existing is None:
                    db.execute(
                        """INSERT INTO billing_subscriptions(
                            id, identity_id, stripe_subscription_id, stripe_customer_id,
                            stripe_price_id, billing_interval, status, current_period_start,
                            current_period_end, cancel_at_period_end, canceled_at, ended_at,
                            created_at, updated_at, last_event_created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            secrets.token_urlsafe(24), customer["identity_id"],
                            subscription_id, customer_id, price_id, interval, status,
                            period_start, period_end, int(cancel_at_period_end),
                            canceled_at, ended_at, now, now, occurred_at,
                        ),
                    )
                else:
                    db.execute(
                        """UPDATE billing_subscriptions SET
                            stripe_price_id=?, billing_interval=?, status=?,
                            current_period_start=?, current_period_end=?,
                            cancel_at_period_end=?, canceled_at=?, ended_at=?,
                            updated_at=?, last_event_created_at=?
                           WHERE stripe_subscription_id=?""",
                        (
                            price_id, interval, status, period_start, period_end,
                            int(cancel_at_period_end), canceled_at, ended_at,
                            now, occurred_at, subscription_id,
                        ),
                    )

            # Once Stripe has confirmed a subscription lifecycle event, the
            # short-lived Checkout reservation is no longer needed. Subscription
            # state itself now prevents duplicate purchases.
            db.execute(
                "DELETE FROM billing_checkout_attempts WHERE identity_id=?",
                (customer["identity_id"],),
            )
            completed = db.execute(
                """UPDATE billing_webhook_events
                   SET processing_state='processed', processed_at=?, updated_at=?,
                       lease_token_hash=NULL, lease_expires_at=NULL, last_error_code=NULL
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (now, now, event_id, _token_digest(lease_token), now),
            )
            if completed.rowcount != 1:
                db.rollback()
                return "lease_lost"
            db.commit()
            return result

    def process_stripe_invoice_event(self, event_id: str, lease_token: str) -> str:
        """Reconcile invoice payment state atomically; never changes entitlements."""
        if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
            return "invalid"
        if not isinstance(lease_token, str) or not lease_token:
            return "invalid"
        now = _timestamp(_now())
        key = config.STRIPE_WEBHOOK_ENCRYPTION_KEY
        if not isinstance(key, str) or not key:
            raise RuntimeError("Stripe webhook encryption is not configured.")
        try:
            cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise RuntimeError("Stripe webhook encryption configuration is invalid.") from None

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inbox = db.execute(
                """SELECT event_type, encrypted_payload FROM billing_webhook_events
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (event_id, _token_digest(lease_token), now),
            ).fetchone()
            if inbox is None:
                db.rollback()
                return "lease_lost"
            if not inbox["encrypted_payload"]:
                db.rollback()
                raise RuntimeError("Stripe webhook event payload is unavailable.")
            try:
                event = json.loads(cipher.decrypt(inbox["encrypted_payload"].encode("ascii")))
                event_created = event["created"]
                invoice = event["data"]["object"]
                invoice_id = invoice["id"]
                customer_id = invoice["customer"]
            except (InvalidToken, ValueError, UnicodeEncodeError, TypeError, KeyError):
                db.rollback()
                raise ValueError("Stripe invoice event payload is invalid.") from None

            event_type = inbox["event_type"]
            status_by_event = {
                "invoice.finalized": "open",
                "invoice.paid": "paid",
                "invoice.payment_failed": "open",
                "invoice.voided": "void",
                "invoice.marked_uncollectible": "uncollectible",
            }
            if event_type not in status_by_event:
                completed = db.execute(
                    """UPDATE billing_webhook_events
                       SET processing_state='processed', processed_at=?, updated_at=?,
                           lease_token_hash=NULL, lease_expires_at=NULL, last_error_code=NULL
                       WHERE stripe_event_id=? AND processing_state='processing'
                         AND lease_token_hash=? AND lease_expires_at>?""",
                    (now, now, event_id, _token_digest(lease_token), now),
                )
                if completed.rowcount != 1:
                    db.rollback()
                    return "lease_lost"
                db.commit()
                return "ignored"
            if (
                isinstance(event_created, bool) or not isinstance(event_created, int)
                or event_created < 0
                or not isinstance(invoice_id, str) or not invoice_id or len(invoice_id) > 255
                or not isinstance(customer_id, str) or not customer_id or len(customer_id) > 255
            ):
                db.rollback()
                raise ValueError("Stripe invoice event identifiers are invalid.")

            customer = db.execute(
                "SELECT identity_id FROM billing_customers WHERE stripe_customer_id=?",
                (customer_id,),
            ).fetchone()
            if customer is None:
                db.rollback()
                raise LookupError("Stripe customer is not linked to a local subscriber.")

            subscription_id = invoice.get("subscription")
            if not isinstance(subscription_id, str):
                parent = invoice.get("parent")
                details = parent.get("subscription_details") if isinstance(parent, dict) else None
                candidate = details.get("subscription") if isinstance(details, dict) else None
                subscription_id = candidate if isinstance(candidate, str) else None
            subscription = None
            if subscription_id:
                subscription = db.execute(
                    """SELECT identity_id, stripe_customer_id FROM billing_subscriptions
                       WHERE stripe_subscription_id=?""", (subscription_id,)
                ).fetchone()
                if (
                    subscription is None
                    or subscription["identity_id"] != customer["identity_id"]
                    or subscription["stripe_customer_id"] != customer_id
                ):
                    db.rollback()
                    raise ValueError("Stripe invoice subscription mapping conflicts with local data.")

            occurred_at = _timestamp(datetime.fromtimestamp(event_created, tz=timezone.utc))
            existing = db.execute(
                """SELECT identity_id, stripe_customer_id, stripe_subscription_id,
                          last_event_created_at, payment_failed_at
                   FROM billing_invoices WHERE stripe_invoice_id=?""", (invoice_id,)
            ).fetchone()
            if existing is not None and (
                existing["identity_id"] != customer["identity_id"]
                or existing["stripe_customer_id"] != customer_id
                or existing["stripe_subscription_id"] != subscription_id
            ):
                db.rollback()
                raise ValueError("Stripe invoice customer mapping conflicts with local data.")

            if existing is not None and existing["last_event_created_at"] >= occurred_at:
                result = "stale"
            else:
                status = status_by_event[event_type]
                amount_due = invoice.get("amount_due")
                amount_paid = invoice.get("amount_paid")
                currency = invoice.get("currency")
                if any(value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ) for value in (amount_due, amount_paid)):
                    db.rollback()
                    raise ValueError("Stripe invoice amount is invalid.")
                if currency is not None and (
                    not isinstance(currency, str) or len(currency) != 3
                    or not currency.isalpha() or currency.lower() != currency
                ):
                    db.rollback()
                    raise ValueError("Stripe invoice currency is invalid.")

                def invoice_period(value):
                    if value is None:
                        return None
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError("Stripe invoice period is invalid.")
                    return _timestamp(datetime.fromtimestamp(value, tz=timezone.utc))

                lines = invoice.get("lines", {})
                line_data = lines.get("data", []) if isinstance(lines, dict) else []
                first_line = line_data[0] if isinstance(line_data, list) and line_data else {}
                period = first_line.get("period", {}) if isinstance(first_line, dict) else {}
                try:
                    period_start = invoice_period(period.get("start") if isinstance(period, dict) else None)
                    period_end = invoice_period(period.get("end") if isinstance(period, dict) else None)
                except (ValueError, OverflowError, OSError):
                    db.rollback()
                    raise ValueError("Stripe invoice period is invalid.") from None

                if existing is None:
                    db.execute(
                        """INSERT INTO billing_invoices(
                            id, identity_id, stripe_invoice_id, stripe_customer_id,
                            stripe_subscription_id, status, amount_due, amount_paid,
                            currency, period_start, period_end, last_event_created_at,
                            created_at, updated_at, payment_failed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (secrets.token_urlsafe(24), customer["identity_id"], invoice_id,
                         customer_id, subscription_id, status, amount_due, amount_paid,
                         currency, period_start, period_end, occurred_at, now, now,
                         occurred_at if event_type == "invoice.payment_failed" else None),
                    )
                    result = "created"
                else:
                    db.execute(
                        """UPDATE billing_invoices SET status=?, amount_due=?, amount_paid=?,
                           currency=?, period_start=?, period_end=?, last_event_created_at=?,
                           updated_at=?, payment_failed_at=? WHERE stripe_invoice_id=?""",
                        (status, amount_due, amount_paid, currency, period_start, period_end,
                         occurred_at, now,
                         None if event_type == "invoice.paid" else (
                             occurred_at if event_type == "invoice.payment_failed" else existing["payment_failed_at"]
                         ), invoice_id),
                    )
                    result = "updated"

            completed = db.execute(
                """UPDATE billing_webhook_events
                   SET processing_state='processed', processed_at=?, updated_at=?,
                       lease_token_hash=NULL, lease_expires_at=NULL, last_error_code=NULL
                   WHERE stripe_event_id=? AND processing_state='processing'
                     AND lease_token_hash=? AND lease_expires_at>?""",
                (now, now, event_id, _token_digest(lease_token), now),
            )
            if completed.rowcount != 1:
                db.rollback()
                return "lease_lost"
            db.commit()
            return result

    def create_subscriber(self, email: str, password: str) -> str:
        """Create an unverified subscriber. Public routes must call this only."""
        return self._create_identity(email, password, "subscriber")

    def create_complimentary_subscriber(
        self,
        email: str,
        entitlement_keys: list[str] | tuple[str, ...],
        *,
        actor_identity_id: str,
        expires_at: datetime,
        campaign: str,
        reason_code: str,
    ) -> dict:
        """Atomically create a password-pending promo account, expiring grants and activation challenge."""
        normalized_email = _normalize_email(email)
        if not isinstance(actor_identity_id, str) or not actor_identity_id or len(actor_identity_id) > 128:
            raise ValueError("An active MFA administrator is required.")
        if not isinstance(entitlement_keys, (list, tuple)) or not entitlement_keys or len(entitlement_keys) > 32:
            raise ValueError("Select at least one feature entitlement.")
        if any(not isinstance(key, str) or key not in _ENTITLEMENT_KEYS for key in entitlement_keys):
            raise ValueError("One or more entitlement keys are not supported.")
        normalized = sorted(set(entitlement_keys))
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("Complimentary access expiry must include a timezone.")
        now_dt = _now()
        if expires_at.astimezone(timezone.utc) <= now_dt or expires_at.astimezone(timezone.utc) > now_dt + timedelta(days=366):
            raise ValueError("Complimentary access must expire within 366 days.")
        if not isinstance(campaign, str) or not campaign.strip() or len(campaign.strip()) > 80:
            raise ValueError("A campaign or promotion name is required.")
        if not isinstance(reason_code, str) or reason_code not in _COMPLIMENTARY_REASON_CODES:
            raise ValueError("Choose a supported complimentary-access reason.")

        now = _timestamp(now_dt)
        expiry = _timestamp(expires_at.astimezone(timezone.utc))
        identity_id = secrets.token_urlsafe(24)
        # The random credential is never returned or sent; the recipient sets their own.
        password_hash = _password_hash(secrets.token_urlsafe(48))
        activation_token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor = self._resolve_audit_actor(db, identity_id, actor_identity_id)
            try:
                db.execute(
                    """INSERT INTO identities(
                        id, email, account_type, password_hash, created_at, updated_at,
                        password_changed_at
                    ) VALUES (?, ?, 'subscriber', ?, ?, ?, ?)""",
                    (identity_id, normalized_email, password_hash, now, now, now),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                raise ValueError("A subscriber account already exists for this email address.") from None
            for key in normalized:
                db.execute(
                    """INSERT INTO subscriber_entitlements(
                        id, identity_id, entitlement_key, starts_at, expires_at,
                        granted_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (secrets.token_urlsafe(18), identity_id, key, now, expiry, actor, now, now),
                )
            db.execute(
                """INSERT INTO password_reset_challenges(
                    id, identity_id, token_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (secrets.token_urlsafe(18), identity_id, _token_digest(activation_token),
                 now, _timestamp(now_dt + timedelta(minutes=30))),
            )
            self._record_audit_event(
                db, "subscriber.complimentary_access_granted", identity_id, now,
                metadata={
                    "entitlements": normalized,
                    "expires_at": expiry,
                    "grant_type": "complimentary",
                    "campaign": campaign.strip(),
                    "reason_code": reason_code,
                    "activation_expires_at": _timestamp(now_dt + timedelta(minutes=30)),
                },
                actor_identity_id=actor,
            )
            db.commit()
        return {
            "identity_id": identity_id,
            "email": normalized_email,
            "entitlements": normalized,
            "expires_at": expiry,
            "activation_token": activation_token,
        }

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
                   email_verified_at=COALESCE(email_verified_at, ?), updated_at=? WHERE id=?""",
                (password_hash, now, now, now, challenge["identity_id"]),
            )
            db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (now, challenge["identity_id"]),
            )
            # A one-time reset/activation link proves control of the mailbox.
            # This also lets administrator-created promotional accounts activate
            # without bypassing the subscriber email-verification requirement.
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                )
                SELECT ?, 'subscriber.email_verified', ?, ?, '{}'
                WHERE EXISTS (
                    SELECT 1 FROM identities
                    WHERE id=? AND account_type='subscriber' AND email_verified_at=?
                )""",
                (secrets.token_urlsafe(18), challenge["identity_id"], now, challenge["identity_id"], now),
            )
            db.execute(
                """INSERT INTO security_audit_events(
                    id, event_type, subject_identity_id, occurred_at, metadata_json
                ) VALUES (?, 'identity.password_reset', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), challenge["identity_id"], now),
            )
            db.commit()
        return True

    def change_subscriber_password(
        self,
        identity_id: str,
        current_password: str,
        new_password: str,
    ) -> bool:
        """Change an active subscriber password and revoke every session atomically.

        The old hash is verified before hashing the replacement, then rechecked
        inside the write transaction so a concurrent reset/change cannot be
        overwritten by a stale password-change request.
        """
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            return False
        if not isinstance(current_password, str) or not current_password or len(current_password) > 1024:
            return False
        if not isinstance(new_password, str) or len(new_password) > 1024:
            raise ValueError("Password is too long.")

        with self.connect() as db:
            row = db.execute(
                """SELECT password_hash FROM identities
                   WHERE id=? AND account_type='subscriber' AND is_active=1
                     AND email_verified_at IS NOT NULL""",
                (identity_id,),
            ).fetchone()
        if row is None:
            return False
        original_hash = row["password_hash"]
        try:
            valid = _PASSWORD_HASHER.verify(original_hash, current_password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
            return False
        if not valid:
            return False
        if current_password == new_password:
            raise ValueError("New password must be different from the current password.")

        replacement_hash = _password_hash(new_password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT password_hash FROM identities
                   WHERE id=? AND account_type='subscriber' AND is_active=1
                     AND email_verified_at IS NOT NULL""",
                (identity_id,),
            ).fetchone()
            if current is None or current["password_hash"] != original_hash:
                db.rollback()
                return False
            now = _timestamp(_now())
            db.execute(
                """UPDATE identities SET password_hash=?, password_changed_at=?,
                   updated_at=? WHERE id=?""",
                (replacement_hash, now, now, identity_id),
            )
            db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (now, identity_id),
            )
            # A previously issued reset link must not remain usable after the
            # subscriber has authenticated and changed their password.
            db.execute(
                """UPDATE password_reset_challenges SET consumed_at=?
                   WHERE identity_id=? AND consumed_at IS NULL""",
                (now, identity_id),
            )
            self._record_audit_event(
                db,
                "subscriber.password_changed",
                identity_id,
                now,
                actor_identity_id=identity_id,
            )
            db.commit()
        return True


    def email_provider_circuit_open(self, provider: str = "resend") -> bool:
        """Return whether the provider circuit is currently open."""
        if provider != "resend":
            raise ValueError("Unsupported email provider.")
        now = _timestamp(_now())
        with self.connect() as db:
            row = db.execute(
                "SELECT opened_until FROM email_provider_circuit WHERE provider=?",
                (provider,),
            ).fetchone()
        return bool(row and row["opened_until"] and row["opened_until"] > now)

    def claim_email_provider_send(self, provider: str = "resend") -> bool:
        """Atomically allow one half-open probe after the provider cooldown.

        While the circuit is open no request is sent. Once its cooldown expires,
        only one worker receives a 60-second probe lease; concurrent workers stay
        blocked until that probe succeeds, fails, or its lease expires.
        """
        if provider != "resend":
            raise ValueError("Unsupported email provider.")
        now_dt = _now()
        now = _timestamp(now_dt)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT consecutive_failures, opened_until, probe_until
                   FROM email_provider_circuit WHERE provider=?""",
                (provider,),
            ).fetchone()
            if row is None:
                db.commit()
                return True
            if row["opened_until"] and row["opened_until"] > now:
                db.commit()
                return False
            if int(row["consecutive_failures"]) >= 5:
                if row["probe_until"] and row["probe_until"] > now:
                    db.commit()
                    return False
                probe_until = _timestamp(now_dt + timedelta(minutes=1))
                db.execute(
                    "UPDATE email_provider_circuit SET probe_until=?, updated_at=? WHERE provider=?",
                    (probe_until, now, provider),
                )
            db.commit()
            return True

    def record_email_delivery_event(
        self,
        identity_id: str,
        purpose: str,
        status: str,
        *,
        provider: str = "resend",
        provider_message_id: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        """Record secret-free delivery status and update the persistent provider circuit.

        Raw email addresses, challenge tokens, request bodies, and provider error
        messages are never stored. Five consecutive provider failures within ten
        minutes open the circuit for ten minutes; a successful send resets it.
        """
        if provider != "resend":
            raise ValueError("Unsupported email provider.")
        if purpose not in {"verification", "password_reset"}:
            raise ValueError("Unsupported transactional email purpose.")
        if status not in {"sent", "failed", "skipped"}:
            raise ValueError("Unsupported email delivery status.")
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid identity reference is required.")
        allowed_failure_codes = {
            "configuration", "provider_auth", "provider_rate_limit",
            "provider_unavailable", "provider_rejected", "transport",
            "invalid_response", "circuit_open", "unexpected",
        }
        if failure_code is not None and failure_code not in allowed_failure_codes:
            raise ValueError("Email failure code is not permitted.")
        if status == "sent":
            if (
                not isinstance(provider_message_id, str)
                or not provider_message_id
                or len(provider_message_id) > 256
                or failure_code is not None
            ):
                raise ValueError("A sent email requires a provider message ID only.")
        elif provider_message_id is not None or failure_code is None:
            raise ValueError("A failed or skipped email requires a safe failure code only.")
        if status == "skipped" and failure_code != "circuit_open":
            raise ValueError("Skipped email status requires an open-circuit reason.")

        now_dt = _now()
        now = _timestamp(now_dt)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO email_delivery_events(
                    id, provider, identity_id, purpose, status,
                    provider_message_id, failure_code, attempted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    secrets.token_urlsafe(18), provider, identity_id, purpose,
                    status, provider_message_id, failure_code, now,
                ),
            )
            if status == "sent":
                db.execute(
                    """INSERT INTO email_provider_circuit(
                        provider, consecutive_failures, opened_until,
                        last_failure_at, last_success_at, last_error_code, updated_at
                    ) VALUES (?, 0, NULL, NULL, ?, NULL, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        consecutive_failures=0, opened_until=NULL, probe_until=NULL,
                        last_success_at=excluded.last_success_at,
                        last_error_code=NULL, updated_at=excluded.updated_at""",
                    (provider, now, now),
                )
            elif status == "failed":
                prior = db.execute(
                    """SELECT consecutive_failures, last_failure_at
                       FROM email_provider_circuit WHERE provider=?""",
                    (provider,),
                ).fetchone()
                within_window = False
                if prior and prior["last_failure_at"]:
                    try:
                        previous_time = datetime.fromisoformat(prior["last_failure_at"])
                        within_window = now_dt - previous_time <= timedelta(minutes=10)
                    except ValueError:
                        within_window = False
                failures = (
                    int(prior["consecutive_failures"]) + 1
                    if prior and within_window else 1
                )
                opened_until = (
                    _timestamp(now_dt + timedelta(minutes=10))
                    if failures >= 5 else None
                )
                db.execute(
                    """INSERT INTO email_provider_circuit(
                        provider, consecutive_failures, opened_until,
                        last_failure_at, last_success_at, last_error_code, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        consecutive_failures=excluded.consecutive_failures,
                        opened_until=excluded.opened_until, probe_until=NULL,
                        last_failure_at=excluded.last_failure_at,
                        last_error_code=excluded.last_error_code,
                        updated_at=excluded.updated_at""",
                    (provider, failures, opened_until, now, failure_code, now),
                )
            # Delivery history is operational metadata, not a permanent audit log.
            db.execute(
                "DELETE FROM email_delivery_events WHERE attempted_at < ?",
                (_timestamp(now_dt - timedelta(days=90)),),
            )
            db.commit()

    def set_subscriber_entitlements(
        self,
        identity_id: str,
        entitlement_keys: list[str] | tuple[str, ...],
        *,
        actor_identity_id: str,
        reason_code: str,
        expires_at: datetime | None = None,
    ) -> list[str]:
        """Replace one subscriber's feature grants with a controlled audit reason."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        if (
            not isinstance(actor_identity_id, str)
            or not actor_identity_id
            or len(actor_identity_id) > 128
            or actor_identity_id == identity_id
        ):
            raise ValueError("A separate administrator actor is required.")
        if not isinstance(reason_code, str) or reason_code not in _ENTITLEMENT_REASON_CODES:
            raise ValueError("A supported entitlement change reason code is required.")
        if not isinstance(entitlement_keys, (list, tuple)) or len(entitlement_keys) > 32:
            raise ValueError("Entitlement list is invalid.")
        if any(
            not isinstance(key, str) or key not in _ENTITLEMENT_KEYS
            for key in entitlement_keys
        ):
            raise ValueError("One or more entitlement keys are not supported.")
        normalized = sorted(set(entitlement_keys))
        now_dt = _now()
        now = _timestamp(now_dt)
        expiry = None
        if expires_at is not None:
            if (
                not isinstance(expires_at, datetime)
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
            ):
                raise ValueError("Entitlement expiry must include a timezone.")
            if expires_at.astimezone(timezone.utc) <= now_dt:
                raise ValueError("Entitlement expiry must be in the future.")
            expiry = _timestamp(expires_at.astimezone(timezone.utc))

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subject = db.execute(
                """SELECT id FROM identities
                   WHERE id=? AND account_type='subscriber' AND is_active=1""",
                (identity_id,),
            ).fetchone()
            if subject is None:
                db.rollback()
                raise ValueError("An active subscriber identity is required.")
            actor = self._resolve_audit_actor(db, identity_id, actor_identity_id)
            db.execute(
                "DELETE FROM subscriber_entitlements WHERE identity_id=?",
                (identity_id,),
            )
            for key in normalized:
                db.execute(
                    """INSERT INTO subscriber_entitlements(
                        id, identity_id, entitlement_key, starts_at, expires_at,
                        granted_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        secrets.token_urlsafe(18), identity_id, key, now, expiry,
                        actor, now, now,
                    ),
                )
            self._record_audit_event(
                db,
                "subscriber.entitlements_updated",
                identity_id,
                now,
                metadata={
                    "entitlements": normalized,
                    "expires_at": expiry,
                    "reason_code": reason_code,
                },
                actor_identity_id=actor,
            )
            db.commit()
        return normalized

    def billing_customer_id(self, identity_id: str) -> str | None:
        """Return the Stripe customer mapped to this active subscriber, if any."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        with self.connect() as db:
            row = db.execute(
                """SELECT c.stripe_customer_id
                   FROM billing_customers c
                   JOIN identities i ON i.id=c.identity_id
                   WHERE c.identity_id=? AND i.account_type='subscriber' AND i.is_active=1""",
                (identity_id,),
            ).fetchone()
        return row["stripe_customer_id"] if row else None

    def link_billing_customer(self, identity_id: str, stripe_customer_id: str) -> str:
        """Atomically link a newly created Stripe customer without changing an existing mapping."""
        if (
            not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128
            or not isinstance(stripe_customer_id, str)
            or not re.fullmatch(r"cus_[A-Za-z0-9]+", stripe_customer_id)
        ):
            raise ValueError("Stripe customer mapping is invalid.")
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute(
                "SELECT id FROM identities WHERE id=? AND account_type='subscriber' AND is_active=1",
                (identity_id,),
            ).fetchone()
            if account is None:
                db.rollback()
                raise ValueError("An active subscriber account is required.")
            existing = db.execute(
                "SELECT stripe_customer_id FROM billing_customers WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
            if existing is not None:
                db.rollback()
                if existing["stripe_customer_id"] != stripe_customer_id:
                    raise ValueError("A different Stripe customer is already linked.")
                return existing["stripe_customer_id"]
            try:
                db.execute(
                    """INSERT INTO billing_customers(
                        identity_id, stripe_customer_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)""",
                    (identity_id, stripe_customer_id, now, now),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                raise ValueError("Stripe customer is already linked to another account.") from None
            db.commit()
        return stripe_customer_id

    def reserve_billing_checkout(
        self, identity_id: str, billing_interval: str, idempotency_key_hash: str,
        *, lifetime_seconds: int = 2100,
    ) -> dict:
        """Atomically reserve one short-lived Checkout attempt per subscriber."""
        if (
            not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128
            or not isinstance(billing_interval, str)
            or billing_interval not in {"month", "year"}
            or not isinstance(idempotency_key_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", idempotency_key_hash)
            or isinstance(lifetime_seconds, bool)
            or not isinstance(lifetime_seconds, int)
            or lifetime_seconds != 2100
        ):
            raise ValueError("Checkout reservation parameters are invalid.")
        now_dt = _now()
        now = _timestamp(now_dt)
        expires_at = _timestamp(now_dt + timedelta(seconds=lifetime_seconds))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute(
                "SELECT id FROM identities WHERE id=? AND account_type='subscriber' AND is_active=1",
                (identity_id,),
            ).fetchone()
            if account is None:
                db.rollback()
                raise ValueError("An active subscriber account is required.")
            subscription = db.execute(
                """SELECT 1 FROM billing_subscriptions
                   WHERE identity_id=? AND status NOT IN ('canceled', 'incomplete_expired')
                   LIMIT 1""",
                (identity_id,),
            ).fetchone()
            if subscription is not None:
                db.rollback()
                return {"state": "existing_subscription"}
            existing = db.execute(
                "SELECT * FROM billing_checkout_attempts WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
            if existing is not None and existing["expires_at"] > now:
                db.rollback()
                if (
                    existing["idempotency_key_hash"] == idempotency_key_hash
                    and existing["billing_interval"] == billing_interval
                ):
                    return {
                        "state": "reserved",
                        "attempt_token": existing["attempt_token"],
                        "expires_at": existing["expires_at"],
                        "checkout_session_id": existing["checkout_session_id"],
                    }
                return {"state": "checkout_pending", "expires_at": existing["expires_at"]}
            attempt_token = secrets.token_urlsafe(32)
            db.execute(
                """INSERT INTO billing_checkout_attempts(
                    identity_id, billing_interval, idempotency_key_hash,
                    attempt_token, checkout_session_id, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    billing_interval=excluded.billing_interval,
                    idempotency_key_hash=excluded.idempotency_key_hash,
                    attempt_token=excluded.attempt_token,
                    checkout_session_id=NULL,
                    expires_at=excluded.expires_at,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at""",
                (identity_id, billing_interval, idempotency_key_hash,
                 attempt_token, expires_at, now, now),
            )
            db.commit()
        return {
            "state": "reserved",
            "attempt_token": attempt_token,
            "expires_at": expires_at,
            "checkout_session_id": None,
        }

    def mark_billing_checkout_session(
        self, identity_id: str, attempt_token: str, checkout_session_id: str
    ) -> bool:
        """Attach the provider session only to the still-current reservation."""
        if (
            not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128
            or not isinstance(attempt_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", attempt_token)
            or not isinstance(checkout_session_id, str)
            or not re.fullmatch(r"cs_test_[A-Za-z0-9]+", checkout_session_id)
        ):
            raise ValueError("Checkout session reference is invalid.")
        now = _timestamp(_now())
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE billing_checkout_attempts
                   SET checkout_session_id=?, updated_at=?
                   WHERE identity_id=? AND attempt_token=? AND expires_at>?""",
                (checkout_session_id, now, identity_id, attempt_token, now),
            )
        return cursor.rowcount == 1

    def billing_checkout_pending(self, identity_id: str) -> bool:
        """Return whether this subscriber has a live Checkout reservation."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        now = _timestamp(_now())
        with self.connect() as db:
            row = db.execute(
                """SELECT 1 FROM billing_checkout_attempts
                   WHERE identity_id=? AND expires_at>? LIMIT 1""",
                (identity_id, now),
            ).fetchone()
        return row is not None

    def subscriber_has_open_subscription(self, identity_id: str) -> bool:
        """Prevent starting a second checkout while a nonterminal subscription exists."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        with self.connect() as db:
            account = db.execute(
                "SELECT id FROM identities WHERE id=? AND account_type='subscriber' AND is_active=1",
                (identity_id,),
            ).fetchone()
            if account is None:
                raise ValueError("An active subscriber account is required.")
            row = db.execute(
                """SELECT 1 FROM billing_subscriptions
                   WHERE identity_id=? AND status NOT IN ('canceled', 'incomplete_expired')
                   LIMIT 1""",
                (identity_id,),
            ).fetchone()
        return row is not None

    def subscriber_billing_summary(self, identity_id: str) -> dict:
        """Return a bounded, read-only billing summary for the signed-in subscriber."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        with self.connect() as db:
            account = db.execute(
                "SELECT id FROM identities WHERE id=? AND account_type='subscriber' AND is_active=1",
                (identity_id,),
            ).fetchone()
            if account is None:
                raise ValueError("An active subscriber account is required.")
            subscriptions = db.execute(
                """SELECT stripe_subscription_id, plan_code, billing_interval, status,
                          current_period_start, current_period_end, cancel_at_period_end,
                          canceled_at, ended_at, updated_at
                   FROM billing_subscriptions WHERE identity_id=?
                   ORDER BY updated_at DESC, stripe_subscription_id LIMIT 20""",
                (identity_id,),
            ).fetchall()
            invoices = db.execute(
                """SELECT stripe_invoice_id, stripe_subscription_id, status,
                          amount_due, amount_paid, currency, period_start, period_end,
                          payment_failed_at, updated_at
                   FROM billing_invoices WHERE identity_id=?
                   ORDER BY updated_at DESC, stripe_invoice_id LIMIT 20""",
                (identity_id,),
            ).fetchall()
        return {
            "subscriptions": [
                {
                    "subscription_id": row["stripe_subscription_id"],
                    "plan_code": row["plan_code"],
                    "billing_interval": row["billing_interval"],
                    "status": row["status"],
                    "current_period_start": row["current_period_start"],
                    "current_period_end": row["current_period_end"],
                    "cancel_at_period_end": bool(row["cancel_at_period_end"]),
                    "canceled_at": row["canceled_at"],
                    "ended_at": row["ended_at"],
                    "updated_at": row["updated_at"],
                }
                for row in subscriptions
            ],
            "invoices": [
                {
                    "invoice_id": row["stripe_invoice_id"],
                    "subscription_id": row["stripe_subscription_id"],
                    "status": row["status"],
                    "amount_due": row["amount_due"],
                    "amount_paid": row["amount_paid"],
                    "currency": row["currency"],
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "payment_failed_at": row["payment_failed_at"],
                    "updated_at": row["updated_at"],
                }
                for row in invoices
            ],
            "checkout_available": False,
            "customer_portal_available": False,
        }

    def administrator_billing_overview(self) -> dict:
        """Return aggregate billing operations data without subscriber or provider secrets."""
        with self.connect() as db:
            subscription_rows = db.execute(
                """SELECT status, billing_interval, COUNT(*) AS total
                   FROM billing_subscriptions GROUP BY status, billing_interval
                   ORDER BY status, billing_interval"""
            ).fetchall()
            invoice_rows = db.execute(
                """SELECT status, COUNT(*) AS total,
                          SUM(CASE WHEN payment_failed_at IS NOT NULL THEN 1 ELSE 0 END) AS failed
                   FROM billing_invoices GROUP BY status ORDER BY status"""
            ).fetchall()
            event_rows = db.execute(
                """SELECT processing_state, COUNT(*) AS total,
                          MAX(updated_at) AS latest_updated
                   FROM billing_webhook_events GROUP BY processing_state
                   ORDER BY processing_state"""
            ).fetchall()
            customer_count = db.execute(
                "SELECT COUNT(*) FROM billing_customers"
            ).fetchone()[0]
            failed_attempts = db.execute(
                """SELECT COUNT(*) FROM billing_webhook_events
                   WHERE processing_state='failed'"""
            ).fetchone()[0]
        subscriptions = [
            {"status": row["status"], "billing_interval": row["billing_interval"], "count": row["total"]}
            for row in subscription_rows
        ]
        invoices = [
            {"status": row["status"], "count": row["total"], "payment_failures": row["failed"]}
            for row in invoice_rows
        ]
        events = [
            {"processing_state": row["processing_state"], "count": row["total"],
             "latest_updated": row["latest_updated"]}
            for row in event_rows
        ]
        try:
            public_origin = urlsplit(config.PUBLIC_BASE_URL)
            public_origin.port
            public_origin_ready = bool(
                public_origin.scheme == "https" and public_origin.hostname
                and public_origin.username is None and public_origin.password is None
                and public_origin.path in {"", "/"}
                and not public_origin.query and not public_origin.fragment
            )
        except (TypeError, ValueError):
            public_origin_ready = False
        try:
            webhook_ready = bool(
                isinstance(config.STRIPE_WEBHOOK_SECRET, str)
                and config.STRIPE_WEBHOOK_SECRET.startswith("whsec_")
                and len(config.STRIPE_WEBHOOK_SECRET) >= 16
                and config.STRIPE_WEBHOOK_SECRET.strip() == config.STRIPE_WEBHOOK_SECRET
                and isinstance(config.STRIPE_WEBHOOK_ENCRYPTION_KEY, str)
                and Fernet(config.STRIPE_WEBHOOK_ENCRYPTION_KEY.encode("ascii", errors="strict"))
            )
        except (ValueError, TypeError, UnicodeEncodeError):
            webhook_ready = False
        test_mode_ready = bool(
            re.fullmatch(r"sk_test_[A-Za-z0-9]+", config.STRIPE_SECRET_KEY)
            and not config.STRIPE_WEBHOOK_LIVEMODE
            and config.STRIPE_TEST_MODE_ACKNOWLEDGED
            and webhook_ready and public_origin_ready
        )
        return {
            "customer_count": customer_count,
            "subscription_count": sum(row["count"] for row in subscriptions),
            "invoice_count": sum(row["count"] for row in invoices),
            "failed_webhook_count": failed_attempts,
            "subscriptions_by_status_and_interval": subscriptions,
            "invoices_by_status": invoices,
            "webhook_events_by_state": events,
            "checkout_available": bool(
                config.STRIPE_CHECKOUT_ENABLED
                and test_mode_ready
                and re.fullmatch(r"price_[A-Za-z0-9]+", config.STRIPE_PRICE_MONTHLY_ID)
                and re.fullmatch(r"price_[A-Za-z0-9]+", config.STRIPE_PRICE_YEARLY_ID)
                and config.STRIPE_PRICE_MONTHLY_ID != config.STRIPE_PRICE_YEARLY_ID
            ),
            "customer_portal_available": bool(
                config.STRIPE_CUSTOMER_PORTAL_ENABLED
                and test_mode_ready
            ),
            "live_billing_enabled": False,
        }

    def active_subscriber_entitlements(self, identity_id: str) -> list[str]:
        """Return only currently active feature grants for an active subscriber."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        now = _timestamp(_now())
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.entitlement_key
                   FROM subscriber_entitlements e
                   JOIN identities i ON i.id=e.identity_id
                   WHERE e.identity_id=? AND i.account_type='subscriber'
                     AND i.is_active=1 AND e.starts_at<=?
                     AND (e.expires_at IS NULL OR e.expires_at>?)
                   ORDER BY e.entitlement_key""",
                (identity_id, now, now),
            ).fetchall()
        return [row["entitlement_key"] for row in rows]

    def has_subscriber_entitlement(self, identity_id: str, entitlement_key: str) -> bool:
        """Fail closed unless an active subscriber has the requested feature grant."""
        if not isinstance(entitlement_key, str) or entitlement_key not in _ENTITLEMENT_KEYS:
            return False
        now = _timestamp(_now())
        with self.connect() as db:
            row = db.execute(
                """SELECT 1
                   FROM subscriber_entitlements e
                   JOIN identities i ON i.id=e.identity_id
                   WHERE e.identity_id=? AND e.entitlement_key=?
                     AND i.account_type='subscriber' AND i.is_active=1
                     AND e.starts_at<=?
                     AND (e.expires_at IS NULL OR e.expires_at>?)
                   LIMIT 1""",
                (identity_id, entitlement_key, now, now),
            ).fetchone()
        return row is not None

    def set_subscriber_status(
        self,
        identity_id: str,
        *,
        is_active: bool,
        actor_identity_id: str,
        reason_code: str,
    ) -> dict:
        """Suspend or reactivate a subscriber with atomic session revocation and audit."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        if (
            not isinstance(actor_identity_id, str)
            or not actor_identity_id
            or len(actor_identity_id) > 128
            or actor_identity_id == identity_id
        ):
            raise ValueError("A separate administrator actor is required.")
        if not isinstance(is_active, bool):
            raise ValueError("Subscriber status must be active or suspended.")
        if not isinstance(reason_code, str) or reason_code not in _SUBSCRIBER_STATUS_REASON_CODES:
            raise ValueError("A supported administrative reason code is required.")
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subject = db.execute(
                """SELECT is_active FROM identities
                   WHERE id=? AND account_type='subscriber'""",
                (identity_id,),
            ).fetchone()
            if subject is None:
                db.rollback()
                raise ValueError("A subscriber identity was not found.")
            actor = self._resolve_audit_actor(db, identity_id, actor_identity_id)
            current_status = bool(subject["is_active"])
            if current_status == is_active:
                db.rollback()
                return {
                    "identity_id": identity_id,
                    "is_active": current_status,
                    "revoked_sessions": 0,
                    "changed": False,
                }

            db.execute(
                "UPDATE identities SET is_active=? WHERE id=? AND account_type='subscriber'",
                (1 if is_active else 0, identity_id),
            )
            revoked_count = 0
            if not is_active:
                cursor = db.execute(
                    """UPDATE sessions SET revoked_at=?
                       WHERE identity_id=? AND revoked_at IS NULL""",
                    (now, identity_id),
                )
                revoked_count = cursor.rowcount

            self._record_audit_event(
                db,
                "subscriber.reactivated" if is_active else "subscriber.suspended",
                identity_id,
                now,
                {
                    "status": "active" if is_active else "suspended",
                    "reason_code": reason_code,
                    "revoked_sessions": revoked_count,
                },
                actor_identity_id=actor,
            )
            db.commit()
        return {
            "identity_id": identity_id,
            "is_active": is_active,
            "revoked_sessions": revoked_count,
            "changed": True,
        }

    def list_subscribers(
        self, *, limit: int = 25, offset: int = 0, email_query: str | None = None
    ) -> dict:
        """Return a bounded subscriber directory without authentication material."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Subscriber page size must be between 1 and 100.")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10_000_000
        ):
            raise ValueError("Subscriber offset must be between 0 and 10000000.")
        where = " WHERE i.account_type='subscriber'"
        values: list[str | int] = []
        if email_query is not None:
            if not isinstance(email_query, str) or not email_query.strip() or len(email_query) > 254:
                raise ValueError("Subscriber email search must be a non-empty bounded string.")
            escaped = email_query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND i.email LIKE ? ESCAPE '\\'"
            values.append("%" + escaped + "%")
        now = _timestamp(_now())
        with self.connect() as db:
            db.execute("BEGIN")
            try:
                total = db.execute(
                    "SELECT COUNT(*) AS count FROM identities i" + where, values
                ).fetchone()["count"]
                rows = db.execute(
                    """SELECT i.id, i.email, i.is_active, i.email_verified_at,
                              i.created_at,
                              COALESCE(GROUP_CONCAT(e.entitlement_key, ','), '') AS entitlements,
                              (SELECT json_extract(a.metadata_json, '$.expires_at')
                                 FROM security_audit_events a
                                WHERE a.subject_identity_id=i.id
                                  AND a.event_type='subscriber.complimentary_access_granted'
                                ORDER BY a.occurred_at DESC LIMIT 1) AS complimentary_expires_at,
                              (SELECT json_extract(a.metadata_json, '$.campaign')
                                 FROM security_audit_events a
                                WHERE a.subject_identity_id=i.id
                                  AND a.event_type='subscriber.complimentary_access_granted'
                                ORDER BY a.occurred_at DESC LIMIT 1) AS complimentary_campaign
                       FROM identities i
                       LEFT JOIN subscriber_entitlements e
                         ON e.identity_id=i.id AND e.starts_at<=?
                        AND (e.expires_at IS NULL OR e.expires_at>?)"""
                    + where
                    + """ GROUP BY i.id
                       ORDER BY i.created_at DESC, i.id DESC LIMIT ? OFFSET ?""",
                    [now, now, *values, limit, offset],
                ).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        subscribers = [
            {
                "identity_id": row["id"],
                "email": row["email"],
                "is_active": bool(row["is_active"]),
                "email_verified": row["email_verified_at"] is not None,
                "created_at": row["created_at"],
                "complimentary_expires_at": row["complimentary_expires_at"],
                "complimentary_campaign": row["complimentary_campaign"],
                "complimentary_access_active": bool(
                    row["complimentary_expires_at"]
                    and row["complimentary_expires_at"] > now
                ),
                "entitlements": sorted(
                    key for key in row["entitlements"].split(",") if key
                ),
            }
            for row in rows
        ]
        return {
            "subscribers": subscribers,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

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

        The administrator-only audit API uses this storage primitive. Results
        omit identity email addresses and authentication material.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Audit page size must be between 1 and 500.")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10_000_000
        ):
            raise ValueError("Audit offset must be between 0 and 10000000.")
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
            # Keep the total and page on the same read snapshot while new
            # audit events may be appended by concurrent requests.
            db.execute("BEGIN")
            try:
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
                db.commit()
            except Exception:
                db.rollback()
                raise
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
                    id, actor_identity_id, event_type, subject_identity_id,
                    occurred_at, metadata_json
                ) VALUES (?, ?, 'administrator.mfa_enrolled', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, identity_id, now),
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
                    id, actor_identity_id, event_type, subject_identity_id,
                    occurred_at, metadata_json
                ) VALUES (?, ?, 'administrator.mfa_verified', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, identity_id, now),
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
                    id, actor_identity_id, event_type, subject_identity_id,
                    occurred_at, metadata_json
                ) VALUES (?, ?, 'administrator.mfa_recovery_used', ?, ?, '{}')""",
                (secrets.token_urlsafe(18), identity_id, identity_id, now),
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

    def revoke_subscriber_sessions(
        self, identity_id: str, *, actor_identity_id: str, reason_code: str,
    ) -> int:
        """Revoke every active session for one subscriber with MFA-admin audit attribution."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity reference is required.")
        if (
            not isinstance(actor_identity_id, str) or not actor_identity_id
            or len(actor_identity_id) > 128 or actor_identity_id == identity_id
        ):
            raise ValueError("A separate administrator actor is required.")
        if reason_code not in {"security_review", "support_request", "account_request", "other"}:
            raise ValueError("A supported session revocation reason code is required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subject = db.execute(
                "SELECT account_type FROM identities WHERE id=?", (identity_id,)
            ).fetchone()
            if subject is None or subject["account_type"] != "subscriber":
                db.rollback()
                raise ValueError("A subscriber identity was not found.")
            audit_actor = self._resolve_audit_actor(db, identity_id, actor_identity_id)
            now = _timestamp(_now())
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE identity_id=? AND revoked_at IS NULL""",
                (now, identity_id),
            )
            revoked_count = cursor.rowcount
            self._record_audit_event(
                db, "administrator.subscriber_sessions_revoked", identity_id, now,
                {"reason_code": reason_code, "revoked_count": revoked_count},
                actor_identity_id=audit_actor,
            )
            db.commit()
        return revoked_count

    def list_subscriber_sessions(
        self, identity_id: str, *, current_session_id: str,
        limit: int = 25, offset: int = 0,
    ) -> dict:
        """Return bounded session metadata for one active subscriber only."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity is required.")
        if not isinstance(current_session_id, str) or not current_session_id or len(current_session_id) > 128:
            raise ValueError("A valid current session is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Session page size must be between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000_000:
            raise ValueError("Session offset must be between 0 and 10000000.")
        now = _timestamp(_now())
        with self.connect() as db:
            owner = db.execute(
                "SELECT account_type, is_active FROM identities WHERE id=?",
                (identity_id,),
            ).fetchone()
            if owner is None or owner["account_type"] != "subscriber" or not owner["is_active"]:
                raise ValueError("An active subscriber account is required.")
            where = (
                "s.identity_id=? AND s.revoked_at IS NULL AND s.expires_at>? "
                "AND i.is_active=1"
            )
            total = db.execute(
                "SELECT COUNT(*) FROM sessions s JOIN identities i ON i.id=s.identity_id WHERE " + where,
                (identity_id, now),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT s.id AS session_id, s.created_at, s.last_seen_at,
                          s.expires_at, (s.id=?) AS is_current
                   FROM sessions s JOIN identities i ON i.id=s.identity_id
                   WHERE """ + where + """
                   ORDER BY (s.id=?) DESC, s.last_seen_at DESC, s.id
                   LIMIT ? OFFSET ?""",
                (current_session_id, identity_id, now, current_session_id, limit, offset),
            ).fetchall()
        sessions = []
        for row in rows:
            item = dict(row)
            item["is_current"] = bool(item["is_current"])
            sessions.append(item)
        return {
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def revoke_subscriber_session_by_id(
        self, identity_id: str, session_id: str,
    ) -> bool:
        """Revoke one session only when it belongs to the authenticated subscriber."""
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid subscriber identity is required.")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("A valid session reference is required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT account_type, is_active FROM identities WHERE id=?",
                (identity_id,),
            ).fetchone()
            if owner is None or owner["account_type"] != "subscriber" or not owner["is_active"]:
                db.rollback()
                raise ValueError("An active subscriber account is required.")
            now = _timestamp(_now())
            row = db.execute(
                """SELECT id FROM sessions
                   WHERE id=? AND identity_id=? AND revoked_at IS NULL AND expires_at>?""",
                (session_id, identity_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE id=? AND identity_id=? AND revoked_at IS NULL AND expires_at>?""",
                (now, session_id, identity_id, now),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return False
            self._record_audit_event(
                db,
                "subscriber.session_revoked",
                identity_id,
                now,
                {"self_service": True},
                actor_identity_id=identity_id,
            )
            db.commit()
        return True

    def list_active_sessions(
        self, *, actor_identity_id: str, current_session_id: str,
        limit: int = 25, offset: int = 0,
    ) -> dict:
        """Return bounded active-session metadata to an authenticated administrator."""
        if not isinstance(actor_identity_id, str) or not actor_identity_id or len(actor_identity_id) > 128:
            raise ValueError("A valid administrator identity is required.")
        if not isinstance(current_session_id, str) or not current_session_id or len(current_session_id) > 128:
            raise ValueError("A valid current administrator session is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Session page size must be between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000_000:
            raise ValueError("Session offset must be between 0 and 10000000.")
        now = _timestamp(_now())
        with self.connect() as db:
            actor = db.execute(
                """SELECT i.account_type, i.is_active, m.enrolled_at
                   FROM identities i LEFT JOIN administrator_mfa m ON m.identity_id=i.id
                   WHERE i.id=?""", (actor_identity_id,)
            ).fetchone()
            if (actor is None or actor["account_type"] != "administrator"
                    or not actor["is_active"] or actor["enrolled_at"] is None):
                raise ValueError("An active MFA-enrolled administrator is required.")
            where = "s.revoked_at IS NULL AND s.expires_at>? AND i.is_active=1 AND s.id<>?"
            total = db.execute(
                "SELECT COUNT(*) FROM sessions s JOIN identities i ON i.id=s.identity_id WHERE " + where,
                (now, current_session_id),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT s.id AS session_id, s.identity_id, i.account_type,
                          s.created_at, s.last_seen_at, s.expires_at
                   FROM sessions s JOIN identities i ON i.id=s.identity_id
                   WHERE """ + where + """
                   ORDER BY s.last_seen_at DESC, s.id LIMIT ? OFFSET ?""",
                (now, current_session_id, limit, offset),
            ).fetchall()
        return {"sessions": [dict(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def revoke_session_by_id(
        self, session_id: str, *, actor_identity_id: str,
        current_session_id: str, reason_code: str,
    ) -> bool:
        """Revoke one session by opaque ID without ever handling its bearer token."""
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("A valid session reference is required.")
        if not isinstance(current_session_id, str) or not current_session_id:
            raise ValueError("A valid current administrator session is required.")
        if session_id == current_session_id:
            raise ValueError("The current administrator session cannot be revoked here.")
        if reason_code not in {"security_review", "support_request", "account_request", "other"}:
            raise ValueError("A supported session revocation reason code is required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(_now())
            row = db.execute(
                """SELECT s.identity_id, i.account_type FROM sessions s
                   JOIN identities i ON i.id=s.identity_id
                   WHERE s.id=? AND s.revoked_at IS NULL AND s.expires_at>? AND i.is_active=1""",
                (session_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            try:
                actor = self._resolve_audit_actor(db, row["identity_id"], actor_identity_id)
            except ValueError:
                db.rollback()
                raise
            cursor = db.execute(
                """UPDATE sessions SET revoked_at=?
                   WHERE id=? AND revoked_at IS NULL AND expires_at>?""",
                (now, session_id, now),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return False
            self._record_audit_event(
                db, "administrator.session_revoked", row["identity_id"], now,
                {"reason_code": reason_code, "account_type": row["account_type"]},
                actor_identity_id=actor,
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
