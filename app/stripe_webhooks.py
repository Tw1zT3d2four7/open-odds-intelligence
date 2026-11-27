"""Strict Stripe webhook signature verification without persisting raw payloads.

This module is deliberately transport- and framework-neutral. A future HTTP
route must pass the exact raw request bytes and enforce the configured mode
before it invokes any billing mutation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any


_MAX_PAYLOAD_BYTES = 1_048_576
_MAX_HEADER_BYTES = 4096
_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_]{1,250}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.]{1,127}$")
_HEX_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")


class StripeWebhookVerificationError(ValueError):
    """Raised when a Stripe webhook cannot be authenticated or accepted."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StripeWebhookVerificationError("Stripe event JSON contains duplicate keys.")
        result[key] = value
    return result


def verify_stripe_event(
    payload: bytes,
    signature_header: str,
    signing_secret: str,
    *,
    expected_livemode: bool = False,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    """Verify Stripe's timestamped HMAC-SHA256 signature and parse its event.

    The caller must supply the exact raw HTTP body. The function returns the
    validated event object but never logs or persists the body or signing key.
    """
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_PAYLOAD_BYTES:
        raise StripeWebhookVerificationError("Stripe webhook payload size is invalid.")
    if not isinstance(signature_header, str) or not 1 <= len(signature_header) <= _MAX_HEADER_BYTES:
        raise StripeWebhookVerificationError("Stripe signature header is invalid.")
    if (
        not isinstance(signing_secret, str)
        or not signing_secret.startswith("whsec_")
        or not 32 <= len(signing_secret) <= 255
    ):
        raise StripeWebhookVerificationError("Stripe webhook signing secret is not configured.")
    if type(expected_livemode) is not bool:
        raise StripeWebhookVerificationError("Expected Stripe mode must be a Boolean.")
    if not isinstance(tolerance_seconds, int) or not 30 <= tolerance_seconds <= 900:
        raise StripeWebhookVerificationError("Stripe signature tolerance is invalid.")

    timestamp_values: list[str] = []
    signatures: list[str] = []
    for item in signature_header.split(","):
        key, separator, value = item.strip().partition("=")
        if not separator or not key or not value:
            raise StripeWebhookVerificationError("Stripe signature header is malformed.")
        if key == "t":
            timestamp_values.append(value)
        elif key == "v1":
            signatures.append(value.lower())
    if len(timestamp_values) != 1 or not signatures:
        raise StripeWebhookVerificationError("Stripe signature header is incomplete.")
    if any(not _HEX_SIGNATURE.fullmatch(signature) for signature in signatures):
        raise StripeWebhookVerificationError("Stripe signature digest is malformed.")
    try:
        timestamp = int(timestamp_values[0])
    except (TypeError, ValueError):
        raise StripeWebhookVerificationError("Stripe signature timestamp is invalid.") from None
    if timestamp <= 0:
        raise StripeWebhookVerificationError("Stripe signature timestamp is invalid.")
    current_time = int(time.time()) if now is None else now
    if not isinstance(current_time, int) or abs(current_time - timestamp) > tolerance_seconds:
        raise StripeWebhookVerificationError("Stripe signature timestamp is outside the tolerance window.")

    signed_payload = str(timestamp).encode("ascii") + b"." + payload
    expected_signature = hmac.new(
        signing_secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected_signature, signature) for signature in signatures):
        raise StripeWebhookVerificationError("Stripe webhook signature verification failed.")

    try:
        event = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                StripeWebhookVerificationError("Stripe event JSON contains an invalid number.")
            ),
        )
    except StripeWebhookVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise StripeWebhookVerificationError("Stripe event JSON is invalid.") from None
    if not isinstance(event, dict):
        raise StripeWebhookVerificationError("Stripe event must be a JSON object.")
    event_id = event.get("id")
    event_type = event.get("type")
    livemode = event.get("livemode")
    data = event.get("data")
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise StripeWebhookVerificationError("Stripe event ID is invalid.")
    if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
        raise StripeWebhookVerificationError("Stripe event type is invalid.")
    if type(livemode) is not bool or livemode is not expected_livemode:
        raise StripeWebhookVerificationError("Stripe event mode does not match this environment.")
    if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
        raise StripeWebhookVerificationError("Stripe event data object is invalid.")
    return event
