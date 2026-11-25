import sqlite3
from datetime import timedelta

import pytest

from app.identity import IdentityStore


def _verify_subscriber(store, email):
    challenge = store.create_email_verification_challenge(email)
    assert challenge
    assert store.consume_email_verification_challenge(challenge) is True


def test_identity_database_is_separate_and_restricts_file_permissions(tmp_path):
    odds_path = tmp_path / "odds_history.sqlite3"
    with sqlite3.connect(odds_path) as db:
        db.execute("CREATE TABLE preserved_history (value TEXT)")
        db.execute("INSERT INTO preserved_history VALUES ('keep-me')")

    accounts_path = tmp_path / "accounts.sqlite3"
    store = IdentityStore(accounts_path)

    with sqlite3.connect(odds_path) as db:
        assert db.execute("SELECT value FROM preserved_history").fetchone() == ("keep-me",)
    assert accounts_path.exists()
    assert accounts_path.stat().st_mode & 0o777 == 0o600
    with store.connect() as db:
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "identities" in tables
    assert "sessions" in tables


def test_identity_schema_initialization_is_idempotent(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    first = IdentityStore(path)
    first.initialize()
    with first.connect() as db:
        versions = db.execute("SELECT version FROM schema_migrations").fetchall()
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_schema_refuses_a_newer_database_version(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    store = IdentityStore(path)
    with store.connect() as db:
        db.execute("UPDATE schema_migrations SET version=99 WHERE version=(SELECT MAX(version) FROM schema_migrations)")
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()


def test_subscriber_registration_normalizes_email_and_never_assigns_admin(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("  Person@Example.COM ", "correct horse battery")
    with store.connect() as db:
        row = db.execute(
            "SELECT email, account_type, is_active, email_verified_at FROM identities WHERE id=?",
            (identity_id,),
        ).fetchone()
    assert row["email"] == "person@example.com"
    assert row["account_type"] == "subscriber"
    assert row["is_active"] == 1
    assert row["email_verified_at"] is None


@pytest.mark.parametrize("email", ["", "bad-address", "a@b", "person example.com", "x" * 255 + "@a.com"])
def test_registration_rejects_invalid_email(tmp_path, email):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError, match="email"):
        store.create_subscriber(email, "correct horse battery")


@pytest.mark.parametrize("password", ["short", "", "x" * 1025])
def test_registration_rejects_weak_or_oversized_password(tmp_path, password):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError, match="Password"):
        store.create_subscriber("person@example.com", password)


def test_email_uniqueness_is_case_insensitive(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.create_subscriber("Person@Example.com", "correct horse battery")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_subscriber("person@example.COM", "another secure password")


def test_authentication_is_scoped_to_account_type(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber("person@example.com", "correct horse battery")
    admin_id = store.provision_administrator("admin@example.com", "another secure password")

    assert store.authenticate("PERSON@example.com", "correct horse battery", "subscriber") is None
    _verify_subscriber(store, "person@example.com")
    assert store.authenticate("PERSON@example.com", "correct horse battery", "subscriber") == subscriber_id
    assert store.authenticate("person@example.com", "correct horse battery", "administrator") is None
    assert store.authenticate("admin@example.com", "another secure password", "subscriber") is None
    assert store.authenticate("admin@example.com", "another secure password", "administrator") == admin_id
    assert store.authenticate("admin@example.com", "wrong password", "administrator") is None


def test_session_token_is_opaque_hashed_revocable_and_expires(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    _verify_subscriber(store, "person@example.com")
    token, session_id = store.create_session(identity_id, timedelta(minutes=10))

    assert token
    assert session_id
    assert store.get_session(token)["identity_id"] == identity_id
    with store.connect() as db:
        stored = db.execute(
            "SELECT token_hash FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["token_hash"]
    assert stored != token

    assert store.revoke_session(token) is True
    assert store.revoke_session(token) is False
    assert store.get_session(token) is None


def test_session_creation_requires_active_identity_and_bounded_lifetime(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    with pytest.raises(ValueError, match="lifetime"):
        store.create_session(identity_id, timedelta(days=31))
    with pytest.raises(ValueError, match="active identity"):
        store.create_session("missing-identity")


def test_revoking_all_sessions_invalidates_each_token(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    _verify_subscriber(store, "person@example.com")
    first, _ = store.create_session(identity_id)
    second, _ = store.create_session(identity_id)
    assert store.revoke_all_sessions(identity_id) == 2
    assert store.get_session(first) is None
    assert store.get_session(second) is None


def test_idle_session_is_revoked_and_last_seen_is_refreshed(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    _verify_subscriber(store, "person@example.com")
    token, session_id = store.create_session(identity_id)
    with store.connect() as db:
        db.execute(
            "UPDATE sessions SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (session_id,),
        )
    assert store.get_session(token) is None
    with store.connect() as db:
        revoked = db.execute(
            "SELECT revoked_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["revoked_at"]
    assert revoked is not None


def test_active_session_updates_last_seen_at(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    _verify_subscriber(store, "person@example.com")
    token, session_id = store.create_session(identity_id)
    assert store.get_session(token) is not None
    with store.connect() as db:
        last_seen = db.execute(
            "SELECT last_seen_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["last_seen_at"]
    assert last_seen


def test_authentication_rejects_unknown_or_malformed_accounts(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.create_subscriber("known@example.com", "correct horse battery")
    assert store.authenticate("missing@example.com", "correct horse battery", "subscriber") is None
    assert store.authenticate("not-an-email", "correct horse battery", "subscriber") is None
    assert store.authenticate("known@example.com", "x" * 1025, "subscriber") is None



def test_email_verification_challenge_is_hashed_expiring_and_one_time(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    token = store.create_email_verification_challenge("PERSON@example.com")
    assert token
    with store.connect() as db:
        row = db.execute(
            "SELECT token_hash, consumed_at FROM email_verification_challenges"
        ).fetchone()
        identity = db.execute(
            "SELECT email_verified_at FROM identities WHERE id=?", (identity_id,)
        ).fetchone()
    assert row["token_hash"] != token
    assert row["consumed_at"] is None
    assert identity["email_verified_at"] is None
    assert store.consume_email_verification_challenge(token) is True
    assert store.consume_email_verification_challenge(token) is False
    with store.connect() as db:
        identity = db.execute(
            "SELECT email_verified_at FROM identities WHERE id=?", (identity_id,)
        ).fetchone()
    assert identity["email_verified_at"] is not None


def test_email_verification_challenge_rejects_expired_and_unknown_tokens(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.create_subscriber("person@example.com", "correct horse battery")
    token = store.create_email_verification_challenge("person@example.com")
    with store.connect() as db:
        db.execute(
            """UPDATE email_verification_challenges
               SET expires_at='2000-01-01T00:00:00+00:00' WHERE token_hash=?""",
            (__import__("hashlib").sha256(token.encode()).hexdigest(),),
        )
    assert store.consume_email_verification_challenge(token) is False
    assert store.consume_email_verification_challenge("unknown-token") is False


def test_password_reset_is_one_time_and_revokes_all_sessions(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
    _verify_subscriber(store, "person@example.com")
    first_token, _ = store.create_session(identity_id)
    second_token, _ = store.create_session(identity_id)
    reset_token = store.create_password_reset_challenge("person@example.com")
    assert reset_token
    with store.connect() as db:
        row = db.execute(
            "SELECT token_hash FROM password_reset_challenges"
        ).fetchone()
    assert row["token_hash"] != reset_token
    assert store.consume_password_reset_challenge(
        reset_token, "a completely new secure password"
    ) is True
    assert store.consume_password_reset_challenge(
        reset_token, "another completely new password"
    ) is False
    assert store.get_session(first_token) is None
    assert store.get_session(second_token) is None
    assert store.authenticate(
        "person@example.com", "a completely new secure password", "subscriber"
    ) == identity_id
    assert store.authenticate(
        "person@example.com", "correct horse battery", "subscriber"
    ) is None


def test_password_reset_unknown_email_is_non_disclosing_and_bad_password_rejected(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    assert store.create_password_reset_challenge("missing@example.com") is None
    store.create_subscriber("person@example.com", "correct horse battery")
    token = store.create_password_reset_challenge("person@example.com")
    assert token
    with pytest.raises(ValueError, match="Password"):
        store.consume_password_reset_challenge(token, "short")
    assert store.consume_password_reset_challenge(token, "valid new password here") is True


def test_new_challenge_invalidates_previous_outstanding_challenge(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.create_subscriber("person@example.com", "correct horse battery")
    first = store.create_password_reset_challenge("person@example.com")
    second = store.create_password_reset_challenge("person@example.com")
    assert first and second and first != second
    assert store.consume_password_reset_challenge(first, "new secure password value") is False
    assert store.consume_password_reset_challenge(second, "new secure password value") is True



def test_admin_totp_enrollment_encrypts_seed_and_issues_hashed_recovery_codes(tmp_path, monkeypatch):
    import pyotp
    from cryptography.fernet import Fernet
    from app import config
    from app import identity as identity_module

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator("admin@example.com", "administrator password")
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(uri)
    code = totp.now()
    recovery_codes = store.confirm_administrator_mfa_enrollment(admin_id, code)
    assert len(recovery_codes) == 10
    with pytest.raises(ValueError, match="already enrolled"):
        store.start_administrator_mfa_enrollment(admin_id)
    with store.connect() as db:
        row = db.execute(
            "SELECT encrypted_secret, enrolled_at, last_totp_counter FROM administrator_mfa WHERE identity_id=?",
            (admin_id,),
        ).fetchone()
        hashes = db.execute(
            "SELECT code_hash FROM administrator_mfa_recovery_codes WHERE identity_id=?",
            (admin_id,),
        ).fetchall()
    assert row["encrypted_secret"]
    assert totp.secret.encode() not in row["encrypted_secret"]
    assert row["enrolled_at"] is not None
    assert all(item["code_hash"] != code for item in hashes)
    assert store.verify_administrator_mfa(admin_id, code) is False  # enrollment step cannot replay
    assert store.consume_administrator_recovery_code(admin_id, recovery_codes[0]) is True
    assert store.consume_administrator_recovery_code(admin_id, recovery_codes[0]) is False


def test_admin_totp_accepts_new_step_once_and_rejects_subscriber(tmp_path, monkeypatch):
    from datetime import timedelta
    import pyotp
    from cryptography.fernet import Fernet
    from app import config
    from app import identity as identity_module

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator("admin@example.com", "administrator password")
    subscriber_id = store.create_subscriber("user@example.com", "subscriber password")
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(uri)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    next_time = identity_module._now() + timedelta(seconds=30)
    monkeypatch.setattr(identity_module, "_now", lambda: next_time)
    next_code = totp.at(next_time)
    assert store.verify_administrator_mfa(admin_id, next_code) is True
    assert store.verify_administrator_mfa(admin_id, next_code) is False
    assert store.verify_administrator_mfa(subscriber_id, next_code) is False
    session_token, _ = store.create_session(admin_id)
    assert store.get_session(session_token)["account_type"] == "administrator"
    with store.connect() as db:
        mfa_events = db.execute(
            """SELECT event_type, actor_identity_id, subject_identity_id
               FROM security_audit_events
               WHERE subject_identity_id=? AND event_type LIKE 'administrator.mfa_%'""",
            (admin_id,),
        ).fetchall()
    assert {row["event_type"] for row in mfa_events} == {
        "administrator.mfa_enrolled", "administrator.mfa_verified"
    }
    assert all(row["actor_identity_id"] == admin_id for row in mfa_events)
    assert all(row["subject_identity_id"] == admin_id for row in mfa_events)
    with pytest.raises(ValueError, match="MFA verification"):
        store.create_session(admin_id)


def test_admin_mfa_fails_closed_without_encryption_key(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", "")
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator("admin@example.com", "administrator password")
    with pytest.raises(RuntimeError, match="MFA_ENCRYPTION_KEY"):
        store.start_administrator_mfa_enrollment(admin_id)



def test_initial_administrator_bootstrap_is_private_and_one_time(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_initial_administrator(
        "first-admin@example.com", "initial administrator password"
    )
    with store.connect() as db:
        row = db.execute(
            "SELECT account_type FROM identities WHERE id=?", (admin_id,)
        ).fetchone()
        events = db.execute(
            "SELECT event_type FROM security_audit_events WHERE subject_identity_id=?",
            (admin_id,),
        ).fetchall()
    assert row["account_type"] == "administrator"
    assert [event["event_type"] for event in events] == ["administrator.initially_provisioned"]
    with pytest.raises(ValueError, match="already exists"):
        store.provision_initial_administrator(
            "second-admin@example.com", "another administrator password"
        )
    assert store.authenticate(
        "second-admin@example.com", "another administrator password", "administrator"
    ) is None


def test_initial_administrator_bootstrap_rejects_invalid_email_without_partial_record(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError, match="email"):
        store.provision_initial_administrator("not-an-email", "initial administrator password")
    with store.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) AS count FROM identities WHERE account_type='administrator'"
        ).fetchone()["count"]
    assert count == 0



def test_administrator_cannot_create_session_without_mfa_proof(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator("admin@example.com", "administrator password")
    with pytest.raises(ValueError, match="MFA verification"):
        store.create_session(admin_id)


def test_recovery_code_provides_one_time_admin_session_proof(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from app import config
    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    admin_id = store.provision_administrator("admin@example.com", "administrator password")
    uri = store.start_administrator_mfa_enrollment(admin_id)
    import pyotp
    totp = pyotp.parse_uri(uri)
    codes = store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    assert store.consume_administrator_recovery_code(admin_id, codes[0]) is True
    with store.connect() as db:
        recovery_event = db.execute(
            """SELECT actor_identity_id, subject_identity_id
               FROM security_audit_events
               WHERE event_type='administrator.mfa_recovery_used'"""
        ).fetchone()
    assert recovery_event["actor_identity_id"] == admin_id
    assert recovery_event["subject_identity_id"] == admin_id
    token, _ = store.create_session(admin_id)
    assert store.get_session(token)["account_type"] == "administrator"
    with pytest.raises(ValueError, match="MFA verification"):
        store.create_session(admin_id)



def test_deactivated_identity_invalidates_existing_session(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("inactive@example.com", "a secure password phrase")
    _verify_subscriber(store, "inactive@example.com")
    token, _session_id = store.create_session(identity_id)

    assert store.get_session(token)["identity_id"] == identity_id
    with store.connect() as db:
        db.execute("UPDATE identities SET is_active=0 WHERE id=?", (identity_id,))

    assert store.get_session(token) is None



@pytest.mark.parametrize("preexisting", [False, True])
def test_identity_database_permissions_are_restricted_before_sqlite_opens(
    tmp_path, monkeypatch, preexisting
):
    from app import identity as identity_module

    path = tmp_path / "accounts.sqlite3"
    if preexisting:
        path.write_bytes(b"")
        path.chmod(0o644)

    real_connect = sqlite3.connect
    observed_modes = []

    def inspect_connect(database, *args, **kwargs):
        if str(database) == str(path):
            observed_modes.append(path.stat().st_mode & 0o777)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(identity_module.sqlite3, "connect", inspect_connect)
    IdentityStore(path)

    assert observed_modes
    assert observed_modes[0] == 0o600
    assert path.stat().st_mode & 0o777 == 0o600



def test_identity_database_rejects_fifo_without_blocking(tmp_path):
    import os

    path = tmp_path / "accounts.sqlite3"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="regular file"):
        IdentityStore(path)


def test_identity_database_rejects_symlink_path(tmp_path):
    target = tmp_path / "real.sqlite3"
    target.write_bytes(b"")
    link = tmp_path / "accounts.sqlite3"
    link.symlink_to(target)

    with pytest.raises(OSError):
        IdentityStore(link)


def test_identity_database_rejects_directory_path(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    path.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        IdentityStore(path)



def test_unverified_subscriber_cannot_authenticate_or_create_session(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "pending@example.com", "a-strong-pending-password"
    )

    assert store.authenticate(
        "pending@example.com", "a-strong-pending-password", "subscriber"
    ) is None
    with pytest.raises(ValueError, match="verified subscriber email"):
        store.create_session(identity_id)

    _verify_subscriber(store, "pending@example.com")
    assert store.authenticate(
        "pending@example.com", "a-strong-pending-password", "subscriber"
    ) == identity_id
    token, _ = store.create_session(identity_id)
    assert store.get_session(token)["identity_id"] == identity_id



def test_subscriber_session_is_rejected_if_email_verification_is_revoked(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "verified@example.com", "a-strong-verified-password"
    )
    _verify_subscriber(store, "verified@example.com")
    token, _ = store.create_session(identity_id)
    assert store.get_session(token) is not None

    with store.connect() as db:
        db.execute(
            "UPDATE identities SET email_verified_at=NULL WHERE id=?",
            (identity_id,),
        )

    assert store.get_session(token) is None



def test_identity_and_session_audit_events_exclude_credentials_and_tokens(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "audit@example.com", "a-strong-audit-password"
    )
    _verify_subscriber(store, "audit@example.com")
    token, _ = store.create_session(identity_id)
    assert store.revoke_session(token) is True

    with store.connect() as db:
        events = db.execute(
            """SELECT event_type, actor_identity_id, metadata_json
               FROM security_audit_events WHERE subject_identity_id=? ORDER BY rowid""",
            (identity_id,),
        ).fetchall()

    assert [row["event_type"] for row in events] == [
        "subscriber.registered",
        "subscriber.email_verified",
        "subscriber.session_created",
        "identity.session_revoked",
    ]
    assert [row["actor_identity_id"] for row in events] == [
        identity_id, None, identity_id, identity_id
    ]
    for row in events:
        assert token not in row["metadata_json"]
        assert "audit@example.com" not in row["metadata_json"]
        assert row["metadata_json"] == "{}"


def test_revoke_all_sessions_audit_records_only_revocation_count(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "bulk-audit@example.com", "a-strong-bulk-audit-password"
    )
    _verify_subscriber(store, "bulk-audit@example.com")
    store.create_session(identity_id)
    store.create_session(identity_id)

    assert store.revoke_all_sessions(identity_id) == 2
    with store.connect() as db:
        row = db.execute(
            """SELECT event_type, actor_identity_id, metadata_json
               FROM security_audit_events
               WHERE subject_identity_id=? AND event_type='identity.sessions_revoked'""",
            (identity_id,),
        ).fetchone()

    assert row["event_type"] == "identity.sessions_revoked"
    assert row["actor_identity_id"] == identity_id
    assert row["metadata_json"] == '{"revoked_count":2}'



def test_cross_account_session_revocation_requires_active_administrator_actor(
    tmp_path, monkeypatch
):
    import pyotp
    from cryptography.fernet import Fernet
    from app import config

    monkeypatch.setattr(config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subject_id = store.create_subscriber(
        "subject@example.com", "a-strong-subject-password"
    )
    _verify_subscriber(store, "subject@example.com")
    token, _ = store.create_session(subject_id)
    other_subscriber_id = store.create_subscriber(
        "other@example.com", "a-strong-other-password"
    )

    with pytest.raises(ValueError, match="administrator is required"):
        store.revoke_session(token, actor_identity_id=other_subscriber_id)
    assert store.get_session(token) is not None

    admin_id = store.provision_administrator(
        "audit-admin@example.com", "a-strong-audit-admin-password"
    )
    with pytest.raises(ValueError, match="MFA-enrolled administrator"):
        store.revoke_session(token, actor_identity_id=admin_id)
    assert store.get_session(token) is not None
    provisioning_uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(provisioning_uri)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    with store.connect() as db:
        db.execute("UPDATE identities SET is_active=0 WHERE id=?", (admin_id,))
    with pytest.raises(ValueError, match="MFA-enrolled administrator"):
        store.revoke_session(token, actor_identity_id=admin_id)
    assert store.get_session(token) is not None
    with store.connect() as db:
        db.execute("UPDATE identities SET is_active=1 WHERE id=?", (admin_id,))
    assert store.revoke_session(token, actor_identity_id=admin_id) is True
    with store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, subject_identity_id
               FROM security_audit_events
               WHERE event_type='identity.session_revoked'""",
        ).fetchone()

    assert event["actor_identity_id"] == admin_id
    assert event["subject_identity_id"] == subject_id

    second_token, _ = store.create_session(subject_id)
    with pytest.raises(ValueError, match="MFA-enrolled administrator"):
        store.revoke_all_sessions(
            subject_id, actor_identity_id=other_subscriber_id
        )
    assert store.get_session(second_token) is not None
    assert store.revoke_all_sessions(subject_id, actor_identity_id=admin_id) == 1
    assert store.get_session(second_token) is None

    filtered = store.list_security_audit_events(
        event_type="identity.session_revoked",
        actor_identity_id=admin_id,
        subject_identity_id=subject_id,
    )
    assert filtered["total"] == 1
    assert filtered["events"][0]["actor_identity_id"] == admin_id



def test_audit_query_is_paginated_filtered_and_secret_free(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "audit-query@example.com", "a-strong-audit-query-password"
    )
    _verify_subscriber(store, "audit-query@example.com")
    token, _ = store.create_session(identity_id)
    store.revoke_session(token)

    page = store.list_security_audit_events(limit=2, offset=1)
    assert page["total"] == 4
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert len(page["events"]) == 2
    assert all("metadata" in event for event in page["events"])
    assert all("audit-query@example.com" not in str(event) for event in page["events"])
    assert all(token not in str(event) for event in page["events"])

    filtered = store.list_security_audit_events(
        event_type="subscriber.session_created",
        actor_identity_id=identity_id,
        subject_identity_id=identity_id,
    )
    assert filtered["total"] == 1
    assert filtered["events"][0]["event_type"] == "subscriber.session_created"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"offset": -1},
        {"offset": 10_000_001},
        {"offset": True},
        {"event_type": ""},
        {"actor_identity_id": "x" * 129},
    ],
)
def test_audit_query_rejects_unbounded_or_invalid_parameters(tmp_path, kwargs):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError):
        store.list_security_audit_events(**kwargs)



@pytest.mark.parametrize("metadata_json", ["not-json", "[]", "null"])
def test_audit_query_fails_closed_on_invalid_metadata(tmp_path, metadata_json):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber(
        "bad-audit@example.com", "a-strong-bad-audit-password"
    )
    with store.connect() as db:
        db.execute(
            """INSERT INTO security_audit_events(
                id, event_type, subject_identity_id, occurred_at, metadata_json
            ) VALUES (?, 'test.corrupt_metadata', ?, ?, ?)""",
            ("corrupt-audit-event", identity_id, "2026-11-24T00:00:00+00:00", metadata_json),
        )

    with pytest.raises(RuntimeError, match="Audit event metadata is invalid"):
        store.list_security_audit_events(event_type="test.corrupt_metadata")



def test_email_delivery_history_is_secret_free_and_provider_success_resets_circuit(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("private@example.com", "a strong password 123")

    store.record_email_delivery_event(
        identity_id, "verification", "failed", failure_code="transport"
    )
    store.record_email_delivery_event(
        identity_id, "verification", "sent", provider_message_id="email_provider_123"
    )
    assert store.email_provider_circuit_open() is False

    with store.connect() as db:
        event = db.execute(
            "SELECT provider, identity_id, purpose, status, provider_message_id, failure_code "
            "FROM email_delivery_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        circuit = db.execute(
            "SELECT consecutive_failures, opened_until, last_success_at "
            "FROM email_provider_circuit WHERE provider='resend'"
        ).fetchone()
        raw = " ".join(
            str(value) for value in db.execute(
                "SELECT * FROM email_delivery_events"
            ).fetchone()
        )
    assert event["status"] == "sent"
    assert event["provider_message_id"] == "email_provider_123"
    assert circuit["consecutive_failures"] == 0
    assert circuit["opened_until"] is None
    assert circuit["last_success_at"] is not None
    assert "private@example.com" not in raw


def test_email_provider_circuit_opens_after_five_consecutive_failures(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "a strong password 123")

    for _ in range(5):
        store.record_email_delivery_event(
            identity_id, "password_reset", "failed",
            failure_code="provider_unavailable",
        )
    assert store.email_provider_circuit_open() is True

    store.record_email_delivery_event(
        identity_id, "password_reset", "skipped", failure_code="circuit_open"
    )
    with store.connect() as db:
        row = db.execute(
            "SELECT consecutive_failures, opened_until FROM email_provider_circuit "
            "WHERE provider='resend'"
        ).fetchone()
        skipped = db.execute(
            "SELECT status, failure_code FROM email_delivery_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row["consecutive_failures"] == 5
    assert row["opened_until"] is not None
    assert skipped["status"] == "skipped"
    assert skipped["failure_code"] == "circuit_open"


def test_email_delivery_event_rejects_unreviewed_failure_metadata(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "a strong password 123")
    with pytest.raises(ValueError, match="failure code"):
        store.record_email_delivery_event(
            identity_id, "verification", "failed",
            failure_code="provider said private token is abc",
        )
    with pytest.raises(ValueError, match="Unsupported email"):
        store.record_email_delivery_event(
            identity_id, "verification", "sent",
            provider="unreviewed-provider",
            provider_message_id="email-id",
        )



def test_email_provider_half_open_probe_is_exclusive_across_workers(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import identity as identity_module

    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "a strong password 123")
    for _ in range(5):
        store.record_email_delivery_event(
            identity_id, "verification", "failed",
            failure_code="provider_unavailable",
        )

    assert store.claim_email_provider_send() is False
    now[0] += timedelta(minutes=11)
    assert store.claim_email_provider_send() is True
    # Only one concurrent worker may hold the half-open probe lease.
    assert store.claim_email_provider_send() is False

    now[0] += timedelta(minutes=1, seconds=1)
    assert store.claim_email_provider_send() is True



def _mfa_admin(store, monkeypatch, email="entitlement-admin@example.com"):
    import pyotp
    from cryptography.fernet import Fernet
    from app import config

    monkeypatch.setattr(
        config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    admin_id = store.provision_administrator(email, "an entitlement admin password")
    uri = store.start_administrator_mfa_enrollment(admin_id)
    totp = pyotp.parse_uri(uri)
    store.confirm_administrator_mfa_enrollment(admin_id, totp.now())
    return admin_id



def test_subscriber_suspension_atomically_revokes_sessions_and_preserves_account_data(
    tmp_path, monkeypatch
):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "lifecycle@example.test", "a strong lifecycle password 123"
    )
    _verify_subscriber(store, "lifecycle@example.test")
    admin_id = _mfa_admin(store, monkeypatch, "lifecycle-admin@example.test")
    store.set_subscriber_entitlements(
        subscriber_id, ["dashboard", "odds"], actor_identity_id=admin_id,
        reason_code="support_request",
    )
    first_token, _ = store.create_session(subscriber_id)
    second_token, _ = store.create_session(subscriber_id)

    suspended = store.set_subscriber_status(
        subscriber_id, is_active=False, actor_identity_id=admin_id,
        reason_code="support_request",
    )
    assert suspended == {
        "identity_id": subscriber_id, "is_active": False,
        "revoked_sessions": 2, "changed": True,
    }
    assert store.get_session(first_token) is None
    assert store.get_session(second_token) is None
    assert store.active_subscriber_entitlements(subscriber_id) == []

    with store.connect() as db:
        row = db.execute(
            "SELECT is_active, email_verified_at FROM identities WHERE id=?",
            (subscriber_id,),
        ).fetchone()
        event = db.execute(
            """SELECT actor_identity_id, event_type, metadata_json
               FROM security_audit_events
               WHERE subject_identity_id=? AND event_type='subscriber.suspended'""",
            (subscriber_id,),
        ).fetchone()
    assert row["is_active"] == 0
    assert row["email_verified_at"] is not None
    assert event["actor_identity_id"] == admin_id
    assert event["event_type"] == "subscriber.suspended"
    assert '"revoked_sessions":2' in event["metadata_json"]
    assert "lifecycle@example.test" not in event["metadata_json"]

    reactivated = store.set_subscriber_status(
        subscriber_id, is_active=True, actor_identity_id=admin_id,
        reason_code="support_request",
    )
    assert reactivated["changed"] is True
    assert reactivated["is_active"] is True
    assert reactivated["revoked_sessions"] == 0
    assert store.active_subscriber_entitlements(subscriber_id) == ["dashboard", "odds"]
    with store.connect() as db:
        row = db.execute(
            "SELECT is_active, email_verified_at FROM identities WHERE id=?",
            (subscriber_id,),
        ).fetchone()
    assert row["is_active"] == 1
    assert row["email_verified_at"] is not None


def test_subscriber_status_change_rejects_unauthorized_actor_and_invalid_reason(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "lifecycle-deny@example.test", "a strong lifecycle deny password"
    )
    other_subscriber = store.create_subscriber(
        "lifecycle-other@example.test", "a strong lifecycle other password"
    )
    with pytest.raises(ValueError, match="MFA-enrolled administrator"):
        store.set_subscriber_status(
            subscriber_id, is_active=False, actor_identity_id=other_subscriber,
            reason_code="support_request",
        )
    with pytest.raises(ValueError, match="reason code"):
        store.set_subscriber_status(
            subscriber_id, is_active=False, actor_identity_id="some-admin",
            reason_code="not_a_valid_code",
        )
    with store.connect() as db:
        row = db.execute("SELECT is_active FROM identities WHERE id=?", (subscriber_id,)).fetchone()
    assert row["is_active"] == 1


def test_subscriber_entitlements_default_deny_and_require_mfa_admin_actor(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "entitled@example.com", "a strong subscriber password"
    )
    assert store.active_subscriber_entitlements(subscriber_id) == []
    assert store.has_subscriber_entitlement(subscriber_id, "odds") is False

    other_subscriber = store.create_subscriber(
        "other-entitled@example.com", "another strong subscriber password"
    )
    with pytest.raises(ValueError, match="MFA-enrolled administrator"):
        store.set_subscriber_entitlements(
            subscriber_id, ["odds"], actor_identity_id=other_subscriber, reason_code="support_request"
        )

    admin_id = _mfa_admin(store, monkeypatch)
    grants = store.set_subscriber_entitlements(
        subscriber_id, ["odds", "dashboard"], actor_identity_id=admin_id, reason_code="support_request"
    )
    assert grants == ["dashboard", "odds"]
    assert store.active_subscriber_entitlements(subscriber_id) == grants
    assert store.has_subscriber_entitlement(subscriber_id, "odds") is True
    assert store.has_subscriber_entitlement(subscriber_id, "arbitrage") is False

    with store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, event_type, metadata_json
               FROM security_audit_events
               WHERE subject_identity_id=? AND event_type='subscriber.entitlements_updated'
               ORDER BY occurred_at DESC LIMIT 1""",
            (subscriber_id,),
        ).fetchone()
    assert event["actor_identity_id"] == admin_id
    assert event["event_type"] == "subscriber.entitlements_updated"
    assert '"dashboard"' in event["metadata_json"]
    assert '"reason_code":"support_request"' in event["metadata_json"]
    assert "entitled@example.com" not in event["metadata_json"]


def test_subscriber_entitlement_expiry_and_replacement_are_enforced(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import identity as identity_module
    import pyotp

    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "expiry@example.com", "a strong expiry password"
    )
    admin_id = _mfa_admin(store, monkeypatch, "expiry-admin@example.com")
    local_expiry = (now[0] + timedelta(hours=1)).astimezone(
        timezone(timedelta(hours=-8))
    )
    store.set_subscriber_entitlements(
        subscriber_id, ["odds"], actor_identity_id=admin_id,
        reason_code="trial_grant", expires_at=local_expiry,
    )
    assert store.has_subscriber_entitlement(subscriber_id, "odds") is True

    now[0] += timedelta(hours=2)
    assert store.has_subscriber_entitlement(subscriber_id, "odds") is False
    assert store.active_subscriber_entitlements(subscriber_id) == []

    store.set_subscriber_entitlements(
        subscriber_id, ["dashboard"], actor_identity_id=admin_id, reason_code="support_request"
    )
    assert store.has_subscriber_entitlement(subscriber_id, "odds") is False
    assert store.has_subscriber_entitlement(subscriber_id, "dashboard") is True


@pytest.mark.parametrize(
    "keys",
    [
        ["unknown-feature"],
        ["odds", "x" * 300],
    ],
)
def test_subscriber_entitlement_updates_reject_unapproved_feature_keys(tmp_path, monkeypatch, keys):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "invalid-entitlement@example.com", "a strong invalid entitlement password"
    )
    admin_id = _mfa_admin(store, monkeypatch, "invalid-entitlement-admin@example.com")
    with pytest.raises(ValueError, match="entitlement"):
        store.set_subscriber_entitlements(
            subscriber_id, keys, actor_identity_id=admin_id, reason_code="support_request"
        )


def test_subscriber_entitlement_updates_reject_unapproved_reason_codes(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "reason-code@example.test", "a strong reason code password"
    )
    admin_id = _mfa_admin(store, monkeypatch, "reason-code-admin@example.test")
    with pytest.raises(ValueError, match="reason code"):
        store.set_subscriber_entitlements(
            subscriber_id, ["dashboard"], actor_identity_id=admin_id,
            reason_code="unapproved",
        )


def test_subscriber_directory_storage_is_bounded_searchable_and_minimal(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "directory-user@example.com", "a strong directory password 123"
    )
    store.create_subscriber(
        "another-directory@example.com", "another directory password 123"
    )
    admin_id = _mfa_admin(store, monkeypatch, "directory-admin@example.com")
    store.set_subscriber_entitlements(
        subscriber_id, ["odds", "dashboard"], actor_identity_id=admin_id, reason_code="support_request"
    )

    result = store.list_subscribers(limit=1, offset=0)
    assert result["total"] == 2
    assert len(result["subscribers"]) == 1
    page = store.list_subscribers(limit=100)
    record = next(row for row in page["subscribers"] if row["identity_id"] == subscriber_id)
    assert record["email"] == "directory-user@example.com"
    assert record["is_active"] is True
    assert record["email_verified"] is False
    assert record["entitlements"] == ["dashboard", "odds"]
    assert "password_hash" not in record
    assert "session" not in record

    filtered = store.list_subscribers(email_query="DIRECTORY-user")
    assert filtered["total"] == 1
    assert filtered["subscribers"][0]["identity_id"] == subscriber_id
    assert store.list_subscribers(email_query="%")["total"] == 0
    with pytest.raises(ValueError, match="page size"):
        store.list_subscribers(limit=0)
    with pytest.raises(ValueError, match="search"):
        store.list_subscribers(email_query=" ")




def test_administrator_session_directory_and_revocation_are_bounded_and_audited(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "session-target@example.test", "a strong session target password"
    )
    _verify_subscriber(store, "session-target@example.test")
    subscriber_token, subscriber_session_id = store.create_session(subscriber_id)
    admin_id = _mfa_admin(store, monkeypatch, "session-admin@example.test")
    current_session_id = "current-admin-session-reference"
    listing = store.list_active_sessions(
        actor_identity_id=admin_id, current_session_id=current_session_id, limit=10
    )
    target = next(row for row in listing["sessions"] if row["session_id"] == subscriber_session_id)
    assert target["identity_id"] == subscriber_id
    assert target["account_type"] == "subscriber"
    assert "token_hash" not in target and "email" not in target
    assert "session-target@example.test" not in str(target)
    with pytest.raises(ValueError, match="current administrator session"):
        store.revoke_session_by_id(
            current_session_id, actor_identity_id=admin_id,
            current_session_id=current_session_id, reason_code="security_review",
        )
    with pytest.raises(ValueError, match="reason code"):
        store.revoke_session_by_id(
            subscriber_session_id, actor_identity_id=admin_id,
            current_session_id=current_session_id, reason_code="free text",
        )
    assert store.revoke_session_by_id(
        subscriber_session_id, actor_identity_id=admin_id,
        current_session_id=current_session_id, reason_code="security_review",
    ) is True
    assert store.get_session(subscriber_token) is None
    assert store.revoke_session_by_id(
        subscriber_session_id, actor_identity_id=admin_id,
        current_session_id=current_session_id, reason_code="security_review",
    ) is False
    with store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, event_type, metadata_json
               FROM security_audit_events WHERE subject_identity_id=?
               AND event_type='administrator.session_revoked'""", (subscriber_id,)
        ).fetchone()
    assert event["actor_identity_id"] == admin_id
    assert event["event_type"] == "administrator.session_revoked"
    assert '"reason_code":"security_review"' in event["metadata_json"]
    assert "session-target@example.test" not in event["metadata_json"]


def test_administrator_can_revoke_all_subscriber_sessions_atomically_and_audit(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber("bulk-sessions@example.test", "a strong bulk session password")
    _verify_subscriber(store, "bulk-sessions@example.test")
    tokens = [store.create_session(subscriber_id) for _ in range(3)]
    admin_id = _mfa_admin(store, monkeypatch, "bulk-session-admin@example.test")
    with pytest.raises(ValueError, match="administrator actor"):
        store.revoke_subscriber_sessions(admin_id, actor_identity_id=admin_id, reason_code="security_review")
    with pytest.raises(ValueError, match="reason code"):
        store.revoke_subscriber_sessions(subscriber_id, actor_identity_id=admin_id, reason_code="free text")
    count = store.revoke_subscriber_sessions(
        subscriber_id, actor_identity_id=admin_id, reason_code="security_review"
    )
    assert count == 3
    assert all(store.get_session(token) is None for token, _session_id in tokens)
    with store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, event_type, metadata_json FROM security_audit_events
               WHERE subject_identity_id=? AND event_type='administrator.subscriber_sessions_revoked'""",
            (subscriber_id,),
        ).fetchone()
    assert event["actor_identity_id"] == admin_id
    assert '"revoked_count":3' in event["metadata_json"]
    assert '"reason_code":"security_review"' in event["metadata_json"]
    assert "bulk-sessions@example.test" not in event["metadata_json"]
    assert store.revoke_subscriber_sessions(
        subscriber_id, actor_identity_id=admin_id, reason_code="other"
    ) == 0


def test_subscriber_password_change_verifies_current_password_and_revokes_all_sessions(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("password-change@example.test", "correct horse battery")
    _verify_subscriber(store, "password-change@example.test")
    first_token, _ = store.create_session(identity_id)
    second_token, _ = store.create_session(identity_id)
    reset_token = store.create_password_reset_challenge("password-change@example.test")
    assert reset_token

    assert store.change_subscriber_password(
        identity_id, "incorrect current password", "a completely new secure password"
    ) is False
    assert store.get_session(first_token) is not None
    assert store.get_session(second_token) is not None

    assert store.change_subscriber_password(
        identity_id, "correct horse battery", "a completely new secure password"
    ) is True
    assert store.get_session(first_token) is None
    assert store.get_session(second_token) is None
    assert store.authenticate(
        "password-change@example.test", "correct horse battery", "subscriber"
    ) is None
    assert store.authenticate(
        "password-change@example.test", "a completely new secure password", "subscriber"
    ) == identity_id
    assert store.consume_password_reset_challenge(
        reset_token, "another entirely new secure password"
    ) is False
    assert store.authenticate(
        "password-change@example.test", "a completely new secure password", "subscriber"
    ) == identity_id

    with store.connect() as db:
        event = db.execute(
            """SELECT actor_identity_id, metadata_json FROM security_audit_events
               WHERE event_type='subscriber.password_changed'
               AND subject_identity_id=?""",
            (identity_id,),
        ).fetchone()
        changed_at = db.execute(
            "SELECT password_changed_at FROM identities WHERE id=?", (identity_id,)
        ).fetchone()["password_changed_at"]
    assert event["actor_identity_id"] == identity_id
    assert event["metadata_json"] == "{}"
    assert changed_at


def test_subscriber_password_change_rejects_reusing_current_password(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("same-password@example.test", "correct horse battery")
    _verify_subscriber(store, "same-password@example.test")
    with pytest.raises(ValueError, match="different from the current password"):
        store.change_subscriber_password(
            identity_id, "correct horse battery", "correct horse battery"
        )


def test_billing_schema_is_additive_idempotent_and_stores_no_payment_card_data(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "billing-schema@example.test", "a strong billing schema password"
    )
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at) "
            "VALUES (?, ?, 'now', 'now')",
            (subscriber_id, "cus_test_123"),
        )
        db.execute(
            """INSERT INTO billing_subscriptions(
                id, identity_id, stripe_subscription_id, stripe_customer_id,
                stripe_price_id, plan_code, billing_interval, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now', 'now')""",
            ("local-sub-1", subscriber_id, "sub_test_123", "cus_test_123",
             "price_configured_later", None, "month", "incomplete"),
        )
        db.execute(
            """INSERT INTO billing_webhook_events(
                stripe_event_id, event_type, livemode, payload_sha256, received_at
            ) VALUES (?, ?, ?, ?, ?)""",
            ("evt_test_123", "customer.subscription.created", 0, "a" * 64, "now"),
        )
        db.commit()
        columns = {
            row["name"] for row in db.execute(
                "PRAGMA table_info(billing_subscriptions)"
            ).fetchall()
        }
        assert not columns.intersection({
            "card_number", "pan", "cvc", "cvv", "payment_method_details",
            "card_fingerprint",
        })
        assert db.execute(
            "SELECT status FROM billing_subscriptions WHERE id='local-sub-1'"
        ).fetchone()["status"] == "incomplete"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO billing_webhook_events(
                    stripe_event_id, event_type, livemode, payload_sha256,
                    processing_state, attempt_count, received_at, processed_at,
                    last_error_code, lease_token_hash, lease_expires_at, updated_at
                ) VALUES ('evt_test_123', 'customer.subscription.created', 0, ?,
                          'received', 0, 'now', NULL, NULL, NULL, NULL, 'now')""",
                ("b" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO billing_subscriptions(
                    id, identity_id, stripe_subscription_id, stripe_customer_id,
                    status, created_at, updated_at
                ) VALUES ('bad-status', ?, 'sub_bad', 'cus_test_123', 'unknown', 'now', 'now')""",
                (subscriber_id,),
            )
    store.initialize()
    with store.connect() as db:
        assert db.execute(
            "SELECT stripe_event_id FROM billing_webhook_events"
        ).fetchone()["stripe_event_id"] == "evt_test_123"
        assert db.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=9"
        ).fetchone()[0] == 1


def test_billing_schema_foreign_keys_prevent_orphan_customer_and_subscription(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    subscriber_id = store.create_subscriber(
        "billing-orphan@example.test", "a strong billing orphan password"
    )
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO billing_customers(identity_id, stripe_customer_id, created_at, updated_at) "
                "VALUES (?, 'cus_orphan', 'now', 'now')",
                ("missing-identity",),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO billing_subscriptions(
                    id, identity_id, stripe_subscription_id, stripe_customer_id,
                    status, created_at, updated_at
                ) VALUES ('orphan', ?, 'sub_orphan', 'cus_missing', 'active', 'now', 'now')""",
                (subscriber_id,),
            )


def test_stripe_webhook_event_ingestion_is_idempotent_and_payload_fingerprinted(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    payload = b'{"id":"evt_one","type":"invoice.paid"}'
    first = store.record_stripe_webhook_event(
        "evt_one", "invoice.paid", False, payload
    )
    duplicate = store.record_stripe_webhook_event(
        "evt_one", "invoice.paid", False, payload
    )
    assert first == {
        "created": True, "processing_state": "received", "attempt_count": 0
    }
    assert duplicate == {
        "created": False, "processing_state": "received", "attempt_count": 0
    }
    with store.connect() as db:
        row = db.execute(
            "SELECT payload_sha256 FROM billing_webhook_events WHERE stripe_event_id='evt_one'"
        ).fetchone()
    assert row["payload_sha256"] == __import__("hashlib").sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="reused with different"):
        store.record_stripe_webhook_event(
            "evt_one", "invoice.paid", False, b'{"different":true}'
        )
    with pytest.raises(ValueError, match="reused with different"):
        store.record_stripe_webhook_event(
            "evt_one", "invoice.paid", True, payload
        )


def test_stripe_webhook_leases_prevent_double_processing_and_recover_expired_worker(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.record_stripe_webhook_event(
        "evt_lease", "customer.subscription.updated", False, b'{"id":"evt_lease"}'
    )
    first = store.claim_stripe_webhook_event("evt_lease", lease_seconds=60)
    assert first
    assert store.claim_stripe_webhook_event("evt_lease") is None
    assert store.complete_stripe_webhook_event("evt_lease", "wrong-token") is False
    with store.connect() as db:
        db.execute(
            "UPDATE billing_webhook_events SET lease_expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE stripe_event_id='evt_lease'"
        )
    second = store.claim_stripe_webhook_event("evt_lease", lease_seconds=60)
    assert second and second != first
    assert store.complete_stripe_webhook_event("evt_lease", first) is False
    assert store.complete_stripe_webhook_event("evt_lease", second) is True
    assert store.claim_stripe_webhook_event("evt_lease") is None
    with store.connect() as db:
        row = db.execute(
            "SELECT processing_state, attempt_count, lease_token_hash, processed_at "
            "FROM billing_webhook_events WHERE stripe_event_id='evt_lease'"
        ).fetchone()
    assert row["processing_state"] == "processed"
    assert row["attempt_count"] == 2
    assert row["lease_token_hash"] is None
    assert row["processed_at"] is not None


def test_stripe_webhook_failure_releases_lease_and_sanitizes_error_code(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    store.record_stripe_webhook_event(
        "evt_failure", "invoice.payment_failed", False, b'{"id":"evt_failure"}'
    )
    token = store.claim_stripe_webhook_event("evt_failure")
    assert token
    with pytest.raises(ValueError, match="failure code"):
        store.fail_stripe_webhook_event("evt_failure", token, "raw provider error with PII")
    assert store.fail_stripe_webhook_event(
        "evt_failure", token, "temporary_provider_error"
    ) is True
    retry = store.claim_stripe_webhook_event("evt_failure")
    assert retry and retry != token
    assert store.fail_stripe_webhook_event(
        "evt_failure", retry, "another_error"
    ) is True
    with store.connect() as db:
        row = db.execute(
            "SELECT processing_state, attempt_count, last_error_code, lease_token_hash "
            "FROM billing_webhook_events WHERE stripe_event_id='evt_failure'"
        ).fetchone()
    assert row["processing_state"] == "failed"
    assert row["attempt_count"] == 2
    assert row["last_error_code"] == "another_error"
    assert row["lease_token_hash"] is None


def test_stripe_webhook_event_input_validation_is_bounded(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(ValueError, match="event ID"):
        store.record_stripe_webhook_event("", "invoice.paid", False, b"x")
    with pytest.raises(ValueError, match="event type"):
        store.record_stripe_webhook_event("evt_bad", "", False, b"x")
    with pytest.raises(ValueError, match="Boolean"):
        store.record_stripe_webhook_event("evt_bad", "invoice.paid", 0, b"x")
    with pytest.raises(ValueError, match="1 MiB"):
        store.record_stripe_webhook_event("evt_bad", "invoice.paid", False, b"x" * 1_048_577)
    with pytest.raises(ValueError, match="lease"):
        store.claim_stripe_webhook_event("evt_bad", lease_seconds=1)
    with pytest.raises(ValueError, match="attempts"):
        store.claim_stripe_webhook_event("evt_bad", max_attempts=99)
