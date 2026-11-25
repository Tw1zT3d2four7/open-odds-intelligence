"""Encrypted, durable queue for verification and password-reset email.

Queue payloads contain challenge links and recipient addresses, so they are
Fernet-encrypted at rest with a dedicated key. Provider I/O always occurs
outside SQLite write transactions. Retries reuse one stable idempotency key.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .email_delivery import EmailDeliveryConfigurationError, EmailDeliveryError
from .identity import IdentityStore, _timestamp


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key_cipher() -> Fernet:
    key = config.EMAIL_QUEUE_ENCRYPTION_KEY
    if not key:
        raise EmailDeliveryConfigurationError(
            "Transactional email queue encryption is not configured."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise EmailDeliveryConfigurationError(
            "Transactional email queue encryption key is invalid."
        ) from None


def validate_email_queue_configuration() -> None:
    """Fail before accepting a challenge when encrypted queue storage is unavailable."""
    _key_cipher()


class TransactionalEmailQueue:
    """Database-backed queue with short leases and bounded idempotent retries."""

    def __init__(self, store: IdentityStore):
        self.store = store

    def enqueue(
        self,
        *,
        identity_id: str,
        purpose: str,
        challenge_token: str,
        recipient: str,
        subject: str,
        text: str,
        html: str | None,
        challenge_expires_at: datetime,
    ) -> str:
        if purpose not in {"verification", "password_reset"}:
            raise ValueError("Unsupported transactional email purpose.")
        if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
            raise ValueError("A valid identity reference is required.")
        if not isinstance(challenge_token, str) or not challenge_token or len(challenge_token) > 256:
            raise ValueError("A valid one-time challenge token is required.")
        if not isinstance(challenge_expires_at, datetime):
            raise ValueError("A challenge expiry timestamp is required.")
        now = _now()
        expiry = challenge_expires_at.astimezone(timezone.utc)
        if expiry <= now:
            raise ValueError("The email challenge has already expired.")
        payload = {
            "recipient": recipient,
            "subject": subject,
            "text": text,
            "html": html,
        }
        encrypted = _key_cipher().encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        challenge_token_hash = hashlib.sha256(challenge_token.encode("utf-8")).hexdigest()
        idempotency_key = f"ooi-{purpose}-{identity_id}-{fingerprint}"
        queue_id = secrets.token_urlsafe(24)
        created = _timestamp(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO transactional_email_queue(
                    id, provider, identity_id, purpose, encrypted_payload,
                    idempotency_key, state, attempt_count, max_attempts,
                    next_attempt_at, challenge_expires_at, idempotency_expires_at,
                    created_at, updated_at, challenge_token_hash
                ) VALUES (?, 'resend', ?, ?, ?, ?, 'pending', 0, 8, ?, ?, ?, ?, ?, ?)""",
                (
                    queue_id, identity_id, purpose, encrypted, idempotency_key,
                    created, _timestamp(expiry),
                    _timestamp(now + timedelta(hours=23)),
                    created, created, challenge_token_hash,
                ),
            )
            db.commit()
        return queue_id

    def _claim(self) -> dict | None:
        now = _now()
        stamp = _timestamp(now)
        lease_token = secrets.token_urlsafe(32)
        lease_hash = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """DELETE FROM transactional_email_queue
                   WHERE state IN ('sent', 'failed', 'expired') AND updated_at<?""",
                (_timestamp(now - timedelta(days=30)),),
            )
            db.execute(
                """UPDATE transactional_email_queue
                   SET state='expired', encrypted_payload=X'', lease_token_hash=NULL,
                       lease_expires_at=NULL, updated_at=?
                   WHERE state IN ('pending', 'leased') AND challenge_expires_at<=?""",
                (stamp, stamp),
            )
            db.execute(
                """UPDATE transactional_email_queue
                   SET state='failed', encrypted_payload=X'', lease_token_hash=NULL,
                       lease_expires_at=NULL, last_failure_code='attempts_exhausted',
                       updated_at=?
                   WHERE state IN ('pending', 'leased') AND attempt_count>=max_attempts""",
                (stamp,),
            )
            # Never deliver a link after its one-time challenge has been consumed,
            # revoked, deleted, or expired. Only a digest is stored in the queue.
            db.execute(
                """UPDATE transactional_email_queue
                   SET state='expired', encrypted_payload=X'', lease_token_hash=NULL,
                       lease_expires_at=NULL, last_failure_code='challenge_unavailable',
                       updated_at=?
                   WHERE state IN ('pending', 'leased') AND (
                     challenge_token_hash IS NULL
                     OR (
                       purpose='verification' AND NOT EXISTS (
                         SELECT 1 FROM email_verification_challenges c
                         WHERE c.identity_id=transactional_email_queue.identity_id
                           AND c.token_hash=transactional_email_queue.challenge_token_hash
                           AND c.consumed_at IS NULL AND c.expires_at>?
                       )
                     )
                     OR (
                       purpose='password_reset' AND NOT EXISTS (
                         SELECT 1 FROM password_reset_challenges c
                         WHERE c.identity_id=transactional_email_queue.identity_id
                           AND c.token_hash=transactional_email_queue.challenge_token_hash
                           AND c.consumed_at IS NULL AND c.expires_at>?
                       )
                     )
                   )""",
                (stamp, stamp, stamp),
            )
            db.execute(
                """UPDATE transactional_email_queue
                   SET state='failed', encrypted_payload=X'', lease_token_hash=NULL,
                       lease_expires_at=NULL, last_failure_code='idempotency_window_elapsed',
                       updated_at=?
                   WHERE state IN ('pending', 'leased')
                     AND idempotency_expires_at<=? AND challenge_expires_at>?""",
                (stamp, stamp, stamp),
            )
            row = db.execute(
                """SELECT * FROM transactional_email_queue
                   WHERE (
                     (state='pending' AND next_attempt_at<=?)
                     OR (state='leased' AND lease_expires_at<=?)
                   )
                   AND challenge_expires_at>? AND idempotency_expires_at>?
                   AND attempt_count<max_attempts
                   ORDER BY next_attempt_at, created_at LIMIT 1""",
                (stamp, stamp, stamp, stamp),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """UPDATE transactional_email_queue
                   SET state='leased', lease_token_hash=?, lease_expires_at=?,
                       attempt_count=attempt_count+1, updated_at=?
                   WHERE id=?""",
                (
                    lease_hash, _timestamp(now + timedelta(seconds=60)),
                    stamp, row["id"],
                ),
            )
            claimed = dict(row)
            claimed["attempt_count"] = int(row["attempt_count"]) + 1
            claimed["lease_token"] = lease_token
            db.commit()
            return claimed

    def _finish(
        self,
        item: dict,
        *,
        state: str,
        failure_code: str | None = None,
        delay_seconds: int = 0,
    ) -> bool:
        now = _now()
        lease_hash = hashlib.sha256(item["lease_token"].encode("ascii")).hexdigest()
        if state not in {"pending", "sent", "failed", "expired"}:
            raise ValueError("Invalid email queue completion state.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE transactional_email_queue
                   SET state=?, next_attempt_at=?, lease_token_hash=NULL,
                       lease_expires_at=NULL, last_failure_code=?,
                       attempt_count=CASE
                           WHEN ? IN ('circuit_open','circuit_state_unavailable')
                           THEN MAX(0, attempt_count-1) ELSE attempt_count END,
                       encrypted_payload=CASE WHEN ? IN ('sent','failed','expired')
                                               THEN X'' ELSE encrypted_payload END,
                       sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                       updated_at=?
                   WHERE id=? AND state='leased' AND lease_token_hash=?
                     AND lease_expires_at>?""",
                (
                    state, _timestamp(now + timedelta(seconds=max(0, delay_seconds))),
                    failure_code, failure_code, state, state, _timestamp(now),
                    _timestamp(now), item["id"], lease_hash, _timestamp(now),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def process_one(self, sender) -> bool:
        """Deliver at most one message. Returns True when a queue item was handled."""
        item = self._claim()
        if item is None:
            return False

        try:
            if not self.store.claim_email_provider_send():
                self._finish(item, state="pending", failure_code="circuit_open", delay_seconds=300)
                return True
        except Exception:
            self._finish(item, state="pending", failure_code="circuit_state_unavailable", delay_seconds=60)
            return True

        try:
            payload = json.loads(_key_cipher().decrypt(item["encrypted_payload"]).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Invalid encrypted email payload.")
            message_id = sender.send(
                payload["recipient"], payload["subject"],
                text=payload["text"], html=payload.get("html"),
                idempotency_key=item["idempotency_key"],
            )
        except EmailDeliveryError as exc:
            try:
                self.store.record_email_delivery_event(
                    item["identity_id"], item["purpose"], "failed",
                    failure_code=exc.failure_code,
                )
            except Exception:
                pass
            transient = exc.failure_code in {
                "transport", "provider_rate_limit", "provider_unavailable"
            }
            can_retry = (
                transient
                and item["attempt_count"] < item["max_attempts"]
                and _timestamp(_now()) < item["idempotency_expires_at"]
                and _timestamp(_now()) < item["challenge_expires_at"]
            )
            delay = min(
                3600,
                15 * (2 ** min(item["attempt_count"] - 1, 8)) + secrets.randbelow(5),
            )
            self._finish(
                item, state="pending" if can_retry else "failed",
                failure_code=exc.failure_code, delay_seconds=delay if can_retry else 0,
            )
            return True
        except (InvalidToken, KeyError, TypeError, ValueError, UnicodeDecodeError):
            self._finish(item, state="failed", failure_code="payload_invalid")
            return True
        except Exception:
            self._finish(item, state="failed", failure_code="unexpected")
            return True

        try:
            self.store.record_email_delivery_event(
                item["identity_id"], item["purpose"], "sent",
                provider_message_id=message_id,
            )
        except Exception:
            # Requeue with the same provider idempotency key. Resend will return
            # the original message result rather than sending a duplicate.
            self._finish(item, state="pending", failure_code="delivery_record_unavailable", delay_seconds=30)
            return True
        self._finish(item, state="sent")
        return True
