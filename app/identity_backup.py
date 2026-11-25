"""Encrypted, offline backup and restore primitives for the identity database.

Only the accounts database is handled here. The odds-history database and
provider state are deliberately out of scope. Restore requires two explicit
operator confirmations and never runs automatically at application startup.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .identity import _SCHEMA_VERSION

_MAGIC = b"OOI-IDENTITY-BACKUP-V1\n"
_MAX_BACKUP_BYTES = 512 * 1024 * 1024


def _max_encrypted_backup_bytes() -> int:
    """Upper bound for Fernet base64 expansion plus framing and padding."""
    return len(_MAGIC) + 4 * ((_MAX_BACKUP_BYTES + 73 + 2) // 3)


def _cipher(key: str | bytes | None = None) -> Fernet:
    value = key if key is not None else config.IDENTITY_BACKUP_ENCRYPTION_KEY
    if isinstance(value, str):
        try:
            value = value.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("IDENTITY_BACKUP_ENCRYPTION_KEY is invalid.") from None
    if not isinstance(value, bytes) or not value:
        raise ValueError("IDENTITY_BACKUP_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(value)
    except (ValueError, TypeError):
        raise ValueError("IDENTITY_BACKUP_ENCRYPTION_KEY is invalid.") from None


def _normalize_journal_header(data: bytes) -> bytes:
    """Convert a consistent SQLite backup image from WAL to rollback mode.

    SQLite header bytes 18 and 19 encode write/read journal versions. The
    online backup API returns a self-contained consistent image; marking that
    image as rollback-journal mode prevents :memory: deserialization from
    attempting to open WAL sidecars for a special in-memory database name.
    """
    if not isinstance(data, bytes) or len(data) < 100 or not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("Identity backup is not a valid SQLite database image.")
    normalized = bytearray(data)
    if normalized[18] not in (1, 2) or normalized[19] not in (1, 2):
        raise ValueError("Identity backup SQLite journal header is invalid.")
    normalized[18] = 1
    normalized[19] = 1
    return bytes(normalized)


def _prepare_directory(path: Path) -> None:
    """Create or validate a leaf directory without changing caller-owned permissions."""
    if path.is_symlink():
        raise ValueError("Identity backup directories cannot be symlinks.")
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Identity backup and restore parents must be directories.")
    # Never chmod an existing caller-selected directory (for example /tmp or
    # a shared application directory). Refuse directories where others can
    # create, remove, or replace entries; the backup file itself is always 0600.
    if path.stat().st_mode & 0o022:
        raise PermissionError("Identity backup and restore directories must not be group- or world-writable.")


def _regular_file(path: Path, *, must_exist: bool = True) -> None:
    if path.is_symlink():
        raise ValueError("Symlink paths are not permitted for identity backups.")
    if must_exist:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Identity database and backup paths must be regular files.")
        if info.st_mode & 0o077:
            raise PermissionError("Identity database and backup files must not be accessible by group or others.")


def _validate_identity_schema(connection: sqlite3.Connection, version: int) -> None:
    """Reject incomplete or referentially corrupt identity database images."""
    if version < 1 or version > _SCHEMA_VERSION:
        raise ValueError("Identity database schema version is unsupported.")

    required_columns = {
        "schema_migrations": {"version", "applied_at"},
        "identities": {
            "id", "email", "account_type", "password_hash", "is_active",
            "email_verified_at", "created_at", "updated_at", "password_changed_at",
        },
        "sessions": {
            "id", "identity_id", "token_hash", "created_at", "last_seen_at",
            "expires_at", "revoked_at",
        },
        "email_verification_challenges": {
            "id", "identity_id", "token_hash", "created_at", "expires_at", "consumed_at",
        },
        "password_reset_challenges": {
            "id", "identity_id", "token_hash", "created_at", "expires_at", "consumed_at",
        },
        "administrator_mfa": {
            "identity_id", "encrypted_secret", "enrolled_at", "last_verified_at",
        },
        "security_audit_events": {
            "id", "actor_identity_id", "event_type", "subject_identity_id",
            "occurred_at", "metadata_json",
        },
    }
    if version >= 2:
        required_columns["administrator_mfa"].add("last_totp_counter")
        required_columns["administrator_mfa_recovery_codes"] = {
            "id", "identity_id", "code_hash", "created_at", "consumed_at",
        }
    if version >= 3:
        required_columns["administrator_mfa"].add("mfa_login_verified_at")
    if version >= 4:
        required_columns["email_delivery_events"] = {
            "id", "provider", "identity_id", "purpose", "status",
            "provider_message_id", "failure_code", "attempted_at",
        }
        required_columns["email_provider_circuit"] = {
            "provider", "consecutive_failures", "opened_until", "last_failure_at",
            "last_success_at", "last_error_code", "updated_at",
        }
    if version >= 5:
        required_columns["email_provider_circuit"].add("probe_until")
    if version >= 6:
        required_columns["transactional_email_queue"] = {
            "id", "provider", "identity_id", "purpose", "encrypted_payload",
            "idempotency_key", "state", "attempt_count", "max_attempts",
            "next_attempt_at", "lease_token_hash", "lease_expires_at",
            "challenge_expires_at", "idempotency_expires_at", "last_failure_code",
            "created_at", "updated_at", "sent_at",
        }
    if version >= 7:
        required_columns["transactional_email_queue"].add("challenge_token_hash")
    if version >= 8:
        required_columns["subscriber_entitlements"] = {
            "id", "identity_id", "entitlement_key", "starts_at", "expires_at",
            "granted_by", "created_at", "updated_at",
        }
    if version >= 9:
        required_columns["billing_customers"] = {
            "identity_id", "stripe_customer_id", "created_at", "updated_at",
        }
        required_columns["billing_subscriptions"] = {
            "id", "identity_id", "stripe_subscription_id", "stripe_customer_id",
            "stripe_price_id", "plan_code", "billing_interval", "status",
            "current_period_start", "current_period_end", "cancel_at_period_end",
            "canceled_at", "ended_at", "created_at", "updated_at",
        }
        required_columns["billing_webhook_events"] = {
            "stripe_event_id", "event_type", "livemode", "payload_sha256",
            "processing_state", "attempt_count", "received_at", "processed_at",
            "last_error_code",
        }
    if version >= 10:
        required_columns["billing_webhook_events"].update({
            "lease_token_hash", "lease_expires_at", "updated_at",
        })
    if version >= 11:
        required_columns["billing_webhook_events"].add("encrypted_payload")
    if version >= 12:
        required_columns["billing_subscriptions"].add("last_event_created_at")

    available_tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "auth_rate_limit_buckets" in available_tables:
        required_columns["auth_rate_limit_buckets"] = {
            "bucket_hash", "window_start", "request_count",
        }
    for table, columns in required_columns.items():
        if table not in available_tables:
            raise ValueError("Identity database schema is incomplete.")
        available_columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns.issubset(available_columns):
            raise ValueError("Identity database schema is incomplete.")

    applied_versions = {
        row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    if not set(range(1, version + 1)).issubset(applied_versions):
        raise ValueError("Identity database migration history is incomplete.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Identity database foreign-key integrity check failed.")


def _snapshot_database(path: Path) -> tuple[bytes, int]:
    _regular_file(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=5.0)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
        # The source database normally uses WAL. Serialize an in-memory snapshot
        # with MEMORY journaling so deserialize() never tries to create sidecars
        # for the special :memory: database name.
        snapshot.execute("PRAGMA journal_mode=MEMORY")
        integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("Identity database integrity check failed.")
        identity_table = snapshot.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identities'"
        ).fetchone()
        if identity_table is None:
            raise ValueError("Source database is not the identity database.")
        row = snapshot.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = int(row[0] or 0) if row else 0
        _validate_identity_schema(snapshot, version)
        data = _normalize_journal_header(snapshot.serialize())
        if len(data) > _MAX_BACKUP_BYTES:
            raise ValueError("Identity backup payload size is invalid.")
        return data, version
    except sqlite3.Error:
        raise ValueError("Identity database could not be backed up safely.") from None
    finally:
        snapshot.close()
        source.close()


def _validate_database_bytes(data: bytes) -> int:
    if not isinstance(data, bytes) or not data or len(data) > _MAX_BACKUP_BYTES:
        raise ValueError("Identity backup payload size is invalid.")
    data = _normalize_journal_header(data)
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA journal_mode=MEMORY")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("Identity backup integrity check failed.")
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = int(row[0] or 0) if row else 0
        _validate_identity_schema(connection, version)
        return version
    except sqlite3.Error as exc:
        raise ValueError(
            f"Identity backup is not a valid identity database: {type(exc).__name__}: {exc}"
        ) from None
    finally:
        connection.close()


def create_encrypted_backup(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    key: str | bytes | None = None,
) -> dict:
    """Create an atomic Fernet-encrypted SQLite snapshot without plaintext disk files."""
    cipher = _cipher(key)
    source = Path(source_path)
    destination = Path(destination_path)
    data, version = _snapshot_database(source)
    encrypted = _MAGIC + cipher.encrypt(data)

    parent = destination.parent
    _prepare_directory(parent)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Identity backup destination already exists.")

    # Write only to a private temporary file. Publish the completed backup with
    # an atomic, no-overwrite hard link so readers never observe a partial file
    # and a concurrent writer cannot replace an existing backup.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".identity-backup-", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    return {
        "path": str(destination),
        "schema_version": version,
        "encrypted_bytes": len(encrypted),
    }


def inspect_encrypted_backup(
    backup_path: str | Path,
    *,
    key: str | bytes | None = None,
) -> dict:
    """Authenticate and integrity-check a backup without writing plaintext to disk."""
    cipher = _cipher(key)
    path = Path(backup_path)
    _regular_file(path)
    size = path.stat().st_size
    if size <= len(_MAGIC) or size > _max_encrypted_backup_bytes():
        raise ValueError("Identity backup file size is invalid.")
    encrypted = path.read_bytes()
    if not encrypted.startswith(_MAGIC):
        raise ValueError("Identity backup format is not recognized.")
    try:
        data = cipher.decrypt(encrypted[len(_MAGIC):])
    except InvalidToken:
        raise ValueError("Identity backup could not be authenticated with this key.") from None
    data = _normalize_journal_header(data)
    version = _validate_database_bytes(data)
    return {"schema_version": version, "database_bytes": len(data)}


def restore_encrypted_backup(
    backup_path: str | Path,
    target_path: str | Path,
    *,
    backup_directory: str | Path,
    apply: bool = False,
    confirm_offline: bool = False,
    key: str | bytes | None = None,
) -> dict:
    """Restore only after explicit flags, preserving an encrypted pre-restore snapshot."""
    if not apply or not confirm_offline:
        raise PermissionError(
            "Restore requires both apply=True and confirm_offline=True."
        )
    cipher = _cipher(key)
    backup = Path(backup_path)
    target = Path(target_path)
    if target.resolve(strict=False) == Path(config.DATABASE_PATH).resolve(strict=False):
        raise ValueError("Refusing to overwrite the odds-history database.")
    _regular_file(backup)
    if target.is_symlink():
        raise ValueError("Identity restore target cannot be a symlink.")
    parent = target.parent
    _prepare_directory(parent)

    if backup.stat().st_size <= len(_MAGIC) or backup.stat().st_size > _max_encrypted_backup_bytes():
        raise ValueError("Identity backup file size is invalid.")
    encrypted = backup.read_bytes()
    if not encrypted.startswith(_MAGIC):
        raise ValueError("Identity backup format is not recognized.")
    try:
        data = cipher.decrypt(encrypted[len(_MAGIC):])
    except InvalidToken:
        raise ValueError("Identity backup could not be authenticated with this key.") from None
    version = _validate_database_bytes(data)

    sidecars = (Path(f"{target}-wal"), Path(f"{target}-shm"))
    if any(path.exists() or path.is_symlink() for path in sidecars):
        raise RuntimeError(
            "Identity database WAL/SHM sidecars exist. Stop the application and checkpoint the database before restore."
        )

    pre_restore_backup = None
    if target.exists():
        _regular_file(target)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pre_restore_backup = Path(backup_directory) / f"identity-pre-restore-{stamp}.sqlite3.fernet"
        create_encrypted_backup(target, pre_restore_backup, key=key)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".identity-restore-", suffix=".sqlite3", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Re-open and validate the exact bytes written before replacing anything.
        _regular_file(temporary)
        _validate_database_bytes(temporary.read_bytes())
        if target.exists() and target.is_symlink():
            raise ValueError("Identity restore target became a symlink.")
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    return {
        "restored": True,
        "schema_version": version,
        "pre_restore_backup": str(pre_restore_backup) if pre_restore_backup else None,
    }
