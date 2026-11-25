"""Server-side feature entitlements for subscriber product routes."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .dependencies import get_identity_store, require_subscriber
from .identity import IdentityStore

# These feature keys describe existing product capabilities, not plan names or
# prices. Commercial plans can later grant bundles of these keys.
ROUTE_ENTITLEMENTS = {
    "/": "dashboard",
    "/api/limits": "market_limits",
    "/api/limits/changes": "market_limits",
    "/api/limits/history": "market_limits",
    "/api/odds": "odds",
    "/api/market-board": "market_board",
    "/api/bookmaker-board": "market_board",
    "/api/simulation/leagues": "simulation",
    "/api/simulation/games": "simulation",
    "/api/simulation/games/refresh": "simulation",
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
    session: dict = Depends(require_subscriber),
    store: IdentityStore = Depends(get_identity_store),
) -> None:
    """Require an active subscriber session and an explicit matching feature grant."""
    entitlement = ROUTE_ENTITLEMENTS.get(request.url.path)
    if entitlement is None:
        raise HTTPException(
            status_code=403,
            detail="This product feature is not enabled for the account.",
        )
    try:
        allowed = store.has_subscriber_entitlement(
            session["identity_id"], entitlement
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Account authorization is temporarily unavailable.",
        ) from None
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="This product feature is not included in the account's current access.",
        )
