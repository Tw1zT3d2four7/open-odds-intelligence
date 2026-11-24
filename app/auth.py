"""HTTP authentication boundary helpers; intentionally not wired to routes yet."""
from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

SUBSCRIBER_COOKIE = "__Host-ooi_subscriber"
ADMINISTRATOR_COOKIE = "__Host-ooi_admin"
CSRF_COOKIE = "__Host-ooi_csrf"
CSRF_HEADER = "x-ooi-csrf-token"
_COOKIE_BY_ACCOUNT = {
    "subscriber": SUBSCRIBER_COOKIE,
    "administrator": ADMINISTRATOR_COOKIE,
}


def set_session_cookie(
    response: Response, token: str, account_type: str, max_age: int = 43200
) -> None:
    """Set a host-only, Secure, HttpOnly cookie for exactly one account class."""
    if account_type not in _COOKIE_BY_ACCOUNT:
        raise ValueError("Unsupported account type.")
    if not isinstance(token, str) or not token or len(token) > 256:
        raise ValueError("A valid opaque session token is required.")
    if not isinstance(max_age, int) or not 1 <= max_age <= 30 * 24 * 60 * 60:
        raise ValueError("Cookie lifetime is outside the permitted range.")
    response.set_cookie(
        key=_COOKIE_BY_ACCOUNT[account_type],
        value=token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, account_type: str) -> None:
    """Expire only the selected account-class cookie."""
    if account_type not in _COOKIE_BY_ACCOUNT:
        raise ValueError("Unsupported account type.")
    response.delete_cookie(
        key=_COOKIE_BY_ACCOUNT[account_type],
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def issue_csrf_cookie(response: Response) -> str:
    """Issue a host-only double-submit CSRF token; callers return no token in JSON."""
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=12 * 60 * 60,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    return token


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(
        key=CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def resolve_session(request: Request, store, account_type: str) -> dict:
    """Resolve only the cookie belonging to the requested account class."""
    if account_type not in _COOKIE_BY_ACCOUNT:
        raise ValueError("Unsupported account type.")
    token = request.cookies.get(_COOKIE_BY_ACCOUNT[account_type])
    session = store.get_session(token) if token else None
    if not session or session.get("account_type") != account_type:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


def require_same_origin_csrf(request: Request) -> None:
    """Require a same-host Origin plus matching host-only CSRF cookie/header."""
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    parsed = urlsplit(origin)
    if (
        not origin
        or not host
        or parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.netloc.casefold() != host.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")

    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    header_token = request.headers.get(CSRF_HEADER, "")
    if (
        not cookie_token
        or not header_token
        or len(cookie_token) > 256
        or not hmac.compare_digest(cookie_token, header_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
