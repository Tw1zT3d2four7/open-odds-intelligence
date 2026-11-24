"""Explicit target access policy for every existing HTTP endpoint.

This is an inventory/guardrail, not active middleware. The legacy Basic Auth
middleware remains the only live HTTP authentication boundary until the
identity-mode cutover is separately reviewed and approved.
"""
from __future__ import annotations

from collections.abc import Iterable

from starlette.routing import BaseRoute


# Values are deliberately descriptive policy classes, not authorization
# functions. Route wiring will happen only after migration and compatibility
# behavior is covered by HTTP integration tests.
ROUTE_ACCESS_POLICY: dict[tuple[str, str], str] = {
    ("GET", "/"): "subscriber_entitled",
    ("GET", "/api/health"): "public_minimal",
    # FastAPI's built-in schema and interactive docs are not APIRoute objects,
    # but must still receive an explicit access policy during identity cutover.
    ("GET", "/openapi.json"): "administrator_only",
    ("GET", "/docs"): "administrator_only",
    ("GET", "/docs/oauth2-redirect"): "administrator_only",
    ("GET", "/redoc"): "administrator_only",
    ("POST", "/api/limits/refresh"): "administrator_only",
    ("GET", "/api/limits"): "subscriber_entitled",
    ("GET", "/api/limits/changes"): "subscriber_entitled",
    ("GET", "/api/limits/history"): "subscriber_entitled",
    ("GET", "/api/odds"): "subscriber_entitled",
    ("POST", "/api/refresh"): "administrator_only",
    ("GET", "/api/market-board"): "subscriber_entitled",
    ("GET", "/api/bookmaker-board"): "subscriber_entitled",
    ("GET", "/api/simulation/leagues"): "subscriber_entitled",
    ("GET", "/api/simulation/games"): "subscriber_entitled",
    ("POST", "/api/simulation/games/refresh"): "administrator_only",
    ("POST", "/api/simulation/run"): "subscriber_entitled",
    ("GET", "/api/player-props"): "subscriber_entitled",
    ("GET", "/api/clv"): "subscriber_entitled",
    ("GET", "/api/movement"): "subscriber_entitled",
    ("GET", "/api/movement/history"): "subscriber_entitled",
    ("GET", "/api/history"): "subscriber_entitled",
    ("GET", "/api/opportunities"): "subscriber_entitled",
    ("GET", "/api/arbitrage"): "subscriber_entitled",
}


def route_key(method: str, path: str) -> tuple[str, str]:
    """Normalize an HTTP method/path pair for policy lookup."""
    return method.upper(), path


def classify_route(method: str, path: str) -> str | None:
    """Return the declared target class, or None for an unclassified route.

    HEAD requests inherit the corresponding GET policy so they cannot bypass
    authentication merely because Starlette handles HEAD through a GET route.
    """
    normalized_method = method.upper()
    if normalized_method == "HEAD":
        return ROUTE_ACCESS_POLICY.get(("GET", path))
    return ROUTE_ACCESS_POLICY.get((normalized_method, path))


def unclassified_routes(routes: Iterable[BaseRoute]) -> list[tuple[str, str]]:
    """List application routes without an explicit policy entry."""
    missing = {
        route_key(method, route.path)
        for route in routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"HEAD", "OPTIONS"}
        and classify_route(method, route.path) is None
    }
    return sorted(missing)


def stale_policy_entries(routes: Iterable[BaseRoute]) -> list[tuple[str, str]]:
    """List policy entries that no longer correspond to a live application route."""
    actual = {
        route_key(method, route.path)
        for route in routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    return sorted(set(ROUTE_ACCESS_POLICY) - actual)
