"""Small, bounded Stripe REST client for hosted billing flows.

Only test-mode requests are permitted by the route layer during initial
acceptance. API credentials and provider error bodies are never returned.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class StripeAPIError(RuntimeError):
    """Sanitized provider failure safe to handle at an HTTP boundary."""


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward the Stripe bearer token through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_stripe_url(request: Request, *, timeout: int):
    """Open only the fixed Stripe API request without following redirects."""
    return build_opener(_RejectRedirects).open(request, timeout=timeout)


def stripe_request(method: str, path: str, *, params: dict | None = None,
                   idempotency_key: str | None = None) -> dict:
    """Perform one bounded form-encoded Stripe API request."""
    from . import config

    secret = config.STRIPE_SECRET_KEY
    if not isinstance(secret, str) or not re.fullmatch(r"sk_test_[A-Za-z0-9]+", secret):
        raise StripeAPIError("Stripe test-mode API credentials are not configured.")
    if method.upper() not in {"GET", "POST"} or not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or ".." in path or "//" in path:
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
        with _open_stripe_url(request, timeout=12) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "api.stripe.com":
                raise StripeAPIError("Stripe returned an unexpected API destination.")
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
