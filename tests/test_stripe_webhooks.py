import hashlib
import hmac
import json

import pytest

from app.stripe_webhooks import (
    StripeWebhookVerificationError,
    verify_stripe_event,
)


_SECRET = "whsec_" + ("s" * 40)
_NOW = 1_800_000_000


def _event(*, livemode=False):
    return {
        "id": "evt_test_123",
        "type": "customer.subscription.updated",
        "livemode": livemode,
        "data": {"object": {"id": "sub_test_123", "status": "active"}},
    }


def _signed(payload, *, timestamp=_NOW, secret=_SECRET, signatures=()):
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    values = [*signatures, digest]
    return f"t={timestamp}," + ",".join(f"v1={value}" for value in values)


def test_verifies_exact_raw_payload_and_accepts_multiple_v1_signatures():
    payload = json.dumps(_event(), separators=(",", ":")).encode()
    header = _signed(payload, signatures=["0" * 64])
    event = verify_stripe_event(
        payload, header, _SECRET, expected_livemode=False, now=_NOW
    )
    assert event["id"] == "evt_test_123"
    assert event["data"]["object"]["id"] == "sub_test_123"


def test_rejects_tampered_body_and_invalid_signature():
    payload = json.dumps(_event()).encode()
    header = _signed(payload)
    with pytest.raises(StripeWebhookVerificationError, match="signature verification"):
        verify_stripe_event(
            payload + b" ", header, _SECRET, expected_livemode=False, now=_NOW
        )
    with pytest.raises(StripeWebhookVerificationError, match="signature verification"):
        verify_stripe_event(
            payload, f"t={_NOW},v1={'0' * 64}", _SECRET,
            expected_livemode=False, now=_NOW,
        )


@pytest.mark.parametrize("timestamp", [_NOW - 301, _NOW + 301, 0, "not-a-time"])
def test_rejects_stale_future_or_invalid_signature_timestamps(timestamp):
    payload = json.dumps(_event()).encode()
    header = _signed(payload, timestamp=timestamp)
    with pytest.raises(StripeWebhookVerificationError, match="timestamp"):
        verify_stripe_event(
            payload, header, _SECRET, expected_livemode=False, now=_NOW
        )


def test_rejects_malformed_headers_secrets_and_oversized_inputs():
    payload = json.dumps(_event()).encode()
    for header in ("", "v1=" + "a" * 64, f"t={_NOW},t={_NOW},v1={'a' * 64}"):
        with pytest.raises(StripeWebhookVerificationError):
            verify_stripe_event(payload, header, _SECRET, now=_NOW)
    for secret in ("", "sk_test_not_a_webhook_secret", "whsec_short"):
        with pytest.raises(StripeWebhookVerificationError, match="secret"):
            verify_stripe_event(payload, _signed(payload), secret, now=_NOW)
    with pytest.raises(StripeWebhookVerificationError, match="payload size"):
        verify_stripe_event(b"", "t=1,v1=" + "a" * 64, _SECRET, now=1)
    with pytest.raises(StripeWebhookVerificationError, match="payload size"):
        verify_stripe_event(b"x" * 1_048_577, "t=1,v1=" + "a" * 64, _SECRET, now=1)


def test_enforces_test_and_live_mode_separation():
    payload = json.dumps(_event(livemode=False)).encode()
    with pytest.raises(StripeWebhookVerificationError, match="mode"):
        verify_stripe_event(
            payload, _signed(payload), _SECRET,
            expected_livemode=True, now=_NOW,
        )
    live_payload = json.dumps(_event(livemode=True)).encode()
    assert verify_stripe_event(
        live_payload, _signed(live_payload), _SECRET,
        expected_livemode=True, now=_NOW,
    )["livemode"] is True


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"id":"evt_1","id":"evt_2","type":"invoice.paid","livemode":false,"data":{"object":{}}}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{"object":{}},"bad":NaN}',
        b'{"id":"not-event","type":"invoice.paid","livemode":false,"data":{"object":{}}}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{}}',
    ],
)
def test_rejects_ambiguous_or_malformed_event_json(payload):
    header = _signed(payload)
    with pytest.raises(StripeWebhookVerificationError):
        verify_stripe_event(payload, header, _SECRET, now=_NOW)


def test_rejects_invalid_configuration_bounds():
    payload = json.dumps(_event()).encode()
    with pytest.raises(StripeWebhookVerificationError, match="tolerance"):
        verify_stripe_event(
            payload, _signed(payload), _SECRET, now=_NOW, tolerance_seconds=5
        )
    with pytest.raises(StripeWebhookVerificationError, match="Boolean"):
        verify_stripe_event(
            payload, _signed(payload), _SECRET, now=_NOW, expected_livemode=0
        )
