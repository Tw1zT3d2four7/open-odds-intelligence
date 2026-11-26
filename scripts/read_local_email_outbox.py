"""Print locally queued authentication emails for development testing.

Run only inside the development container or on the trusted local host:
    python scripts/read_local_email_outbox.py
This intentionally prints one-time links to the local terminal and exposes no
HTTP endpoint. Never run it in staging or production.
"""
from __future__ import annotations

import json
import os
import sys

from cryptography.fernet import Fernet, InvalidToken

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import config
from app.auth_config import AUTHENTICATION_POLICY
from app.identity import IdentityStore


def main() -> int:
    if not AUTHENTICATION_POLICY.is_local_development or not AUTHENTICATION_POLICY.local_outbox_enabled:
        print("Local email outbox is available only with AUTH_ENVIRONMENT=development and AUTH_EMAIL_TRANSPORT=local_outbox.", file=sys.stderr)
        return 2
    if not config.EMAIL_QUEUE_ENCRYPTION_KEY:
        print("EMAIL_QUEUE_ENCRYPTION_KEY is not configured.", file=sys.stderr)
        return 2
    try:
        cipher = Fernet(config.EMAIL_QUEUE_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        print("EMAIL_QUEUE_ENCRYPTION_KEY is invalid.", file=sys.stderr)
        return 2

    store = IdentityStore()
    with store.connect() as db:
        rows = db.execute(
            """SELECT id, purpose, encrypted_payload, created_at, challenge_expires_at
               FROM transactional_email_queue
               WHERE state IN ('pending', 'leased')
               ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()

    if not rows:
        print("Local email outbox is empty.")
        return 0

    for row in rows:
        try:
            payload = json.loads(cipher.decrypt(row["encrypted_payload"]).decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError, TypeError):
            print(f"[{row['id']}] Encrypted message unavailable; check the configured queue key.")
            continue
        print("\n" + "=" * 72)
        print(f"Purpose: {row['purpose']}")
        print(f"To: {payload.get('recipient', '')}")
        print(f"Subject: {payload.get('subject', '')}")
        print(f"Created: {row['created_at']}")
        print(f"Challenge expires: {row['challenge_expires_at']}")
        print("-" * 72)
        print(payload.get("text", ""))
    print("\nThese one-time links are sensitive. Do not share this terminal output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
