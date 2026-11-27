import importlib

import pytest

from app import auth_config


def _reload(monkeypatch, **values):
    names = (
        "AUTH_ENVIRONMENT", "AUTH_IDENTITY_ENABLED",
        "AUTH_REGISTRATION_ENABLED", "AUTH_SUBSCRIBER_LOGIN_ENABLED",
        "AUTH_EMAIL_VERIFICATION_REQUIRED", "AUTH_EMAIL_TRANSPORT",
        "AUTH_PASSWORD_RECOVERY_ENABLED", "AUTH_ADMIN_MFA_REQUIRED",
        "AUTH_SUBSCRIBER_MFA_REQUIRED", "AUTH_RATE_LIMITING_ENABLED",
        "AUTH_CSRF_REQUIRED", "AUTH_SUBSCRIBER_SESSION_IDLE_SECONDS",
        "AUTH_ADMIN_SESSION_IDLE_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    return importlib.reload(auth_config)


def test_production_defaults_keep_security_floor(monkeypatch):
    module = _reload(monkeypatch)
    policy = module.AUTHENTICATION_POLICY
    assert policy.environment == "production"
    assert policy.identity_enabled
    assert policy.email_verification_required
    assert policy.administrator_mfa_required
    assert policy.rate_limiting_enabled
    assert policy.csrf_required
    assert policy.email_transport == "resend"


@pytest.mark.parametrize(
    "override",
    [
        {"AUTH_IDENTITY_ENABLED": "false"},
        {"AUTH_ADMIN_MFA_REQUIRED": "false"},
        {"AUTH_RATE_LIMITING_ENABLED": "false"},
        {"AUTH_CSRF_REQUIRED": "false"},
        {"AUTH_EMAIL_TRANSPORT": "local_outbox"},
    ],
)
def test_production_rejects_weak_or_local_only_overrides(monkeypatch, override):
    with pytest.raises(ValueError):
        _reload(monkeypatch, **override)


def test_production_can_disable_email_verification_without_weakening_security(monkeypatch):
    module = _reload(
        monkeypatch,
        AUTH_EMAIL_VERIFICATION_REQUIRED="false",
    )
    policy = module.AUTHENTICATION_POLICY
    assert not policy.email_verification_required
    assert policy.identity_enabled
    assert policy.administrator_mfa_required
    assert policy.rate_limiting_enabled
    assert policy.csrf_required


def test_staging_keeps_mandatory_security_but_allows_resend(monkeypatch):
    module = _reload(monkeypatch, AUTH_ENVIRONMENT="staging")
    assert module.AUTHENTICATION_POLICY.administrator_mfa_required
    assert module.AUTHENTICATION_POLICY.email_transport == "resend"


def test_development_can_use_local_outbox_and_disable_optional_flows(monkeypatch):
    module = _reload(
        monkeypatch,
        AUTH_ENVIRONMENT="development",
        AUTH_EMAIL_TRANSPORT="local_outbox",
        AUTH_EMAIL_VERIFICATION_REQUIRED="false",
        AUTH_SUBSCRIBER_MFA_REQUIRED="false",
        AUTH_RATE_LIMITING_ENABLED="false",
    )
    policy = module.AUTHENTICATION_POLICY
    assert policy.is_local_development
    assert policy.local_outbox_enabled
    assert not policy.email_verification_required
    assert not policy.rate_limiting_enabled


def test_disabled_transport_cannot_leave_verification_required(monkeypatch):
    with pytest.raises(ValueError, match="Email verification"):
        _reload(
            monkeypatch,
            AUTH_ENVIRONMENT="development",
            AUTH_EMAIL_TRANSPORT="disabled",
            AUTH_EMAIL_VERIFICATION_REQUIRED="true",
        )


def test_invalid_boolean_and_session_lifetimes_are_rejected(monkeypatch):
    with pytest.raises(ValueError, match="boolean"):
        _reload(monkeypatch, AUTH_REGISTRATION_ENABLED="sometimes")
    with pytest.raises(ValueError, match="between 60"):
        _reload(monkeypatch, AUTH_SUBSCRIBER_SESSION_IDLE_SECONDS="30")



def test_development_can_register_without_email_when_verification_and_recovery_are_off(monkeypatch):
    module = _reload(
        monkeypatch,
        AUTH_ENVIRONMENT="development",
        AUTH_EMAIL_TRANSPORT="disabled",
        AUTH_EMAIL_VERIFICATION_REQUIRED="false",
        AUTH_PASSWORD_RECOVERY_ENABLED="false",
        AUTH_REGISTRATION_ENABLED="true",
    )
    policy = module.AUTHENTICATION_POLICY
    assert policy.registration_enabled
    assert not policy.email_verification_required
    assert not policy.password_recovery_enabled
    assert policy.email_transport == "disabled"


def test_development_cannot_disable_email_while_password_recovery_is_enabled(monkeypatch):
    with pytest.raises(ValueError, match="Password recovery"):
        _reload(
            monkeypatch,
            AUTH_ENVIRONMENT="development",
            AUTH_EMAIL_TRANSPORT="disabled",
            AUTH_EMAIL_VERIFICATION_REQUIRED="false",
            AUTH_PASSWORD_RECOVERY_ENABLED="true",
        )
