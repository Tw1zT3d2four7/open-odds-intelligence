import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import config
from app.identity import IdentityStore
from app.identity_backup import (
    create_encrypted_backup,
    inspect_encrypted_backup,
    restore_encrypted_backup,
)


def _identity_store(path):
    store = IdentityStore(path)
    identity_id = store.create_subscriber(
        "backup-user@example.test", "a strong backup password"
    )
    return store, identity_id


def test_encrypted_identity_backup_and_restore_round_trip(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(config, "IDENTITY_BACKUP_ENCRYPTION_KEY", key)
    source = tmp_path / "data" / "accounts.sqlite3"
    source.parent.mkdir()
    _store, identity_id = _identity_store(source)

    backup = tmp_path / "backups" / "identity.sqlite3.fernet"
    result = create_encrypted_backup(source, backup)
    assert result["schema_version"] == 8
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert b"backup-user@example.test" not in backup.read_bytes()

    inspected = inspect_encrypted_backup(backup)
    assert inspected["schema_version"] == 8

    restored = tmp_path / "restored" / "accounts.sqlite3"
    result = restore_encrypted_backup(
        backup,
        restored,
        backup_directory=tmp_path / "restored-backups",
        apply=True,
        confirm_offline=True,
    )
    assert result["restored"] is True
    assert result["pre_restore_backup"] is None
    assert restored.stat().st_mode & 0o777 == 0o600

    restored_store = IdentityStore(restored)
    with restored_store.connect() as db:
        row = db.execute(
            "SELECT id, email FROM identities WHERE id=?",
            (identity_id,),
        ).fetchone()
    assert row["email"] == "backup-user@example.test"


def test_backup_preserves_existing_parent_permissions_and_rejects_shared_writable_dirs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)

    shared = tmp_path / "shared-backups"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    backup = shared / "identity.sqlite3.fernet"
    create_encrypted_backup(source, backup)
    assert shared.stat().st_mode & 0o777 == 0o755
    assert backup.stat().st_mode & 0o777 == 0o600

    unsafe = tmp_path / "unsafe-backups"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(PermissionError, match="group- or world-writable"):
        create_encrypted_backup(source, unsafe / "identity.sqlite3.fernet")
    assert unsafe.stat().st_mode & 0o777 == 0o777


def test_restore_creates_encrypted_pre_restore_snapshot(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(config, "IDENTITY_BACKUP_ENCRYPTION_KEY", key)
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)

    target = tmp_path / "target.sqlite3"
    target_store, old_identity_id = _identity_store(target)
    result = restore_encrypted_backup(
        backup,
        target,
        backup_directory=tmp_path / "backups",
        apply=True,
        confirm_offline=True,
    )
    assert result["pre_restore_backup"] is not None
    pre_restore = Path(result["pre_restore_backup"])
    assert pre_restore.exists()
    assert pre_restore.stat().st_mode & 0o777 == 0o600
    assert inspect_encrypted_backup(pre_restore)["schema_version"] == 8

    restored_store = IdentityStore(target)
    with restored_store.connect() as db:
        ids = {
            row[0] for row in db.execute("SELECT id FROM identities").fetchall()
        }
    assert old_identity_id not in ids


def test_restore_preserves_existing_target_parent_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)

    target_parent = tmp_path / "shared-target"
    target_parent.mkdir(mode=0o755)
    target_parent.chmod(0o755)
    target = target_parent / "accounts.sqlite3"
    restore_encrypted_backup(
        backup, target, backup_directory=tmp_path / "pre-restore", apply=True,
        confirm_offline=True,
    )
    assert target_parent.stat().st_mode & 0o777 == 0o755
    assert target.stat().st_mode & 0o777 == 0o600


def test_restore_requires_both_explicit_confirmation_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)

    with pytest.raises(PermissionError, match="apply=True"):
        restore_encrypted_backup(
            backup,
            tmp_path / "target.sqlite3",
            backup_directory=tmp_path / "backups",
        )


def test_wrong_backup_key_and_symlink_sources_fail_closed(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(config, "IDENTITY_BACKUP_ENCRYPTION_KEY", key)
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)

    with pytest.raises(ValueError, match="authenticated"):
        inspect_encrypted_backup(backup, key=Fernet.generate_key())

    link = tmp_path / "source-link.sqlite3"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="Symlink"):
        create_encrypted_backup(link, tmp_path / "link-backup.fernet")


def test_backup_refuses_odds_history_database(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    odds_db = tmp_path / "odds_history.sqlite3"
    connection = sqlite3.connect(odds_db)
    connection.execute("CREATE TABLE odds_history (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    odds_db.chmod(0o600)

    with pytest.raises(ValueError, match="identity database"):
        create_encrypted_backup(odds_db, tmp_path / "not-identity.fernet")


def test_restore_refuses_odds_database_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)
    odds_db = tmp_path / "odds_history.sqlite3"
    monkeypatch.setattr(config, "DATABASE_PATH", str(odds_db))

    with pytest.raises(ValueError, match="odds-history"):
        restore_encrypted_backup(
            backup,
            odds_db,
            backup_directory=tmp_path / "backups",
            apply=True,
            confirm_offline=True,
        )


def test_restore_refuses_live_wal_sidecars_without_touching_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    backup = tmp_path / "backup.sqlite3.fernet"
    create_encrypted_backup(source, backup)

    target = tmp_path / "target.sqlite3"
    _identity_store(target)
    before = target.read_bytes()
    Path(f"{target}-wal").write_bytes(b"active wal marker")

    with pytest.raises(RuntimeError, match="WAL/SHM"):
        restore_encrypted_backup(
            backup,
            target,
            backup_directory=tmp_path / "backups",
            apply=True,
            confirm_offline=True,
        )
    assert target.read_bytes() == before
