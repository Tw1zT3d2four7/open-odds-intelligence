"""Subscriber and administrator authentication routes.

The router is registered in app.main but exposed only through the explicit
AUTH_MODE=identity allowlist. In legacy_basic mode its paths remain hidden;
identity mode still blocks all product routes until authorization and
entitlement wiring is complete.
"""
from __future__ import annotations

import hashlib
import html
import logging
import sqlite3
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
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
    get_odds_service,
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

_AUTH_PAGE = Path(__file__).resolve().parent.parent / "web" / "auth.html"
_ADMIN_PAGE = Path(__file__).resolve().parent.parent / "web" / "admin.html"


def _render_auth_page() -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    content = _AUTH_PAGE.read_text(encoding="utf-8")
    if content.count("<script>") != 1:
        raise RuntimeError("Authentication page must contain exactly one nonceable script block.")
    content = content.replace("<script>", f'<script nonce="{nonce}">', 1)
    response = HTMLResponse(content)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _render_admin_page() -> HTMLResponse:
    """Render the private console with a per-response script nonce and safe headers."""
    nonce = secrets.token_urlsafe(18)
    content = _ADMIN_PAGE.read_text(encoding="utf-8")
    if content.count("<script>") != 1:
        raise RuntimeError("Administrator page must contain exactly one nonceable script block.")
    content = content.replace("<script>", f'<script nonce="{nonce}">', 1)
    response = HTMLResponse(content)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@router.get("/login", response_class=HTMLResponse)
def subscriber_login_page():
    return _render_auth_page()


@router.get("/register", response_class=HTMLResponse)
def subscriber_registration_page():
    return _render_auth_page()


@router.get("/admin/login", response_class=HTMLResponse)
def administrator_login_page():
    return _render_auth_page()


@router.get(
    "/admin", response_class=HTMLResponse,
    dependencies=[Depends(require_administrator)],
)
def administrator_console_page():
    """Serve the separate console only to an active MFA-authenticated administrator."""
    return _render_admin_page()


@router.get("/forgot-password", response_class=HTMLResponse)
def password_recovery_page():
    return _render_auth_page()


@router.get("/verify-email", response_class=HTMLResponse)
def email_verification_page():
    return _render_auth_page()


@router.get("/reset-password", response_class=HTMLResponse)
def password_reset_page():
    return _render_auth_page()


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


class SessionRevokeInput(StrictModel):
    reason_code: str = Field(pattern="^(security_review|support_request|account_request|other)$")


class EntitlementUpdateInput(StrictModel):
    identity_id: str = Field(min_length=1, max_length=128)
    entitlements: list[str] = Field(max_length=32)
    expires_at: datetime | None = None


class SubscriberStatusInput(StrictModel):
    is_active: bool
    reason_code: str = Field(pattern="^(support_request|policy_violation|security_concern|billing_issue|other)$")


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
    try:
        parsed.port  # Force validation of malformed explicit ports.
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
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
        f"Use this link to {action}:\n\n{link}\n\n"
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
        challenge_token=token,
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
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
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
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
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
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
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


