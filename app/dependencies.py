"""Lazy FastAPI dependencies for subscriber/admin session authorization.

Identity routes use strict subscriber/admin dependencies. Product routes use
mode-aware wrappers so the legacy Basic Auth deployment is unchanged, while
identity mode requires active sessions and server-side feature entitlements.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response

from .auth import require_same_origin_csrf, resolve_session
from .identity import IdentityStore
from .rate_limit import SQLiteRateLimiter


def get_identity_store(request: Request) -> IdentityStore:
    """Return the app-scoped identity store, creating it only on first use.

    Tests and future application wiring may inject an already-created store
    through app.state.identity_store. Initialization failures fail closed and
    never expose database paths or exception details to the client.
    """
    store = getattr(request.app.state, "identity_store", None)
    if store is not None:
        return store
    try:
        store = IdentityStore()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication services are temporarily unavailable.",
        ) from None
    request.app.state.identity_store = store
    return store


def _resolve_session_fail_closed(request: Request, store: IdentityStore, account_type: str) -> dict:
    """Translate identity-store outages into a generic fail-closed response."""
    try:
        return resolve_session(request, store, account_type)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication services are temporarily unavailable.",
        ) from None


def _set_no_store(response: Response) -> None:
    """Mark identity/auth responses non-cacheable before authorization can fail."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def require_subscriber(
    request: Request,
    response: Response,
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Require an active subscriber session; admin cookies never satisfy it."""
    _set_no_store(response)
    return _resolve_session_fail_closed(request, store, "subscriber")


def require_administrator(
    request: Request,
    response: Response,
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Require an active administrator session established after MFA."""
    _set_no_store(response)
    return _resolve_session_fail_closed(request, store, "administrator")


def require_authenticated_csrf(request: Request, response: Response) -> None:
    """Require same-origin CSRF and prevent caching even when validation rejects."""
    _set_no_store(response)
    require_same_origin_csrf(request)



def get_rate_limiter(request: Request) -> SQLiteRateLimiter:
    """Return a lazy app-scoped limiter; missing secret/storage fails closed."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        return limiter
    try:
        limiter = SQLiteRateLimiter()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication protection is temporarily unavailable.",
        ) from None
    request.app.state.rate_limiter = limiter
    return limiter


def get_odds_service():
    """Return the shared odds service without creating an import-time cycle."""
    try:
        from .service import service
        return service
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Platform diagnostics are temporarily unavailable.",
        ) from None
