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
    assert [row["version"] for row in versions] == [1, 2, 3]


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
    token, _ = store.create_session(admin_id)
    assert store.get_session(token)["account_type"] == "administrator"
    with pytest.raises(ValueError, match="MFA verification"):
        store.create_session(admin_id)



def test_deactivated_identity_invalidates_existing_session(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("inactive@example.com", "a secure password phrase")
    _verify_subscriber(store, "person@example.com")
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
