"""Validated environment-aware authentication policy.

This module is deliberately side-effect free: it parses policy but does not
silently weaken existing route guards. Route wiring must consume these values
explicitly. Production and staging always retain the mandatory security floor.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}.")
    return value


@dataclass(frozen=True)
class AuthenticationPolicy:
    environment: str
    identity_enabled: bool
    registration_enabled: bool
    subscriber_login_enabled: bool
    email_verification_required: bool
    email_transport: str
    password_recovery_enabled: bool
    administrator_mfa_required: bool
    subscriber_mfa_required: bool
    rate_limiting_enabled: bool
    csrf_required: bool
    subscriber_session_idle_seconds: int
    administrator_session_idle_seconds: int

    @property
    def is_local_development(self) -> bool:
        return self.environment == "development"

    @property
    def local_outbox_enabled(self) -> bool:
        return self.email_transport == "local_outbox"


def load_authentication_policy() -> AuthenticationPolicy:
    """Load and validate auth settings; unsafe production overrides are rejected."""
    environment = _choice(
        "AUTH_ENVIRONMENT", "production", {"development", "staging", "production"}
    )
    policy = AuthenticationPolicy(
        environment=environment,
        identity_enabled=_bool("AUTH_IDENTITY_ENABLED", True),
        registration_enabled=_bool("AUTH_REGISTRATION_ENABLED", True),
        subscriber_login_enabled=_bool("AUTH_SUBSCRIBER_LOGIN_ENABLED", True),
        email_verification_required=_bool("AUTH_EMAIL_VERIFICATION_REQUIRED", True),
        email_transport=_choice(
            "AUTH_EMAIL_TRANSPORT", "resend",
            {"resend", "local_outbox", "disabled"},
        ),
        password_recovery_enabled=_bool("AUTH_PASSWORD_RECOVERY_ENABLED", True),
        administrator_mfa_required=_bool("AUTH_ADMIN_MFA_REQUIRED", True),
        subscriber_mfa_required=_bool("AUTH_SUBSCRIBER_MFA_REQUIRED", False),
        rate_limiting_enabled=_bool("AUTH_RATE_LIMITING_ENABLED", True),
        csrf_required=_bool("AUTH_CSRF_REQUIRED", True),
        subscriber_session_idle_seconds=int(
            os.getenv("AUTH_SUBSCRIBER_SESSION_IDLE_SECONDS", "3600")
        ),
        administrator_session_idle_seconds=int(
            os.getenv("AUTH_ADMIN_SESSION_IDLE_SECONDS", "900")
        ),
    )
    if not 60 <= policy.subscriber_session_idle_seconds <= 2_592_000:
        raise ValueError("AUTH_SUBSCRIBER_SESSION_IDLE_SECONDS must be between 60 and 2592000.")
    if not 60 <= policy.administrator_session_idle_seconds <= 86_400:
        raise ValueError("AUTH_ADMIN_SESSION_IDLE_SECONDS must be between 60 and 86400.")

    if environment != "development":
        mandatory = {
            "AUTH_IDENTITY_ENABLED": policy.identity_enabled,
            "AUTH_EMAIL_VERIFICATION_REQUIRED": policy.email_verification_required,
            "AUTH_ADMIN_MFA_REQUIRED": policy.administrator_mfa_required,
            "AUTH_RATE_LIMITING_ENABLED": policy.rate_limiting_enabled,
            "AUTH_CSRF_REQUIRED": policy.csrf_required,
        }
        disabled = [name for name, enabled in mandatory.items() if not enabled]
        if disabled:
            raise ValueError(
                "Staging and production require these security controls: "
                + ", ".join(disabled)
                + "."
            )
        if policy.email_transport == "local_outbox":
            raise ValueError("AUTH_EMAIL_TRANSPORT=local_outbox is development-only.")
        if policy.email_verification_required and policy.email_transport == "disabled":
            raise ValueError(
                "Email verification cannot be required while AUTH_EMAIL_TRANSPORT=disabled."
            )
    else:
        if policy.email_verification_required and policy.email_transport == "disabled":
            raise ValueError(
                "Disable email verification or configure a delivery transport for local development."
            )
        if policy.email_transport == "disabled" and (
            policy.registration_enabled or policy.password_recovery_enabled
        ):
            raise ValueError(
                "Email delivery cannot be disabled while registration or password recovery is enabled."
            )
    return policy


# Module-level policy is evaluated at process startup. Invalid configuration
# prevents the application from starting rather than weakening auth at runtime.
AUTHENTICATION_POLICY = load_authentication_policy()
