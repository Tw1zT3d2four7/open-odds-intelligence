"""Fail-closed dependency assembly for the future identity route integration.

This module deliberately does not attach dependencies to live routes. Callers
must provide a real server-side entitlement dependency for subscriber product
routes; there is no permissive default.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import Depends

from .dependencies import (
    require_administrator,
    require_authenticated_csrf,
    require_subscriber,
)
from .route_policy import ROUTE_ACCESS_POLICY, classify_route

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def dependencies_for_route(
    method: str,
    path: str,
    *,
    entitlement_dependency: Callable | None = None,
    policy: Mapping[tuple[str, str], str] = ROUTE_ACCESS_POLICY,
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
        dependencies = [Depends(require_administrator)]
    elif access_class == "subscriber_only":
        dependencies = [Depends(require_subscriber)]
    elif access_class == "subscriber_entitled":
        if entitlement_dependency is None or not callable(entitlement_dependency):
            raise RuntimeError(
                "A server-side entitlement dependency is required for subscriber routes."
            )
        dependencies = [
            Depends(require_subscriber),
            Depends(entitlement_dependency),
        ]
    else:
        raise ValueError("Route access policy is not supported.")

    if mutating:
        dependencies.append(Depends(require_authenticated_csrf))
    return dependencies
