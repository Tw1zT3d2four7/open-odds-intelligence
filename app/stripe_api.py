"""Small, bounded Stripe REST client for hosted billing flows.

Only test-mode requests are permitted by the route layer during initial
acceptance. API credentials and provider error bodies are never returned.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class StripeAPIError(RuntimeError):
    """Sanitized provider failure safe to handle at an HTTP boundary."""


def stripe_request(method: str, path: str, *, params: dict | None = None,
                   idempotency_key: str | None = None) -> dict:
    """Perform one bounded form-encoded Stripe API request."""
    from . import config

    secret = config.STRIPE_SECRET_KEY
    if not isinstance(secret, str) or not secret.startswith("sk_test_"):
        raise StripeAPIError("Stripe test-mode API credentials are not configured.")
    if not path.startswith("/") or ".." in path:
        raise StripeAPIError("Stripe API request is invalid.")

    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "OpenOddsIntelligence/StripeBilling",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:255]
    body = urlencode(params or {}, doseq=True).encode("utf-8")
    request = Request(
        f"https://api.stripe.com/v1{path}",
        data=body if method.upper() != "GET" else None,
        headers=headers,
        method=method.upper(),
    )
    if method.upper() == "GET" and body:
        request = Request(
            f"https://api.stripe.com/v1{path}?{body.decode('ascii')}",
            headers=headers,
            method="GET",
        )
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise StripeAPIError("Stripe returned an oversized response.")
    except HTTPError as exc:
        # Never expose the body: Stripe errors can echo request or account data.
        raise StripeAPIError("Stripe could not complete the billing request.") from None
    except (URLError, TimeoutError, OSError):
        raise StripeAPIError("Stripe is temporarily unavailable.") from None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StripeAPIError("Stripe returned an invalid response.") from None
    if not isinstance(decoded, dict) or decoded.get("error"):
        raise StripeAPIError("Stripe could not complete the billing request.")
    return decoded
