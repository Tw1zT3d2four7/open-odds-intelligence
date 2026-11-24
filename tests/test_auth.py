from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException, Request, Response

from app.auth import (
    ADMINISTRATOR_COOKIE,
    CSRF_COOKIE,
    SUBSCRIBER_COOKIE,
    clear_session_cookie,
    issue_csrf_cookie,
    require_same_origin_csrf,
    resolve_session,
    set_session_cookie,
)


def make_request(headers=None, cookie=""):
    values = [(b"type", b"http"), (b"method", b"POST"), (b"path", b"/api/test"),
              (b"headers", b""), (b"scheme", b"https")]
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": "/api/test", "raw_path": b"/api/test",
        "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
                   + ([(b"cookie", cookie.encode())] if cookie else []),
        "server": ("example.com", 443), "client": ("127.0.0.1", 1234),
    }
    return Request(scope)


def test_session_cookies_are_separate_secure_httponly_host_cookies():
    response = Response()
    set_session_cookie(response, "opaque-subscriber-token", "subscriber")
    set_session_cookie(response, "opaque-admin-token", "administrator")
    headers = response.headers.getlist("set-cookie")
    assert any(SUBSCRIBER_COOKIE in item and "HttpOnly" in item and "Secure" in item for item in headers)
    assert any(ADMINISTRATOR_COOKIE in item and "HttpOnly" in item and "Secure" in item for item in headers)
    assert all("Path=/" in item and "SameSite=lax" in item for item in headers)
    assert all("Domain=" not in item for item in headers)


@pytest.mark.parametrize("account_type", ["root", "admin", "", None])
def test_cookie_helpers_reject_unknown_account_classes(account_type):
    with pytest.raises(ValueError):
        set_session_cookie(Response(), "token", account_type)


def test_cookie_helper_rejects_invalid_lifetime_and_token():
    with pytest.raises(ValueError):
        set_session_cookie(Response(), "token", "subscriber", 0)
    with pytest.raises(ValueError):
        set_session_cookie(Response(), "", "subscriber")


def test_session_resolution_uses_only_matching_cookie():
    class Store:
        def get_session(self, token):
            return {"identity_id": "id-1", "account_type": "subscriber"} if token == "sub-token" else None

    request = make_request(cookie=f"{SUBSCRIBER_COOKIE}=sub-token")
    assert resolve_session(request, Store(), "subscriber")["identity_id"] == "id-1"
    with pytest.raises(HTTPException) as error:
        resolve_session(request, Store(), "administrator")
    assert error.value.status_code == 401


def test_session_resolution_rejects_cross_class_session_even_if_store_returns_it():
    class Store:
        def get_session(self, token):
            return {"identity_id": "id-1", "account_type": "administrator"}

    request = make_request(cookie=f"{SUBSCRIBER_COOKIE}=admin-token")
    with pytest.raises(HTTPException) as error:
        resolve_session(request, Store(), "subscriber")
    assert error.value.status_code == 401


def test_csrf_cookie_is_secure_strict_and_not_httponly():
    response = Response()
    token = issue_csrf_cookie(response)
    cookie = response.headers.get("set-cookie")
    assert token
    assert CSRF_COOKIE in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "HttpOnly" not in cookie


def test_same_origin_csrf_accepts_matching_origin_cookie_and_header():
    token = "csrf-random-token"
    request = make_request(
        headers={"host": "example.com", "origin": "https://example.com",
                 "x-ooi-csrf-token": token},
        cookie=f"{CSRF_COOKIE}={token}",
    )
    require_same_origin_csrf(request)


@pytest.mark.parametrize("origin", [
    "https://attacker.example", "null", "https://example.com.attacker.example",
    "https://user@example.com", "https://example.com/path",
])
def test_same_origin_csrf_rejects_untrusted_origins(origin):
    request = make_request(
        headers={"host": "example.com", "origin": origin,
                 "x-ooi-csrf-token": "same"},
        cookie=f"{CSRF_COOKIE}=same",
    )
    with pytest.raises(HTTPException) as error:
        require_same_origin_csrf(request)
    assert error.value.status_code == 403


def test_same_origin_csrf_requires_header_and_cookie_match():
    request = make_request(
        headers={"host": "example.com", "origin": "https://example.com",
                 "x-ooi-csrf-token": "wrong"},
        cookie=f"{CSRF_COOKIE}=right",
    )
    with pytest.raises(HTTPException) as error:
        require_same_origin_csrf(request)
    assert error.value.status_code == 403
