"""Private, interactive first-administrator bootstrap.

Run manually on the host with MFA_ENCRYPTION_KEY configured. This command is
not an HTTP endpoint and accepts no credentials through command-line arguments.
"""
from __future__ import annotations

from getpass import getpass

from app.identity import IdentityStore


def main() -> int:
    store = IdentityStore()
    # Fail before creating any identity if encryption is not correctly configured.
    store._mfa_cipher()

    print("Open Odds Intelligence — initial administrator setup")
    email = input("Administrator email: ").strip()
    password = getpass("Administrator password (12+ characters): ")
    confirmation = getpass("Confirm administrator password: ")
    if password != confirmation:
        print("Passwords did not match. No administrator was created.")
        return 2

    try:
        try:
            identity_id = store.provision_initial_administrator(email, password)
        except ValueError as exc:
            # If a prior run created the first admin but was interrupted before
            # MFA enrollment, allow only that same account to resume with its
            # existing password. Never create a second administrator here.
            identity_id = store.authenticate(email, password, "administrator")
            if identity_id is None:
                raise exc
        provisioning_uri = store.start_administrator_mfa_enrollment(identity_id)
    except (ValueError, RuntimeError) as exc:
        print(f"Bootstrap could not continue: {exc}")
        return 2

    print("\nAdd this account to your authenticator app using the provisioning URI below.")
    print("Treat this URI as a secret; it contains the MFA seed and is shown only now.")
    print(provisioning_uri)
    print("\nEnter the current six-digit code from the authenticator app to finish setup.")
    while True:
        code = input("Authenticator code: ").strip()
        try:
            recovery_codes = store.confirm_administrator_mfa_enrollment(identity_id, code)
            break
        except ValueError as exc:
            print(f"Enrollment not confirmed: {exc}")

    print("\nAdministrator MFA is enrolled.")
    print("Save these recovery codes somewhere secure. Each code works once.")
    for recovery_code in recovery_codes:
        print(recovery_code)
    print("\nBootstrap complete. Recovery codes will not be displayed again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
