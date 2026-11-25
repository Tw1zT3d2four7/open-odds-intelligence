from getpass import getpass

import pyotp
from cryptography.fernet import Fernet

from app import config
from app.identity import IdentityStore
from scripts import bootstrap_admin


def test_initial_admin_bootstrap_can_resume_after_mfa_enrollment_interruption(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    email = "bootstrap-resume@example.test"
    password = "a strong bootstrap password 123"
    identity_id = store.provision_initial_administrator(email, password)

    monkeypatch.setattr(bootstrap_admin, "IdentityStore", lambda: store)
    monkeypatch.setattr(bootstrap_admin, "getpass", lambda _prompt: password)
    provisioning = {}
    original_start = store.start_administrator_mfa_enrollment

    def capture_provisioning_uri(target_identity_id):
        uri = original_start(target_identity_id)
        provisioning["uri"] = uri
        return uri

    monkeypatch.setattr(store, "start_administrator_mfa_enrollment", capture_provisioning_uri)

    def answer(prompt):
        if prompt.startswith("Administrator email:"):
            return email
        if prompt.startswith("Authenticator code:"):
            secret = pyotp.parse_uri(provisioning["uri"]).secret
            return pyotp.TOTP(secret).now()
        raise AssertionError(f"Unexpected bootstrap prompt: {prompt}")

    monkeypatch.setattr("builtins.input", answer)
    assert bootstrap_admin.main() == 0
    output = capsys.readouterr().out
    assert "Administrator MFA is enrolled." in output
    assert "Bootstrap complete." in output

    with store.connect() as db:
        mfa = db.execute(
            "SELECT enrolled_at FROM administrator_mfa WHERE identity_id=?",
            (identity_id,),
        ).fetchone()
        recovery_count = db.execute(
            "SELECT COUNT(*) FROM administrator_mfa_recovery_codes WHERE identity_id=?",
            (identity_id,),
        ).fetchone()[0]
    assert mfa["enrolled_at"] is not None
    assert recovery_count == 10


def test_bootstrap_resume_rejects_wrong_password_without_creating_mfa(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    email = "bootstrap-existing@example.test"
    store.provision_initial_administrator(email, "a strong bootstrap password 123")

    monkeypatch.setattr(bootstrap_admin, "IdentityStore", lambda: store)
    monkeypatch.setattr(bootstrap_admin, "getpass", lambda _prompt: "wrong password 123")
    monkeypatch.setattr("builtins.input", lambda _prompt: email)
    assert bootstrap_admin.main() == 2
    with store.connect() as db:
        assert db.execute(
            "SELECT 1 FROM administrator_mfa"
        ).fetchone() is None

