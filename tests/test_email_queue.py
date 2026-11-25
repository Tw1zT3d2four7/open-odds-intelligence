from datetime import datetime, timedelta, timezone
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from app import email_queue as queue_module
from app.email_delivery import EmailDeliveryError
from app.email_queue import TransactionalEmailQueue
from app.identity import IdentityStore


class FakeSender:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = []

    def send(self, recipient, subject, *, text, html=None, idempotency_key=None):
        self.calls.append((recipient, subject, text, html, idempotency_key))
        if self.failures:
            raise self.failures.pop(0)
        return "resend_message_123"


def _setup(tmp_path, monkeypatch, *, now=None):
    monkeypatch.setattr(
        queue_module.config,
        "EMAIL_QUEUE_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("member@example.test", "a strong password 123")
    queue = TransactionalEmailQueue(store)
    clock = now or datetime.now(timezone.utc)
    challenge_token = store.create_email_verification_challenge("member@example.test")
    queue_id = queue.enqueue(
        identity_id=identity_id,
        purpose="verification",
        challenge_token=challenge_token,
        recipient="member@example.test",
        subject="Verify account",
        text="Use challenge secret-token-987",
        html="<p>secret-token-987</p>",
        challenge_expires_at=clock + timedelta(hours=2),
    )
    return store, queue, identity_id, queue_id, clock


def test_queue_encrypts_recipient_and_challenge_payload_at_rest(tmp_path, monkeypatch):
    store, _queue, _identity_id, queue_id, _clock = _setup(tmp_path, monkeypatch)
    with store.connect() as db:
        row = db.execute(
            "SELECT encrypted_payload, state FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "pending"
    assert b"member@example.test" not in row["encrypted_payload"]
    assert b"secret-token-987" not in row["encrypted_payload"]
    # Identity records intentionally retain the account email; the queue payload
    # itself must be ciphertext and must not duplicate the address or challenge.


def test_queue_worker_sends_and_records_secret_free_success(tmp_path, monkeypatch):
    store, queue, identity_id, queue_id, _clock = _setup(tmp_path, monkeypatch)
    sender = FakeSender()
    assert queue.process_one(sender) is True
    assert sender.calls[0][0] == "member@example.test"
    with store.connect() as db:
        row = db.execute(
            "SELECT state, encrypted_payload, sent_at FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        event = db.execute(
            "SELECT status, provider_message_id FROM email_delivery_events WHERE identity_id=?",
            (identity_id,),
        ).fetchone()
    assert row["state"] == "sent"
    assert row["encrypted_payload"] == b""
    assert row["sent_at"] is not None
    assert event["status"] == "sent"
    assert event["provider_message_id"] == "resend_message_123"


def test_transient_retry_reuses_exact_idempotency_key_and_payload(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(queue_module, "_now", lambda: now[0])
    store, queue, _identity_id, queue_id, _clock = _setup(tmp_path, monkeypatch)
    sender = FakeSender([EmailDeliveryError("sanitized", "transport")])

    assert queue.process_one(sender) is True
    with store.connect() as db:
        row = db.execute(
            "SELECT state, attempt_count, next_attempt_at FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["attempt_count"] == 1

    now[0] += timedelta(seconds=20)
    assert queue.process_one(sender) is True
    assert len(sender.calls) == 2
    assert sender.calls[0] == sender.calls[1]
    with store.connect() as db:
        row = db.execute(
            "SELECT state, attempt_count FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "sent"
    assert row["attempt_count"] == 2


def test_two_workers_cannot_claim_the_same_active_lease(tmp_path, monkeypatch):
    store, queue, _identity_id, _queue_id, _clock = _setup(tmp_path, monkeypatch)
    other = TransactionalEmailQueue(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda q: q._claim(), (queue, other)))
    assert sum(claim is not None for claim in claims) == 1


def test_missing_or_invalid_queue_encryption_key_fails_closed(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    queue = TransactionalEmailQueue(store)
    monkeypatch.setattr(queue_module.config, "EMAIL_QUEUE_ENCRYPTION_KEY", "")
    with pytest.raises(Exception, match="encryption is not configured"):
        queue.enqueue(
            identity_id="identity",
            purpose="verification",
            challenge_token="token",
            recipient="member@example.test",
            subject="Verify",
            text="secret-token",
            html=None,
            challenge_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    monkeypatch.setattr(queue_module.config, "EMAIL_QUEUE_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(Exception, match="key is invalid"):
        queue.enqueue(
            identity_id="identity",
            purpose="verification",
            challenge_token="token",
            recipient="member@example.test",
            subject="Verify",
            text="secret-token",
            html=None,
            challenge_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_expired_challenge_is_never_delivered(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(queue_module, "_now", lambda: now[0])
    store, queue, _identity_id, queue_id, _clock = _setup(tmp_path, monkeypatch, now=now[0])
    now[0] += timedelta(hours=3)
    sender = FakeSender()
    assert queue.process_one(sender) is False
    assert sender.calls == []
    with store.connect() as db:
        row = db.execute(
            "SELECT state, encrypted_payload FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "expired"
    assert row["encrypted_payload"] == b""



def test_consumed_challenge_is_discarded_before_provider_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        queue_module.config,
        "EMAIL_QUEUE_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("member@example.test", "a strong password 123")
    token = store.create_email_verification_challenge("member@example.test")
    queue = TransactionalEmailQueue(store)
    queue_id = queue.enqueue(
        identity_id=identity_id,
        purpose="verification",
        challenge_token=token,
        recipient="member@example.test",
        subject="Verify account",
        text=f"Use token {token}",
        html=None,
        challenge_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert store.consume_email_verification_challenge(token) is True

    sender = FakeSender()
    assert queue.process_one(sender) is False
    assert sender.calls == []
    with store.connect() as db:
        row = db.execute(
            "SELECT state, encrypted_payload, last_failure_code "
            "FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "expired"
    assert row["encrypted_payload"] == b""
    assert row["last_failure_code"] == "challenge_unavailable"



def test_expired_worker_lease_cannot_complete_after_reclaim(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(queue_module, "_now", lambda: now[0])
    store, queue, _identity_id, queue_id, _clock = _setup(
        tmp_path, monkeypatch, now=now[0]
    )

    stale = queue._claim()
    assert stale is not None
    now[0] += timedelta(seconds=61)
    assert queue._finish(stale, state="sent") is False

    current = queue._claim()
    assert current is not None
    assert current["lease_token"] != stale["lease_token"]
    assert queue._finish(
        stale, state="failed", failure_code="unexpected"
    ) is False
    assert queue._finish(current, state="sent") is True

    with store.connect() as db:
        row = db.execute(
            "SELECT state FROM transactional_email_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["state"] == "sent"
