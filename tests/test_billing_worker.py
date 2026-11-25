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



def test_worker_reconciles_invoice_payment_state_without_granting_access(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "invoice-subscriber@example.test", "a strong invoice subscriber password"
    )
    now = "2026-11-25T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            "INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at) "
            "VALUES (?, 'cus_invoice', ?, ?)", (subscriber_id, now, now),
        )
        db.execute(
            """INSERT INTO billing_subscriptions(
                id, identity_id, stripe_subscription_id, stripe_customer_id,
                status, created_at, updated_at
            ) VALUES ('local-invoice-sub', ?, 'sub_invoice', 'cus_invoice', 'active', ?, ?)""",
            (subscriber_id, now, now),
        )

    def enqueue(event_id, event_type, created, amount_paid):
        event = {
            "id": event_id, "type": event_type, "created": created,
            "data": {"object": {
                "id": "in_invoice", "customer": "cus_invoice",
                "subscription": "sub_invoice", "amount_due": 2500,
                "amount_paid": amount_paid, "currency": "usd",
                "lines": {"data": [{"period": {"start": 1795650000, "end": 1798242000}}]},
            }},
        }
        store.record_stripe_webhook_event(
            event_id, event_type, False,
            json.dumps(event, separators=(",", ":")).encode(),
        )

    enqueue("evt_invoice_paid", "invoice.paid", 1795650000, 2500)
    first = process_pending_stripe_events(store)
    assert first["created"] == 1
    with store.connect() as db:
        row = db.execute(
            "SELECT status, amount_due, amount_paid, currency, stripe_subscription_id "
            "FROM billing_invoices WHERE stripe_invoice_id='in_invoice'"
        ).fetchone()
        entitlements = db.execute(
            "SELECT COUNT(*) FROM subscriber_entitlements WHERE identity_id=?",
            (subscriber_id,),
        ).fetchone()[0]
    assert row["status"] == "paid"
    assert row["amount_due"] == 2500
    assert row["amount_paid"] == 2500
    assert row["currency"] == "usd"
    assert row["stripe_subscription_id"] == "sub_invoice"
    assert entitlements == 0

    enqueue("evt_invoice_failed_later", "invoice.payment_failed", 1795653600, 0)
    second = process_pending_stripe_events(store)
    assert second["updated"] == 1
    with store.connect() as db:
        row = db.execute(
            "SELECT status, amount_paid, payment_failed_at FROM billing_invoices WHERE stripe_invoice_id='in_invoice'"
        ).fetchone()
    assert row["status"] == "open"
    assert row["amount_paid"] == 0
    assert row["payment_failed_at"] == "2026-11-26T00:40:00+00:00"

    enqueue("evt_invoice_old", "invoice.paid", 1795640000, 2500)
    third = process_pending_stripe_events(store)
    assert third["stale"] == 1
    with store.connect() as db:
        row = db.execute(
            "SELECT status FROM billing_invoices WHERE stripe_invoice_id='in_invoice'"
        ).fetchone()
    assert row["status"] == "open"
