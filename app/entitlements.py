"""Server-side feature entitlements for subscriber product routes."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from . import config
from .auth import require_same_origin_csrf
from .dependencies import (
    get_identity_store, require_administrator, require_subscriber,
)
from .identity import IdentityStore

# These feature keys describe existing product capabilities, not plan names or
# prices. Commercial plans can later grant bundles of these keys.
def require_product_subscriber(
    request: Request,
) -> dict:
    """Use the legacy Basic Auth boundary unchanged until identity mode is selected."""
    if config.AUTH_MODE == "legacy_basic":
        return {"account_type": "legacy_operator"}
    store = get_identity_store(request)
    # Product workspaces are available to both paid subscribers and MFA-authenticated
    # administrators. Admins bypass subscriber feature grants but retain the same
    # server-side session validation and receive the full provider data scope.
    try:
        session = require_administrator(request, store)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return require_subscriber(request, store)
    request.state.include_premium_data = True
    return session


def require_product_administrator(
    request: Request,
) -> dict:
    """Preserve legacy operator access; require an MFA-backed admin in identity mode."""
    if config.AUTH_MODE == "legacy_basic":
        request.state.include_premium_data = True
        return {"account_type": "legacy_operator"}
    session = require_administrator(request, get_identity_store(request))
    request.state.include_premium_data = True
    return session


def require_product_csrf(request: Request) -> None:
    """Keep legacy Basic Auth mutation behavior; enforce CSRF for cookie sessions."""
    if config.AUTH_MODE == "identity":
        require_same_origin_csrf(request)


ROUTE_ENTITLEMENTS = {
    "/": "dashboard",
    "/api/workspace/status": "dashboard",
    "/api/refresh": "dashboard",
    "/api/limits/refresh": "market_limits",
    "/api/simulation/games/refresh": "simulation",
    "/api/limits": "market_limits",
    "/api/limits/changes": "market_limits",
    "/api/limits/history": "market_limits",
    "/api/odds": "odds",
    "/api/market-board": "market_board",
    "/api/bookmaker-board": "market_board",
    "/api/simulation/leagues": "simulation",
    "/api/simulation/games": "simulation",
    "/api/simulation/run": "simulation",
    "/api/player-props": "player_props",
    "/api/clv": "clv",
    "/api/movement": "market_movement",
    "/api/movement/history": "market_movement",
    "/api/history": "history",
    "/api/opportunities": "opportunities",
    "/api/arbitrage": "arbitrage",
}


def require_product_entitlement(
    request: Request,
    session: dict = Depends(require_product_subscriber),
) -> None:
    """Require an active subscriber session and an explicit matching feature grant."""
    if config.AUTH_MODE == "legacy_basic":
        request.state.include_premium_data = True
        return
    if session.get("account_type") in {"administrator", "legacy_operator"}:
        request.state.include_premium_data = True
        return
    entitlement = ROUTE_ENTITLEMENTS.get(request.url.path)
    if entitlement is None:
        raise HTTPException(
            status_code=403,
            detail="This product feature is not enabled for the account.",
        )
    try:
        store = get_identity_store(request)
        allowed = store.has_subscriber_entitlement(
            session["identity_id"], entitlement
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Account authorization is temporarily unavailable.",
        ) from None
    request.state.include_premium_data = bool(
        allowed and store.has_subscriber_entitlement(session["identity_id"], "premium_data")
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="This product feature is not included in the account's current access.",
        )
