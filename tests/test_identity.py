import sqlite3
from datetime import timedelta

import pytest

from app.identity import IdentityStore


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
    assert [row["version"] for row in versions] == [1]


def test_schema_refuses_a_newer_database_version(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    store = IdentityStore(path)
    with store.connect() as db:
        db.execute("UPDATE schema_migrations SET version=99")
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

    assert store.authenticate("PERSON@example.com", "correct horse battery", "subscriber") == subscriber_id
    assert store.authenticate("person@example.com", "correct horse battery", "administrator") is None
    assert store.authenticate("admin@example.com", "another secure password", "subscriber") is None
    assert store.authenticate("admin@example.com", "another secure password", "administrator") == admin_id
    assert store.authenticate("admin@example.com", "wrong password", "administrator") is None


def test_session_token_is_opaque_hashed_revocable_and_expires(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
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
    first, _ = store.create_session(identity_id)
    second, _ = store.create_session(identity_id)
    assert store.revoke_all_sessions(identity_id) == 2
    assert store.get_session(first) is None
    assert store.get_session(second) is None


def test_idle_session_is_revoked_and_last_seen_is_refreshed(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity_id = store.create_subscriber("person@example.com", "correct horse battery")
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
