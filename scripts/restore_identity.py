"""Inspect or explicitly restore an encrypted identity database backup."""
from __future__ import annotations

import argparse
from pathlib import Path

from app import config
from app.identity_backup import inspect_encrypted_backup, restore_encrypted_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="Encrypted .sqlite3.fernet backup file")
    parser.add_argument(
        "--backup-directory",
        type=Path,
        help="Directory for the automatic encrypted pre-restore snapshot.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually replace the identity database. Without this, only inspect the backup.",
    )
    parser.add_argument(
        "--confirm-offline",
        action="store_true",
        help="Confirm the application is stopped and no process has the identity DB open.",
    )
    args = parser.parse_args()
    target = Path(config.ACCOUNTS_DATABASE_PATH)
    backup_directory = args.backup_directory or (target.parent / "backups")
    try:
        if not args.apply:
            if args.confirm_offline:
                parser.error("--confirm-offline requires --apply.")
            result = inspect_encrypted_backup(args.backup)
            print(
                "Backup verified: "
                f"schema v{result['schema_version']}, "
                f"{result['database_bytes']} database bytes. No restore performed."
            )
            return 0
        if not args.confirm_offline:
            parser.error("--apply requires --confirm-offline.")
        result = restore_encrypted_backup(
            args.backup,
            target,
            backup_directory=backup_directory,
            apply=True,
            confirm_offline=True,
        )
    except Exception as exc:
        print(f"Identity restore failed: {exc}")
        return 2
    print(
        "Identity database restored: "
        f"schema v{result['schema_version']}. "
        f"Pre-restore snapshot: {result['pre_restore_backup'] or 'not needed'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
