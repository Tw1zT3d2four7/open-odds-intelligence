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


def _regular_file(path: Path, *, must_exist: bool = True) -> None:
    if path.is_symlink():
        raise ValueError("Symlink paths are not permitted for identity backups.")
    if must_exist:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Identity database and backup paths must be regular files.")
        if info.st_mode & 0o077:
            raise PermissionError("Identity database and backup files must not be accessible by group or others.")


def _snapshot_database(path: Path) -> tuple[bytes, int]:
    _regular_file(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=5.0)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
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
        if version < 1 or version > _SCHEMA_VERSION:
            raise ValueError("Identity database schema version is unsupported.")
        return snapshot.serialize(), version
    except sqlite3.Error:
        raise ValueError("Identity database could not be backed up safely.") from None
    finally:
        snapshot.close()
        source.close()


def _validate_database_bytes(data: bytes) -> int:
    if not isinstance(data, bytes) or not data or len(data) > _MAX_BACKUP_BYTES:
        raise ValueError("Identity backup payload size is invalid.")
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("Identity backup integrity check failed.")
        identity_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identities'"
        ).fetchone()
        if identity_table is None:
            raise ValueError("Backup does not contain an identity database.")
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = int(row[0] or 0) if row else 0
        if version < 1 or version > _SCHEMA_VERSION:
            raise ValueError("Identity backup schema version is unsupported.")
        return version
    except sqlite3.Error:
        raise ValueError("Identity backup is not a valid identity database.") from None
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
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise ValueError("Identity backup directory cannot be a symlink.")
    os.chmod(parent, 0o700)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Identity backup destination already exists.")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow,
        0o600,
    )
    try:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(descriptor)
    directory_fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

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
    if size <= len(_MAGIC) or size > _MAX_BACKUP_BYTES + 1024 * 1024:
        raise ValueError("Identity backup file size is invalid.")
    encrypted = path.read_bytes()
    if not encrypted.startswith(_MAGIC):
        raise ValueError("Identity backup format is not recognized.")
    try:
        data = cipher.decrypt(encrypted[len(_MAGIC):])
    except InvalidToken:
        raise ValueError("Identity backup could not be authenticated with this key.") from None
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
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise ValueError("Identity restore directory cannot be a symlink.")
    os.chmod(parent, 0o700)

    if backup.stat().st_size <= len(_MAGIC) or backup.stat().st_size > _MAX_BACKUP_BYTES + 1024 * 1024:
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
