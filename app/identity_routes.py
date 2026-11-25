"""Isolated subscriber and administrator authentication routes.

This router is intentionally not mounted by app.main yet. It is exercised as
an independent HTTP surface while the legacy application remains unchanged.
The final cutover will mount it only after route-policy and entitlement wiring.
"""
from __future__ import annotations

import hashlib
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .auth import (
    ADMINISTRATOR_COOKIE,
    SUBSCRIBER_COOKIE,
    clear_csrf_cookie,
    clear_session_cookie,
    issue_csrf_cookie,
    set_session_cookie,
)
from .dependencies import (
    get_identity_store,
    get_rate_limiter,
    require_administrator,
    require_authenticated_csrf,
    require_subscriber,
)
from .email_delivery import (
    EmailDeliveryConfigurationError,
    EmailDeliveryError,
    create_email_sender,
)
from .identity import IdentityStore
from .email_queue import TransactionalEmailQueue, validate_email_queue_configuration
from .rate_limit import SQLiteRateLimiter, request_peer_ip

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegistrationInput(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class EmailInput(StrictModel):
    email: str = Field(min_length=3, max_length=254)


class TokenInput(StrictModel):
    token: str = Field(min_length=1, max_length=256)


class PasswordResetInput(StrictModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=1024)


class LoginInput(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class AdministratorLoginInput(LoginInput):
    totp_code: str | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=128)


def _rate_limit(
    request: Request,
    limiter: SQLiteRateLimiter,
    operation: str,
    identifier: str | None,
) -> None:
    """Atomically consume operation-specific IP, identity, cooldown and daily buckets."""
    ip = request_peer_ip(request)
    policies: list[tuple[str, int, int]] = []
    if operation == "subscriber-login":
        policies = [
            (f"auth:{operation}:ip:{ip}", 20, 900),
            (f"auth:{operation}:identity:{identifier}", 8, 900),
        ]
    elif operation == "administrator-login":
        policies = [
            (f"auth:{operation}:ip:{ip}", 8, 900),
            (f"auth:{operation}:identity:{identifier}", 5, 900),
        ]
    elif operation in {"subscriber-register", "verification-email", "password-reset-email"}:
        ip_limit = 10 if operation != "verification-email" else 5
        account_limit = 3
        policies = [
            (f"auth:{operation}:ip:{ip}", ip_limit, 3600),
            (f"auth:{operation}:identity:{identifier}", account_limit, 3600),
            (f"auth:{operation}:cooldown:{identifier}", 1, 60),
            (f"auth:{operation}:daily:{identifier}", 5, 86400),
        ]
    elif operation in {"email-verification-consume", "password-reset-consume"}:
        policies = [
            (f"auth:{operation}:ip:{ip}", 10, 3600),
            (f"auth:{operation}:token:{identifier}", 5, 3600),
        ]
    else:
        raise ValueError("Unsupported authentication rate-limit operation.")

    try:
        decision = limiter.consume_many(policies)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication protection is temporarily unavailable.",
        ) from None
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _identifier(value: str) -> str:
    """Normalize only account identifiers; the limiter stores only HMAC digests."""
    return value.strip().casefold()


def _public_origin() -> str:
    base = config.PUBLIC_BASE_URL
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail="Authentication email delivery is not configured.",
        )
    return base.rstrip("/")


def _public_link(path: str, token: str) -> str:
    base = _public_origin()
    return f"{base}/{path.lstrip('/')}#token={quote(token, safe='')}"


def _send_challenge_email(
    sender,
    *,
    store: IdentityStore,
    recipient: str,
    token: str,
    identity_id: str,
    purpose: str,
) -> str:
    """Encrypt and enqueue a one-time challenge; provider I/O runs in a worker."""
    if purpose == "verification":
        path = "verify-email"
        subject = "Verify your Open Odds Intelligence email"
        action = "verify your email address"
        lifetime = timedelta(hours=24)
        queue_purpose = "verification"
    else:
        path = "reset-password"
        subject = "Reset your Open Odds Intelligence password"
        action = "reset your password"
        lifetime = timedelta(minutes=30)
        queue_purpose = "password_reset"

    link = _public_link(path, token)
    safe_link = html.escape(link, quote=True)
    text_body = (
        f"Use this link to {action}:\\n\\n{link}\\n\\n"
        "If you did not request this message, you can ignore it."
    )
    html_body = (
        f"<p>Use the link below to {html.escape(action)}.</p>"
        f'<p><a href="{safe_link}">{html.escape(subject)}</a></p>'
        "<p>If you did not request this message, you can ignore it.</p>"
    )
    return TransactionalEmailQueue(store).enqueue(
        identity_id=identity_id,
        purpose=queue_purpose,
        recipient=recipient,
        subject=subject,
        text=text_body,
        html=html_body,
        challenge_expires_at=datetime.now(timezone.utc) + lifetime,
    )