@router.get(
    "/admin/overview",
    dependencies=[Depends(require_administrator)],
)
def administrator_overview(
    response: Response,
    _session: dict = Depends(require_administrator),
    odds_service=Depends(get_odds_service),
):
    """Return a safe allowlist of live operational metrics to the Admin Console."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        status = odds_service.status()
        if not isinstance(status, dict):
            raise RuntimeError("Invalid service status response.")
        freshness = []
        for item in status.get("sport_freshness", []):
            if not isinstance(item, dict):
                continue
            freshness.append({
                "sport": str(item.get("sport") or "")[:80],
                "freshness": str(item.get("freshness") or "unknown")[:24],
                "last_updated": item.get("last_updated"),
                "age_seconds": item.get("age_seconds"),
                "event_count": max(0, int(item.get("event_count") or 0)),
            })
        return {
            "providers": [
                str(name)[:40] for name in status.get("providers", [])
                if isinstance(name, str)
            ],
            "configured": bool(status.get("configured")),
            "data_freshness": str(status.get("data_freshness") or "unknown")[:24],
            "last_updated": status.get("last_updated"),
            "stored_snapshots": max(0, int(status.get("stored_snapshots") or 0)),
            "stored_events": max(0, int(status.get("stored_events") or 0)),
            "stored_bookmakers": max(0, int(status.get("stored_bookmakers") or 0)),
            "sports": freshness,
            "alerts": {
                "enabled": bool(status.get("alerts_enabled")),
                "sent": max(0, int(status.get("alerts_sent") or 0)),
                "last_sent": status.get("last_alert_at"),
                "last_check": status.get("last_alert_check"),
            },
            "provider_error_present": bool(status.get("last_error")),
            "alert_error_present": bool(status.get("last_alert_error") or status.get("alert_error")),
        }
    except Exception:
        logger.exception("Administrator overview query failed.")
        raise HTTPException(
            status_code=503,
            detail="Platform overview is temporarily unavailable.",
        ) from None


@router.get(
    "/admin/subscribers",
    dependencies=[Depends(require_administrator)],
)
def administrator_subscribers(
    response: Response,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000_000),
    email_query: str | None = Query(None, min_length=1, max_length=254),
    _session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Return a minimal, paginated subscriber directory to administrators."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return store.list_subscribers(
            limit=limit, offset=offset, email_query=email_query
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator subscriber directory query failed.")
        raise HTTPException(
            status_code=503,
            detail="Subscriber directory is temporarily unavailable.",
        ) from None


@router.post(
    "/admin/subscribers/{identity_id}/status",
    dependencies=[
        Depends(require_administrator),
        Depends(require_authenticated_csrf),
    ],
)
def update_subscriber_status(
    identity_id: str,
    payload: SubscriberStatusInput,
    response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Suspend/reactivate a subscriber with MFA-admin attribution and CSRF."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return store.set_subscriber_status(
            identity_id,
            is_active=payload.is_active,
            actor_identity_id=session["identity_id"],
            reason_code=payload.reason_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator subscriber status change failed.")
        raise HTTPException(
            status_code=503,
            detail="Subscriber status change is temporarily unavailable.",
        ) from None


@router.post(
    "/admin/subscribers/{identity_id}/sessions/revoke",
    dependencies=[Depends(require_administrator), Depends(require_authenticated_csrf)],
)
def revoke_subscriber_account_sessions(
    identity_id: str, payload: SessionRevokeInput, response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Revoke all sessions for one subscriber with MFA-admin attribution and CSRF."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        count = store.revoke_subscriber_sessions(
            identity_id, actor_identity_id=session["identity_id"],
            reason_code=payload.reason_code,
        )
        return {"identity_id": identity_id, "revoked_sessions": count}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator subscriber session revocation failed.")
        raise HTTPException(status_code=503, detail="Account session revocation is temporarily unavailable.") from None


@router.get(
    "/admin/sessions",
    dependencies=[Depends(require_administrator)],
)
def administrator_sessions(
    response: Response,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000_000),
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Return active session metadata without exposing bearer tokens or hashes."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return store.list_active_sessions(
            actor_identity_id=session["identity_id"],
            current_session_id=session["session_id"], limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator session directory query failed.")
        raise HTTPException(status_code=503, detail="Session directory is temporarily unavailable.") from None


@router.post(
    "/admin/sessions/{session_id}/revoke",
    dependencies=[Depends(require_administrator), Depends(require_authenticated_csrf)],
)
def revoke_administrator_visible_session(
    session_id: str, payload: SessionRevokeInput, response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Revoke a selected session; the current administrator session is protected."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        changed = store.revoke_session_by_id(
            session_id, actor_identity_id=session["identity_id"],
            current_session_id=session["session_id"], reason_code=payload.reason_code,
        )
        if not changed:
            raise HTTPException(status_code=404, detail="Active session not found.")
        return {"revoked": True}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator session revocation failed.")
        raise HTTPException(status_code=503, detail="Session revocation is temporarily unavailable.") from None


@router.get(
    "/admin/audit-events",
    dependencies=[Depends(require_administrator)],
)
def administrator_audit_events(
    response: Response,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000_000),
    event_type: str | None = Query(None, min_length=1, max_length=128),
    actor_identity_id: str | None = Query(None, min_length=1, max_length=128),
    subject_identity_id: str | None = Query(None, min_length=1, max_length=128),
    _session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    """Return a bounded, secret-free audit page to an active MFA administrator."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return store.list_security_audit_events(
            limit=limit,
            offset=offset,
            event_type=event_type,
            actor_identity_id=actor_identity_id,
            subject_identity_id=subject_identity_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Administrator audit query failed.")
        raise HTTPException(
            status_code=503,
            detail="Security audit history is temporarily unavailable.",
        ) from None


@router.get(
    "/admin/subscriber-entitlements",
    dependencies=[Depends(require_administrator)],
)
def get_subscriber_entitlements(
    response: Response,
    identity_id: str,
    _session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        entitlements = store.active_subscriber_entitlements(identity_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Entitlement lookup is temporarily unavailable.") from None
    return {"identity_id": identity_id, "entitlements": entitlements}


@router.put(
    "/admin/subscriber-entitlements",
    dependencies=[
        Depends(require_administrator),
        Depends(require_authenticated_csrf),
    ],
)
def update_subscriber_entitlements(
    payload: EntitlementUpdateInput,
    response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        entitlements = store.set_subscriber_entitlements(
            payload.identity_id,
            payload.entitlements,
            actor_identity_id=session["identity_id"],
            expires_at=payload.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Entitlement update is temporarily unavailable.") from None
    return {"identity_id": payload.identity_id, "entitlements": entitlements}


@router.post("/admin/auth/logout", dependencies=[Depends(require_authenticated_csrf)])
def administrator_logout(
    request: Request,
    response: Response,
    session: dict = Depends(require_administrator),
    store: IdentityStore = Depends(get_identity_store),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    token = request.cookies.get(ADMINISTRATOR_COOKIE)
    try:
        if token:
            store.revoke_session(token, actor_identity_id=session["identity_id"])
    except Exception:
        raise HTTPException(status_code=503, detail="Logout is temporarily unavailable.") from None
    clear_session_cookie(response, "administrator")
    clear_csrf_cookie(response)
    return {"ok": True}
