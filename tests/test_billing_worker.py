import json

import pytest
from cryptography.fernet import Fernet

from app import config
from app.billing_worker import process_pending_stripe_events
from app.identity import IdentityStore


@pytest.fixture(autouse=True)
def stripe_encryption_key(monkeypatch):
    monkeypatch.setattr(
        config, "STRIPE_WEBHOOK_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )


def test_worker_processes_verified_subscription_and_is_idempotent(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "worker-subscriber@example.test", "a strong worker subscriber password"
    )
    now = "2026-11-25T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            "INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at) "
            "VALUES (?, 'cus_worker', ?, ?)",
            (subscriber_id, now, now),
        )
    event = {
        "id": "evt_worker",
        "type": "customer.subscription.created",
        "created": 1795650000,
        "data": {"object": {
            "id": "sub_worker", "customer": "cus_worker", "status": "trialing",
            "items": {"data": [{"price": {"id": "price_year", "recurring": {"interval": "year"}}}]},
        }},
    }
    store.record_stripe_webhook_event(
        "evt_worker", event["type"], False,
        json.dumps(event, separators=(",", ":")).encode(),
    )
    assert process_pending_stripe_events(store) == {
        "claimed": 1, "created": 1, "updated": 0, "stale": 0,
        "ignored": 0, "failed": 0, "lease_lost": 0,
    }
    assert process_pending_stripe_events(store)["claimed"] == 0
    with store.connect() as db:
        row = db.execute(
            "SELECT status, billing_interval FROM billing_subscriptions "
            "WHERE stripe_subscription_id='sub_worker'"
        ).fetchone()
    assert row["status"] == "trialing"
    assert row["billing_interval"] == "year"


def test_worker_releases_unmapped_customer_for_bounded_retry(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    event = {
        "id": "evt_unmapped",
        "type": "customer.subscription.created",
        "created": 1795650000,
        "data": {"object": {"id": "sub_unmapped", "customer": "cus_unmapped", "status": "active"}},
    }
    store.record_stripe_webhook_event(
        "evt_unmapped", event["type"], False,
        json.dumps(event, separators=(",", ":")).encode(),
    )
    totals = process_pending_stripe_events(store)
    assert totals["failed"] == 1
    with store.connect() as db:
        row = db.execute(
            "SELECT processing_state, last_error_code, attempt_count "
            "FROM billing_webhook_events WHERE stripe_event_id='evt_unmapped'"
        ).fetchone()
    assert row["processing_state"] == "failed"
    assert row["last_error_code"] == "customer_unmapped"
    assert row["attempt_count"] == 1


def test_worker_batch_limit_is_bounded(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError, match="batch limit"):
        process_pending_stripe_events(store, limit=501)