def get_email_sender(request: Request):
    """Validate provider and encrypted-queue configuration before accepting a challenge."""
    configured = getattr(request.app.state, "email_sender", None)
    try:
        validate_email_queue_configuration()
        sender = configured if configured is not None else create_email_sender()
    except EmailDeliveryConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="Authentication email delivery is not configured.",
        ) from None
    if sender is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication email delivery is not configured.",
        )
    _public_origin()
    if configured is None:
        request.app.state.email_sender = sender
    return sender


def _deliver_or_record_failure(store: IdentityStore, sender, **kwargs) -> None:
    try:
        queue_id = _send_challenge_email(sender, store=store, **kwargs)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Transactional authentication email could not be queued.")
        raise HTTPException(
            status_code=503,
            detail="Authentication email delivery is temporarily unavailable.",
        ) from None
    logger.info("Transactional authentication email queued (reference=%s).", queue_id)


@router.get("/auth/csrf")
def csrf_bootstrap(response: Response):
    """Set the readable double-submit CSRF cookie before public auth mutations."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    issue_csrf_cookie(response)
    return {"ok": True}


@router.post("/auth/register", status_code=202, dependencies=[Depends(require_authenticated_csrf)])
def register_subscriber(
    payload: RegistrationInput,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
    sender=Depends(get_email_sender),
):
    _rate_limit(request, limiter, "subscriber-register", _identifier(payload.email))
    try:
        identity_id = store.create_subscriber(payload.email, payload.password)
    except sqlite3.IntegrityError:
        # Match the successful response to avoid disclosing whether the address
        # is already registered.
        return {"message": "If this address can be registered, a verification email will be sent."}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Registration is temporarily unavailable. Please try again later.",
        ) from None
    try:
        token = store.create_email_verification_challenge(payload.email)
        if token:
            _deliver_or_record_failure(
                store, sender,
                recipient=payload.email,
                token=token,
                identity_id=identity_id,
                purpose="verification",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Registration is temporarily unavailable. Please try again later.",
        ) from None
    return {"message": "If this address can be registered, a verification email will be sent."}


@router.post("/auth/verification/resend", status_code=202, dependencies=[Depends(require_authenticated_csrf)])
def resend_verification(
    payload: EmailInput,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
    sender=Depends(get_email_sender),
):
    _rate_limit(request, limiter, "verification-email", _identifier(payload.email))
    try:
        token = store.create_email_verification_challenge(payload.email)
        if token:
            # Resolve the internal ID only for an opaque idempotency namespace.
            with store.connect() as db:
                row = db.execute(
                    "SELECT id FROM identities WHERE email=? AND account_type='subscriber' AND is_active=1",
                    (_identifier(payload.email),),
                ).fetchone()
            if row:
                _deliver_or_record_failure(
                    store, sender, recipient=payload.email, token=token,
                    identity_id=row["id"], purpose="verification",
                )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable.") from None
    return {"message": "If the address requires verification, an email will be sent."}


@router.post("/auth/verify-email", dependencies=[Depends(require_authenticated_csrf)])
def verify_email(
    payload: TokenInput,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
):
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    _rate_limit(request, limiter, "email-verification-consume", token_hash)
    try:
        if not store.consume_email_verification_challenge(payload.token):
            raise HTTPException(status_code=400, detail="Verification link is invalid or expired.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Email verification is temporarily unavailable.") from None
    return {"ok": True, "message": "Email address verified. You can now sign in."}


@router.post("/auth/password-reset/request", status_code=202, dependencies=[Depends(require_authenticated_csrf)])
def request_password_reset(
    payload: EmailInput,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
    sender=Depends(get_email_sender),
):
    _rate_limit(request, limiter, "password-reset-email", _identifier(payload.email))
    try:
        token = store.create_password_reset_challenge(payload.email)
        if token:
            with store.connect() as db:
                row = db.execute(
                    "SELECT id FROM identities WHERE email=? AND is_active=1",
                    (_identifier(payload.email),),
                ).fetchone()
            if row:
                _deliver_or_record_failure(
                    store, sender, recipient=payload.email, token=token,
                    identity_id=row["id"], purpose="password-reset",
                )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Password recovery is temporarily unavailable.") from None
    return {"message": "If an active account matches this address, a recovery email will be sent."}


@router.post("/auth/password-reset/consume", dependencies=[Depends(require_authenticated_csrf)])
def consume_password_reset(
    payload: PasswordResetInput,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
):
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    _rate_limit(request, limiter, "password-reset-consume", token_hash)
    try:
        if not store.consume_password_reset_challenge(payload.token, payload.new_password):
            raise HTTPException(status_code=400, detail="Password reset link is invalid or expired.")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Password recovery is temporarily unavailable.") from None
    response = {"ok": True, "message": "Password updated. Please sign in again."}
    return response


@router.post("/auth/login", dependencies=[Depends(require_authenticated_csrf)])
def subscriber_login(
    payload: LoginInput,
    request: Request,
    response: Response,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
):
    _rate_limit(request, limiter, "subscriber-login", _identifier(payload.email))
    try:
        identity_id = store.authenticate(payload.email, payload.password, "subscriber")
        if identity_id is None:
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        previous_token = request.cookies.get(SUBSCRIBER_COOKIE)
        if previous_token:
            store.revoke_session(previous_token)
        token, _session_id = store.create_session(identity_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Authentication services are temporarily unavailable.") from None
    set_session_cookie(response, token, "subscriber", max_age=12 * 60 * 60)
    issue_csrf_cookie(response)
    return {"ok": True}


@router.post("/auth/logout", dependencies=[Depends(require_authenticated_csrf)])
def subscriber_logout(
    request: Request,
    response: Response,
    session: dict = Depends(require_subscriber),
    store: IdentityStore = Depends(get_identity_store),
):
    token = request.cookies.get(SUBSCRIBER_COOKIE)
    try:
        if token:
            store.revoke_session(token, actor_identity_id=session["identity_id"])
    except Exception:
        raise HTTPException(status_code=503, detail="Logout is temporarily unavailable.") from None
    clear_session_cookie(response, "subscriber")
    clear_csrf_cookie(response)
    return {"ok": True}


@router.post("/admin/auth/login", dependencies=[Depends(require_authenticated_csrf)])
def administrator_login(
    payload: AdministratorLoginInput,
    request: Request,
    response: Response,
    store: IdentityStore = Depends(get_identity_store),
    limiter: SQLiteRateLimiter = Depends(get_rate_limiter),
):
    _rate_limit(request, limiter, "administrator-login", _identifier(payload.email))
    if bool(payload.totp_code) == bool(payload.recovery_code):
        raise HTTPException(status_code=401, detail="Administrator authentication failed.")
    try:
        identity_id = store.authenticate(payload.email, payload.password, "administrator")
        if identity_id is None:
            raise HTTPException(status_code=401, detail="Administrator authentication failed.")
        if payload.totp_code:
            verified = store.verify_administrator_mfa(identity_id, payload.totp_code)
        else:
            verified = store.consume_administrator_recovery_code(identity_id, payload.recovery_code)
        if not verified:
            raise HTTPException(status_code=401, detail="Administrator authentication failed.")
        previous_token = request.cookies.get(ADMINISTRATOR_COOKIE)
        if previous_token:
            store.revoke_session(previous_token)
        token, _session_id = store.create_session(identity_id, timedelta(hours=4))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Administrator authentication is temporarily unavailable.") from None
    set_session_cookie(response, token, "administrator", max_age=4 * 60 * 60)
    issue_csrf_cookie(response)
    return {"ok": True}


@router.post("/admin/auth/logout", dependencies=[Depends(require_authenticated_csrf)])
def administrator_logout(
    request: Request,
    response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    token = request.cookies.get(ADMINISTRATOR_COOKIE)
    try:
        if token:
            store.revoke_session(token, actor_identity_id=session["identity_id"])
    except Exception:
        raise HTTPException(status_code=503, detail="Logout is temporarily unavailable.") from None
    clear_session_cookie(response, "administrator")
    clear_csrf_cookie(response)
    return {"ok": True}
