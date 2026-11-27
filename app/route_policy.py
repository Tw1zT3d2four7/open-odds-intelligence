"""Explicit access policy for product and identity HTTP endpoints.

Product APIRoutes use this policy to assemble mode-aware dependencies. The
identity-mode middleware admits only classified routes; legacy Basic Auth
behavior remains the default compatibility boundary.
"""
from __future__ import annotations

from collections.abc import Iterable

from starlette.routing import BaseRoute


# Values are descriptive policy classes. They are translated into concrete
# FastAPI dependencies by route_authorization.py and protected by the identity
# mode gate; they are never treated as authorization by themselves.
ROUTE_ACCESS_POLICY: dict[tuple[str, str], str] = {
    ("GET", "/"): "public_minimal",
    ("GET", "/workspace"): "subscriber_entitled",
    # Dedicated full-workspace entry point for MFA-authenticated administrators.
    ("GET", "/admin/workspace"): "administrator_only",
    ("GET", "/api/health"): "public_minimal",
    ("GET", "/api/workspace/status"): "subscriber_entitled",
    # Public transport endpoint, but the handler requires a valid Stripe HMAC signature.
    ("POST", "/webhooks/stripe"): "stripe_signature",
    # FastAPI's built-in schema and interactive docs are not APIRoute objects,
    # but must still receive an explicit access policy during identity cutover.
    ("GET", "/openapi.json"): "administrator_only",
    ("GET", "/docs"): "administrator_only",
    ("GET", "/docs/oauth2-redirect"): "administrator_only",
    ("GET", "/redoc"): "administrator_only",
    ("POST", "/api/limits/refresh"): "subscriber_entitled",
    ("GET", "/api/limits"): "subscriber_entitled",
    ("GET", "/api/limits/changes"): "subscriber_entitled",
    ("GET", "/api/limits/history"): "subscriber_entitled",
    ("GET", "/api/odds"): "subscriber_entitled",
    ("POST", "/api/refresh"): "subscriber_entitled",
    ("GET", "/api/market-board"): "subscriber_entitled",
    ("GET", "/api/bookmaker-board"): "subscriber_entitled",
    ("GET", "/api/simulation/leagues"): "subscriber_entitled",
    ("GET", "/api/simulation/games"): "subscriber_entitled",
    ("POST", "/api/simulation/games/refresh"): "subscriber_entitled",
    ("POST", "/api/simulation/run"): "subscriber_entitled",
    ("GET", "/api/player-props"): "subscriber_entitled",
    ("GET", "/api/clv"): "subscriber_entitled",
    ("GET", "/api/movement"): "subscriber_entitled",
    ("GET", "/api/movement/history"): "subscriber_entitled",
    ("GET", "/api/history"): "subscriber_entitled",
    ("GET", "/api/opportunities"): "subscriber_entitled",
    ("GET", "/api/arbitrage"): "subscriber_entitled",
}

