"""Fail-closed tests for authentication controls not yet supported by runtime."""
import pytest

from app.auth_config import load_authentication_policy


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        (
            "AUTH_ADMIN_MFA_REQUIRED",
            "false",
            "administrator MFA is mandatory",
        ),
        (
            "AUTH_SUBSCRIBER_MFA_REQUIRED",
            "true",
            "subscriber MFA is implemented",
        ),
        (
            "AUTH_CSRF_REQUIRED",
            "false",
            "CSRF protection is mandatory",
        ),
    ],
)
def test_unsupported_auth_controls_are_rejected(
    monkeypatch, environment, setting, value, message
):
    monkeypatch.setenv("AUTH_ENVIRONMENT", environment)
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=message):
        load_authentication_policy()


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_mandatory_auth_controls_remain_enabled_by_default(monkeypatch, environment):
    monkeypatch.setenv("AUTH_ENVIRONMENT", environment)
    for name in (
        "AUTH_ADMIN_MFA_REQUIRED",
        "AUTH_SUBSCRIBER_MFA_REQUIRED",
        "AUTH_CSRF_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)

    policy = load_authentication_policy()

    assert policy.administrator_mfa_required is True
    assert policy.subscriber_mfa_required is False
    assert policy.csrf_required is True
