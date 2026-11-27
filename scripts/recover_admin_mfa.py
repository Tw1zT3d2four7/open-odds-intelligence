"""Offline, password-gated administrator MFA recovery.

Run inside the application environment. The new seed exists only in memory;
the existing enrollment is replaced only after the new Authenticator code verifies.
"""
from __future__ import annotations

import getpass
import sys

import pyotp
import qrcode

from app import config
from app.identity import IdentityStore


def main() -> int:
    print("Open Odds Intelligence — administrator MFA recovery")
    print("This replaces the current administrator authenticator only after verification.")
    email = input("Administrator email: ").strip()
    password = getpass.getpass("Administrator password: ")
    store = IdentityStore()
    identity_id = store.authenticate(email, password, "administrator")
    password = ""
    if identity_id is None:
        print("Administrator credentials were not accepted. Nothing was changed.", file=sys.stderr)
        return 1

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=email, issuer_name=config.APP_NAME
    )
    qr = qrcode.QRCode(box_size=2, border=2)
    qr.add_data(uri)
    qr.make(fit=True)

    print("\nIn Authy, choose Add Account / Scan QR Code and scan this QR from your screen:")
    qr.print_ascii()
    print("\nKeep this terminal open. The existing enrollment is still active.")
    code = input("Enter the six-digit code shown by Authy: ").strip()
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        print("The new Authy code was not valid. The existing enrollment was not changed.", file=sys.stderr)
        return 1

    try:
        recovery_codes = store.replace_administrator_mfa_after_password_verification(
            identity_id, secret, code
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Recovery was not completed: {exc}. Existing enrollment was not changed.", file=sys.stderr)
        return 1

    secret = ""
    uri = ""
    print("\nMFA recovery succeeded. Save these one-time recovery codes somewhere secure:")
    for recovery_code in recovery_codes:
        print(recovery_code)
    print("\nEach recovery code can be used once. Do not share these codes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
