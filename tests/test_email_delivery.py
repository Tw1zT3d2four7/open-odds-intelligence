import json
import urllib.error

import pytest

from app.email_delivery import (
    EmailDeliveryConfigurationError,
    EmailDeliveryError,
    ResendEmailSender,
)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


def test_resend_sender_posts_expected_payload_and_preserves_api_key_case():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(b'{"id":"email_123"}')

    sender = ResendEmailSender(
        "Re_CaseSensitive-Key",
        "Open Odds Intelligence <alerts@example.com>",
        opener=opener,
    )
    message_id = sender.send(
        "member@example.net",
        "Verify your email",
        text="Open the verification link.",
        html="<p>Open the verification link.</p>",
        idempotency_key="verify:challenge-123",
    )

    assert message_id == "email_123"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer Re_CaseSensitive-Key"
    assert captured["headers"]["Idempotency-key"] == "verify:challenge-123"
    assert captured["body"]["to"] == ["member@example.net"]
    assert captured["body"]["from"] == "Open Odds Intelligence <alerts@example.com>"
    assert captured["body"]["html"] == "<p>Open the verification link.</p>"
    assert captured["timeout"] == 10.0


def test_resend_sender_requires_provider_configuration():
    with pytest.raises(EmailDeliveryConfigurationError):
        ResendEmailSender("", "alerts@example.com")
    with pytest.raises(EmailDeliveryConfigurationError):
        ResendEmailSender("re_secret", "")


@pytest.mark.parametrize("recipient", [
    "", "not-an-email", "a@@example.com", "a@example", "a@example.com\r\nBcc:x@y.com",
])
def test_resend_sender_rejects_invalid_recipient(recipient):
    sender = ResendEmailSender(
        "re_secret", "alerts@example.com",
        opener=lambda *_args, **_kwargs: FakeResponse(b'{"id":"unused"}'),
    )
    with pytest.raises(ValueError):
        sender.send(recipient, "Subject", text="Body")


def test_resend_sender_rejects_invalid_subject_and_idempotency_key():
    sender = ResendEmailSender(
        "re_secret", "alerts@example.com",
        opener=lambda *_args, **_kwargs: FakeResponse(b'{"id":"unused"}'),
    )
    with pytest.raises(ValueError):
        sender.send("member@example.com", "Subject\nBcc:attacker@example.com", text="Body")
    with pytest.raises(ValueError):
        sender.send("member@example.com", "Subject", text="Body", idempotency_key="bad key")


def test_resend_sender_sanitizes_provider_http_errors():
    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.resend.com/emails", 401, "unauthorized",
            hdrs=None, fp=None,
        )

    sender = ResendEmailSender("re_secret", "alerts@example.com", opener=opener)
    with pytest.raises(EmailDeliveryError) as error:
        sender.send("member@example.com", "Subject", text="Body")
    assert "HTTP 401" in str(error.value)
    assert "re_secret" not in str(error.value)
    assert "unauthorized" not in str(error.value)


def test_resend_sender_sanitizes_transport_failures():
    def opener(*_args, **_kwargs):
        raise OSError("private transport details")

    sender = ResendEmailSender("re_secret", "alerts@example.com", opener=opener)
    with pytest.raises(EmailDeliveryError) as error:
        sender.send("member@example.com", "Subject", text="Body")
    assert "private transport details" not in str(error.value)


def test_resend_sender_rejects_invalid_provider_response():
    sender = ResendEmailSender(
        "re_secret", "alerts@example.com",
        opener=lambda *_args, **_kwargs: FakeResponse(b'{"unexpected":"value"}'),
    )
    with pytest.raises(EmailDeliveryError, match="message ID"):
        sender.send("member@example.com", "Subject", text="Body")


def test_create_email_sender_can_be_disabled(monkeypatch):
    from app import config
    from app.email_delivery import create_email_sender

    monkeypatch.setattr(config, "EMAIL_PROVIDER", "disabled")
    assert create_email_sender() is None
