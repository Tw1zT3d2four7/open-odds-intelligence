"""Authenticated Stripe webhook intake.

Only signature-verified events enter the durable inbox. Processing and
subscription/entitlement mutations are deliberately separate from intake.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from . import config
from .dependencies import get_identity_store
from .identity import IdentityStore
from .stripe_webhooks import StripeWebhookVerificationError, verify_stripe_event

router = APIRouter(include_in_schema=False)
_MAX_BODY_BYTES = 1_048_576


@router.post("/webhooks/stripe")
async def receive_stripe_webhook(
    request: Request,
    response: Response,
    store: IdentityStore = Depends(get_identity_store),
):
    """Verify and persist a fingerprint plus encrypted event data for durable processing."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if not config.STRIPE_WEBHOOK_SECRET or not config.STRIPE_WEBHOOK_ENCRYPTION_KEY:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook intake or encrypted event storage is not configured.",
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) < 1 or int(content_length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload is too large or empty.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Webhook content length is invalid.") from None

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large.")
        chunks.append(chunk)
    payload = b"".join(chunks)
    signature = request.headers.get("stripe-signature", "")
    try:
        event = verify_stripe_event(
            payload,
            signature,
            config.STRIPE_WEBHOOK_SECRET,
            expected_livemode=config.STRIPE_WEBHOOK_LIVEMODE,
        )
        result = store.record_stripe_webhook_event(
            event["id"], event["type"], event["livemode"], payload
        )
    except StripeWebhookVerificationError:
        raise HTTPException(status_code=400, detail="Stripe webhook verification failed.") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="Stripe webhook event was rejected.") from None
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook intake is temporarily unavailable.",
        ) from None

    return {
        "received": True,
        "duplicate": not result["created"],
        "processing_state": result["processing_state"],
    }
