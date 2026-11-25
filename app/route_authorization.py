"""Fail-closed dependency assembly for product and identity HTTP routes.

Product APIRoutes use mode-aware dependencies that preserve legacy Basic Auth
behavior while enforcing sessions, entitlements, administrator access, and
CSRF in identity mode. No subscriber route has a permissive entitlement default.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import Depends

from .dependencies import (
    require_administrator,
    require_authenticated_csrf,
    require_subscriber,
)
from .route_policy import classify_route

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def dependencies_for_route(
    method: str,
    path: str,
    *,
    entitlement_dependency: Callable | None = None,
    policy: Mapping[tuple[str, str], str] | None = None,
    subscriber_dependency: Callable = require_subscriber,
    administrator_dependency: Callable = require_administrator,
    csrf_dependency: Callable = require_authenticated_csrf,
) -> list[Depends]:
    """Return the complete dependency list for a classified route.

    Unknown routes and subscriber product routes without an explicit entitlement
    dependency fail closed. Public mutation routes must use the explicit
    public_csrf class; all state-changing authenticated routes include the
    same-origin CSRF guard.
    """
    normalized_method = method.upper()
    access_class = classify_route(normalized_method, path, policy)
    if access_class is None:
        raise ValueError("Route has no explicit access policy.")

    # Treat every method outside the HTTP safe-method set as state-changing;
    # do not let uncommon/custom verbs bypass CSRF protection.
    mutating = normalized_method not in _SAFE_METHODS
    if access_class == "public_minimal":
        if mutating:
            raise ValueError("Public routes cannot use state-changing methods.")
        return []
    if access_class == "public_csrf":
        if not mutating:
            raise ValueError("Public CSRF routes must use state-changing methods.")
        return [Depends(require_authenticated_csrf)]

    if access_class == "administrator_only":
        dependencies = [Depends(administrator_dependency)]
    elif access_class == "subscriber_only":
        dependencies = [Depends(subscriber_dependency)]
    elif access_class == "subscriber_entitled":
        if entitlement_dependency is None or not callable(entitlement_dependency):
            raise RuntimeError(
                "A server-side entitlement dependency is required for subscriber routes."
            )
        dependencies = [
            Depends(subscriber_dependency),
            Depends(entitlement_dependency),
        ]
    else:
        raise ValueError("Route access policy is not supported.")

    if mutating:
        dependencies.append(Depends(csrf_dependency))
    return dependencies
