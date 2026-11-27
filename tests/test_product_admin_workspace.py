from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import entitlements


def _request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(identity_store=object())),
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/odds"),
    )


def test_mfa_administrator_can_use_product_routes_without_subscriber_entitlements(monkeypatch):
    monkeypatch.setattr(entitlements.config, "AUTH_MODE", "identity")
    administrator = {"account_type": "administrator", "identity_id": "admin-1"}
    monkeypatch.setattr(
        entitlements, "require_administrator",
        lambda request, store: administrator,
    )
    monkeypatch.setattr(
        entitlements, "require_subscriber",
        lambda request, store: pytest.fail("administrator must not need a subscriber session"),
    )
    request = _request()

    session = entitlements.require_product_subscriber(request)
    assert session is administrator
    assert request.state.include_premium_data is True

    monkeypatch.setattr(
        entitlements, "get_identity_store",
        lambda request: pytest.fail("administrator entitlement bypass must not query subscriber grants"),
    )
    entitlements.require_product_entitlement(request, session=session)
    assert request.state.include_premium_data is True


def test_subscriber_still_requires_subscriber_session_when_no_admin_session(monkeypatch):
    monkeypatch.setattr(entitlements.config, "AUTH_MODE", "identity")
    subscriber = {"account_type": "subscriber", "identity_id": "sub-1"}

    def no_admin(request, store):
        raise HTTPException(status_code=401, detail="Authentication required.")

    monkeypatch.setattr(entitlements, "require_administrator", no_admin)
    monkeypatch.setattr(
        entitlements, "require_subscriber",
        lambda request, store: subscriber,
    )
    request = _request()

    assert entitlements.require_product_subscriber(request) is subscriber


def test_admin_workspace_is_registered_as_admin_only():
    from app import main
    from app.route_policy import classify_route

    assert classify_route("GET", "/admin/workspace") == "administrator_only"
    route = next(
        route for route in main.app.routes
        if getattr(route, "path", None) == "/admin/workspace"
    )
    assert route.endpoint is main.administrator_sports_workspace