# Identity endpoints have their own explicit policy and are admitted only by
# the AUTH_MODE=identity middleware allowlist. Product route dependencies remain
# a separate integration milestone.
IDENTITY_ROUTE_ACCESS_POLICY: dict[tuple[str, str], str] = {
    # Static authentication entry points are public safe reads; mutations
    # remain protected by the dedicated public_csrf class below.
    ("GET", "/login"): "public_minimal",
    ("GET", "/register"): "public_minimal",
    ("GET", "/admin/login"): "public_minimal",
    ("GET", "/account"): "subscriber_only",
    ("GET", "/auth/account"): "subscriber_only",
    ("GET", "/auth/billing"): "subscriber_only",
    ("POST", "/auth/billing/checkout"): "subscriber_only",
    ("POST", "/auth/billing/portal"): "subscriber_only",
    ("GET", "/admin"): "administrator_only",
    ("GET", "/admin/overview"): "administrator_only",
    ("GET", "/admin/workspace/status"): "administrator_only",
    ("GET", "/admin/authentication-policy"): "administrator_only",
    ("GET", "/forgot-password"): "public_minimal",
    ("GET", "/verify-email"): "public_minimal",
    ("GET", "/reset-password"): "public_minimal",
    ("GET", "/auth/csrf"): "public_minimal",
    ("POST", "/auth/register"): "public_csrf",
    ("POST", "/auth/verification/resend"): "public_csrf",
    ("POST", "/auth/verify-email"): "public_csrf",
    ("POST", "/auth/password-reset/request"): "public_csrf",
    ("POST", "/auth/password-reset/consume"): "public_csrf",
    ("POST", "/auth/password/change"): "subscriber_only",
    ("GET", "/auth/sessions"): "subscriber_only",
    ("POST", "/auth/sessions/{session_id}/revoke"): "subscriber_only",
    ("POST", "/auth/sessions/revoke-all"): "subscriber_only",
    ("POST", "/auth/login"): "public_csrf",
    ("POST", "/auth/logout"): "subscriber_only",
    ("POST", "/admin/auth/login"): "public_csrf",
    ("POST", "/admin/auth/logout"): "administrator_only",
    ("GET", "/admin/audit-events"): "administrator_only",
    ("GET", "/admin/sessions"): "administrator_only",
    ("POST", "/admin/sessions/{session_id}/revoke"): "administrator_only",
    ("GET", "/admin/subscribers"): "administrator_only",
    ("POST", "/admin/subscribers/complimentary"): "administrator_only",
    ("PUT", "/admin/subscribers/complimentary"): "administrator_only",
    ("GET", "/admin/billing"): "administrator_only",
    ("GET", "/admin/notifications"): "administrator_only",
    ("POST", "/admin/subscribers/{identity_id}/status"): "administrator_only",
    ("POST", "/admin/subscribers/{identity_id}/sessions/revoke"): "administrator_only",
    ("GET", "/admin/subscriber-entitlements"): "administrator_only",
    ("PUT", "/admin/subscriber-entitlements"): "administrator_only",
}



def application_route_access_policy() -> dict[tuple[str, str], str]:
    """Return the current combined policy, including separately managed auth routes."""
    return {**ROUTE_ACCESS_POLICY, **IDENTITY_ROUTE_ACCESS_POLICY}


def route_key(method: str, path: str) -> tuple[str, str]:
    """Normalize an HTTP method/path pair for policy lookup."""
    return method.upper(), path


def classify_route(
    method: str,
    path: str,
    policy: dict[tuple[str, str], str] | None = None,
) -> str | None:
    """Return the declared target class, or None for an unclassified route.

    HEAD requests inherit the corresponding GET policy so they cannot bypass
    authentication merely because Starlette handles HEAD through a GET route.
    """
    policy = application_route_access_policy() if policy is None else policy
    normalized_method = method.upper()
    if normalized_method == "HEAD":
        return policy.get(("GET", path))
    return policy.get((normalized_method, path))


def unclassified_routes(
    routes: Iterable[BaseRoute],
    policy: dict[tuple[str, str], str] | None = None,
) -> list[tuple[str, str]]:
    """List application routes without an explicit policy entry."""
    policy = application_route_access_policy() if policy is None else policy
    missing = {
        route_key(method, route.path)
        for route in routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"HEAD", "OPTIONS"}
        and classify_route(method, route.path, policy) is None
    }
    return sorted(missing)


def stale_policy_entries(
    routes: Iterable[BaseRoute],
    policy: dict[tuple[str, str], str] | None = None,
) -> list[tuple[str, str]]:
    """List policy entries that no longer correspond to a live application route."""
    policy = application_route_access_policy() if policy is None else policy
    actual = {
        route_key(method, route.path)
        for route in routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    return sorted(set(policy) - actual)


def matched_route_path(scope: dict, routes: Iterable[BaseRoute]) -> str | None:
    """Resolve a concrete ASGI request path to its registered route template.

    Middleware runs before Starlette attaches scope["route"]. Use the route's
    compiled path expression and declared methods so parameterized endpoints
    resolve without depending on router dispatch having already run.
    """
    path = scope.get("path", "")
    method = str(scope.get("method", "GET")).upper()
    for route in routes:
        pattern = getattr(route, "path_regex", None)
        route_path = getattr(route, "path", None)
        if pattern is None or route_path is None or pattern.match(path) is None:
            continue
        methods = getattr(route, "methods", None)
        if not methods or method in methods or (method == "HEAD" and "GET" in methods):
            return route_path
    return None
