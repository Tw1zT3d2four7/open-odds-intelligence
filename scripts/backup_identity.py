"""Create an encrypted offline snapshot of the identity database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.identity_backup import create_encrypted_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional encrypted backup path; defaults to the protected data/backups directory.",
    )
    args = parser.parse_args()
    source = Path(config.ACCOUNTS_DATABASE_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = args.output or (
        source.parent / "backups" / f"identity-{stamp}.sqlite3.fernet"
    )
    try:
        result = create_encrypted_backup(source, destination)
    except Exception as exc:
        print(f"Identity backup failed: {exc}")
        return 2
    print(
        "Identity backup created: "
        f"{result['path']} (schema v{result['schema_version']}, "
        f"{result['encrypted_bytes']} encrypted bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
