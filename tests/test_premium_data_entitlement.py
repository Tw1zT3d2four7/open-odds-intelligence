from datetime import datetime, timezone

from app.identity import IdentityStore


def test_premium_data_is_an_explicit_server_side_entitlement(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator(
        "premium-admin@example.com", "administrator password"
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO administrator_mfa(identity_id, enrolled_at) VALUES (?, ?)",
            (admin_id, datetime.now(timezone.utc).isoformat()),
        )

    subscriber_id = store.create_subscriber(
        "premium-subscriber@example.com", "subscriber password"
    )
    challenge = store.create_email_verification_challenge(
        "premium-subscriber@example.com"
    )
    assert store.consume_email_verification_challenge(challenge) is True

    store.set_subscriber_entitlements(
        subscriber_id,
        ["dashboard", "odds", "premium_data"],
        actor_identity_id=admin_id,
        reason_code="promotional_access",
    )

    assert store.has_subscriber_entitlement(subscriber_id, "dashboard") is True
    assert store.has_subscriber_entitlement(subscriber_id, "premium_data") is True
    assert store.has_subscriber_entitlement(subscriber_id, "unknown_data_scope") is False
