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
    assert result["schema_version"] == 13
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert b"backup-user@example.test" not in backup.read_bytes()

    inspected = inspect_encrypted_backup(backup)
    assert inspected["schema_version"] == 13

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


def test_backup_never_overwrites_an_existing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    destination = tmp_path / "backups" / "identity.sqlite3.fernet"
    create_encrypted_backup(source, destination)
    original = destination.read_bytes()

    with pytest.raises(FileExistsError):
        create_encrypted_backup(source, destination)

    assert destination.read_bytes() == original
    assert list(destination.parent.glob(".identity-backup-*.tmp")) == []


def test_backup_publication_failure_never_leaves_a_partial_final_file(
    tmp_path, monkeypatch
):
    from app import identity_backup

    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "source.sqlite3"
    _identity_store(source)
    destination = tmp_path / "backups" / "identity.sqlite3.fernet"
    original_link = identity_backup.os.link

    def fail_publish(*_args, **_kwargs):
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(identity_backup.os, "link", fail_publish)
    with pytest.raises(OSError, match="simulated atomic publication failure"):
        create_encrypted_backup(source, destination)

    assert not destination.exists()
    assert list(destination.parent.glob(".identity-backup-*.tmp")) == []

    monkeypatch.setattr(identity_backup.os, "link", original_link)
    result = create_encrypted_backup(source, destination)
    assert result["schema_version"] == 13
    assert inspect_encrypted_backup(destination)["schema_version"] == 13


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
    assert inspect_encrypted_backup(pre_restore)["schema_version"] == 13

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


def test_backup_rejects_snapshot_above_configured_size_limit(tmp_path, monkeypatch):
    from app import identity_backup

    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setattr(identity_backup, "_MAX_BACKUP_BYTES", 1024)
    source = tmp_path / "oversized.sqlite3"
    _identity_store(source)

    with pytest.raises(ValueError, match="payload size"):
        create_encrypted_backup(source, tmp_path / "oversized.fernet")


def test_backup_rejects_incomplete_schema_even_with_current_version_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    incomplete = tmp_path / "incomplete.sqlite3"
    connection = sqlite3.connect(incomplete)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(8, 'now')")
    connection.execute("CREATE TABLE identities (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    incomplete.chmod(0o600)

    with pytest.raises(ValueError, match="schema is incomplete"):
        create_encrypted_backup(incomplete, tmp_path / "invalid.fernet")


def test_backup_rejects_malformed_optional_rate_limit_table(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "malformed-rate-limit.sqlite3"
    _identity_store(source)
    connection = sqlite3.connect(source)
    connection.execute(
        "CREATE TABLE auth_rate_limit_buckets (bucket_hash TEXT PRIMARY KEY, window_start INTEGER NOT NULL)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="schema is incomplete"):
        create_encrypted_backup(source, tmp_path / "malformed-rate-limit.fernet")


def test_backup_rejects_foreign_key_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    source = tmp_path / "broken-relations.sqlite3"
    _identity_store(source)
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        """INSERT INTO sessions(
            id, identity_id, token_hash, created_at, last_seen_at, expires_at
        ) VALUES ('orphan-session', 'missing-identity', 'orphan-token-hash', 'now', 'now', 'later')"""
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="foreign-key integrity check failed"):
        create_encrypted_backup(source, tmp_path / "invalid-relations.fernet")


def test_backup_and_restore_cli_requires_explicit_offline_apply(tmp_path, monkeypatch, capsys):
    import sys
    from scripts import backup_identity, restore_identity

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(config, "IDENTITY_BACKUP_ENCRYPTION_KEY", key)
    source = tmp_path / "source" / "accounts.sqlite3"
    _identity_store(source)
    monkeypatch.setattr(config, "ACCOUNTS_DATABASE_PATH", str(source))

    monkeypatch.setattr(sys, "argv", ["backup_identity"])
    assert backup_identity.main() == 0
    backups = list((source.parent / "backups").glob("identity-*.sqlite3.fernet"))
    assert len(backups) == 1
    backup = backups[0]
    assert inspect_encrypted_backup(backup)["schema_version"] == 13

    target = tmp_path / "target" / "accounts.sqlite3"
    target_store, old_identity_id = _identity_store(target)
    monkeypatch.setattr(config, "ACCOUNTS_DATABASE_PATH", str(target))

    # Inspection is the default and must never replace the current database.
    monkeypatch.setattr(sys, "argv", ["restore_identity", str(backup)])
    assert restore_identity.main() == 0
    with target_store.connect() as db:
        assert db.execute("SELECT 1 FROM identities WHERE id=?", (old_identity_id,)).fetchone()

    # --apply alone is insufficient and must fail before touching the target.
    monkeypatch.setattr(sys, "argv", ["restore_identity", str(backup), "--apply"])
    with pytest.raises(SystemExit):
        restore_identity.main()
    with target_store.connect() as db:
        assert db.execute("SELECT 1 FROM identities WHERE id=?", (old_identity_id,)).fetchone()

    monkeypatch.setattr(
        config, "IDENTITY_BACKUP_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["restore_identity", str(backup), "--apply", "--confirm-offline"],
    )
    assert restore_identity.main() == 2
    with target_store.connect() as db:
        assert db.execute("SELECT 1 FROM identities WHERE id=?", (old_identity_id,)).fetchone()
    assert not list((target.parent / "backups").glob("identity-pre-restore-*.sqlite3.fernet"))

    monkeypatch.setattr(config, "IDENTITY_BACKUP_ENCRYPTION_KEY", key)
    assert restore_identity.main() == 0
    restored_store = IdentityStore(target)
    with restored_store.connect() as db:
        restored = db.execute(
            "SELECT email FROM identities ORDER BY email"
        ).fetchall()
    assert [row["email"] for row in restored] == ["backup-user@example.test"]
    pre_restore = list((target.parent / "backups").glob("identity-pre-restore-*.sqlite3.fernet"))
    assert len(pre_restore) == 1
    assert inspect_encrypted_backup(pre_restore[0])["schema_version"] == 13


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
