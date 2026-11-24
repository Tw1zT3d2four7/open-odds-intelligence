"""Lazy FastAPI dependencies for the isolated subscriber/admin session boundary.

These dependencies are deliberately not wired to application routes yet. The
legacy Basic Auth middleware and all existing endpoint behavior remain intact.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .auth import require_same_origin_csrf, resolve_session
from .identity import IdentityStore


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


def require_subscriber(
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Require an active subscriber session; admin cookies never satisfy it."""
    return resolve_session(request, store, "subscriber")


def require_administrator(
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Require an active administrator session established after MFA."""
    return resolve_session(request, store, "administrator")


def require_authenticated_csrf(request: Request) -> None:
    """Require the HTTPS same-origin double-submit CSRF check for mutations."""
    require_same_origin_csrf(request)
