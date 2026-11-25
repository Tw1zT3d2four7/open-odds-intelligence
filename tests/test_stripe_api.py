"""Bounded and secret-safe Stripe REST client tests."""
import io
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from app import config, stripe_api


class FakeResponse:
    def __init__(self, body=b'{"id":"ok"}', url="https://api.stripe.com/v1/customers"):
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_post_uses_case_preserved_test_key_form_body_and_idempotency(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_CaseSensitiveKey123")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(stripe_api, "_open_stripe_url", fake_urlopen)
    result = stripe_api.stripe_request(
        "POST", "/customers",
        params={"email": "buyer@example.test", "metadata[identity_id]": "identity-123"},
        idempotency_key="ooi-idempotency-key",
    )
    request = seen["request"]
    assert result == {"id": "ok"}
    assert seen["timeout"] == 12
    assert request.full_url == "https://api.stripe.com/v1/customers"
    assert request.get_header("Authorization") == "Bearer sk_test_CaseSensitiveKey123"
    assert request.get_header("Idempotency-key") == "ooi-idempotency-key"
    assert parse_qs(request.data.decode()) == {
        "email": ["buyer@example.test"],
        "metadata[identity_id]": ["identity-123"],
    }


def test_get_encodes_query_without_request_body(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_GetQueryCase")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        return FakeResponse(b'{"id":"price_123"}', "https://api.stripe.com/v1/prices/price_123")

    monkeypatch.setattr(stripe_api, "urlopen", fake_urlopen)
    result = stripe_api.stripe_request("GET", "/prices/price_123", params={"expand[]": ["product"]})
    request = seen["request"]
    parsed = urlsplit(request.full_url)
    assert result == {"id": "price_123"}
    assert parsed.path == "/v1/prices/price_123"
    assert parse_qs(parsed.query) == {"expand[]": ["product"]}
    assert request.data is None


@pytest.mark.parametrize(
    ("secret", "method", "path"),
    [
        ("sk_live_NotAllowed", "POST", "/customers"),
        ("sk_test_Invalid!", "POST", "/customers"),
        ("sk_test_ValidKey", "DELETE", "/customers"),
        ("sk_test_ValidKey", "POST", "//evil.example/customers"),
        ("sk_test_ValidKey", "POST", "/customers?expand=all"),
        ("sk_test_ValidKey", "POST", "/../customers"),
    ],
)
def test_invalid_key_method_or_path_fails_before_network(monkeypatch, secret, method, path):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", secret)
    monkeypatch.setattr(
        stripe_api, "urlopen",
        lambda *_args, **_kwargs: pytest.fail("invalid Stripe request must not reach the network"),
    )
    with pytest.raises(stripe_api.StripeAPIError):
        stripe_api.stripe_request(method, path)


def test_provider_http_error_is_sanitized(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_ErrorSanitize")
    def fail(*_args, **_kwargs):
        raise HTTPError(
            "https://api.stripe.com/v1/customers", 400, "bad request", {},
            io.BytesIO(b'{"error":{"message":"secret customer details"}}'),
        )
    monkeypatch.setattr(stripe_api, "urlopen", fail)
    with pytest.raises(stripe_api.StripeAPIError) as error:
        stripe_api.stripe_request("POST", "/customers")
    assert "secret customer details" not in str(error.value)


def test_redirect_handler_never_follows_or_forwards_bearer_credentials():
    handler = stripe_api._RejectRedirects()
    request = Request(
        "https://api.stripe.com/v1/customers",
        headers={"Authorization": "Bearer sk_test_never_forward"},
    )
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://attacker.example/collect"
    ) is None


def test_unexpected_response_host_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_RedirectGuard")
    monkeypatch.setattr(
        stripe_api, "urlopen",
        lambda *_args, **_kwargs: FakeResponse(url="https://attacker.example/steal"),
    )
    with pytest.raises(stripe_api.StripeAPIError, match="destination"):
        stripe_api.stripe_request("GET", "/prices/price_123")


def test_oversized_and_invalid_json_responses_are_rejected(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_ResponseBounds")
    monkeypatch.setattr(
        stripe_api, "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"x" * 1_048_577),
    )
    with pytest.raises(stripe_api.StripeAPIError, match="oversized"):
        stripe_api.stripe_request("GET", "/prices/price_123")

    monkeypatch.setattr(
        stripe_api, "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"not-json"),
    )
    with pytest.raises(stripe_api.StripeAPIError, match="invalid response"):
        stripe_api.stripe_request("GET", "/prices/price_123")
