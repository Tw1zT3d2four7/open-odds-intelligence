"""Transactional email delivery adapters.

The first supported provider is Resend's HTTPS API. This module deliberately
uses the Python standard library so delivery remains lightweight and replaceable.
Provider errors are sanitized; response bodies and credentials are never logged
or included in raised exception messages.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable


class EmailDeliveryError(RuntimeError):
    """A sanitized email delivery failure safe for application-level handling."""


class EmailDeliveryConfigurationError(EmailDeliveryError):
    """Email delivery is not configured safely."""


class ResendEmailSender:
    """Send transactional messages through the Resend REST API."""

    endpoint = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        from_address: str,
        *,
        timeout: float = 10.0,
        opener: Callable = urllib.request.urlopen,
    ) -> None:
        if not isinstance(api_key, str) or not api_key or not api_key.strip():
            raise EmailDeliveryConfigurationError("Resend API key is not configured.")
        if not isinstance(from_address, str) or not from_address.strip():
            raise EmailDeliveryConfigurationError("Transactional sender address is not configured.")
        if "\r" in from_address or "\n" in from_address or len(from_address) > 254:
            raise EmailDeliveryConfigurationError("Transactional sender address is invalid.")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise EmailDeliveryConfigurationError("Email request timeout is outside the permitted range.")
        self._api_key = api_key  # Preserve provider keys exactly; they are case-sensitive.
        self._from_address = from_address
        self._timeout = float(timeout)
        self._opener = opener

    @staticmethod
    def _validate_recipient(recipient: str) -> str:
        if (
            not isinstance(recipient, str)
            or not recipient
            or len(recipient) > 254
            or "\r" in recipient
            or "\n" in recipient
            or recipient.count("@") != 1
            or any(ch.isspace() for ch in recipient)
        ):
            raise ValueError("A valid recipient email address is required.")
        local, domain = recipient.rsplit("@", 1)
        if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("A valid recipient email address is required.")
        return recipient

    def send(
        self,
        recipient: str,
        subject: str,
        *,
        text: str,
        html: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Send one message and return the provider message ID.

        No automatic retry is performed: a network timeout can occur after a
        provider accepted the message, so blind retries could create duplicates.
        Callers may supply a stable idempotency key for safe retry orchestration.
        """
        recipient = self._validate_recipient(recipient)
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            raise ValueError("A non-empty bounded email subject is required.")
        if "\r" in subject or "\n" in subject:
            raise ValueError("Email subject contains invalid line breaks.")
        if not isinstance(text, str) or not text or len(text) > 100_000:
            raise ValueError("A non-empty bounded plain-text email body is required.")
        if html is not None and (not isinstance(html, str) or len(html) > 200_000):
            raise ValueError("HTML email body exceeds the permitted size.")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key is not None:
            if (
                not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key)
            ):
                raise ValueError("Email idempotency key is invalid.")
            headers["Idempotency-Key"] = idempotency_key

        payload = {
            "from": self._from_address,
            "to": [recipient],
            "subject": subject,
            "text": text,
        }
        if html is not None:
            payload["html"] = html
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            raise EmailDeliveryError(
                f"Transactional email provider rejected the request (HTTP {exc.code})."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EmailDeliveryError("Transactional email provider is temporarily unavailable.") from None

        if not 200 <= int(status) < 300:
            raise EmailDeliveryError(
                f"Transactional email provider returned HTTP {int(status)}."
            )
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EmailDeliveryError("Transactional email provider returned an invalid response.") from None
        message_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise EmailDeliveryError("Transactional email provider response omitted its message ID.")
        return message_id


def create_email_sender():
    """Build the configured email sender, or fail explicitly when incomplete."""
    from . import config

    if config.EMAIL_PROVIDER == "disabled":
        return None
    if config.EMAIL_PROVIDER == "resend":
        return ResendEmailSender(config.RESEND_API_KEY, config.EMAIL_FROM)
    raise EmailDeliveryConfigurationError("Configured email provider is not supported.")
