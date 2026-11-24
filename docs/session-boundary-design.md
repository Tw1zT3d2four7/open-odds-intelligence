# HTTP Session and CSRF Boundary

**Status:** Isolated helpers implemented; not wired into FastAPI routes or middleware.  
**Branch:** `feature/admin-panel-and-subscriptions`

## Session cookies

The helper module `app/auth.py` defines two non-interchangeable session cookies:

- `__Host-ooi_subscriber` for subscriber sessions.
- `__Host-ooi_admin` for administrator sessions.

Both are host-only, `Secure`, `HttpOnly`, `Path=/`, and `SameSite=Lax`. No `Domain` attribute is set. The `__Host-` prefix requires HTTPS, `Path=/`, and no Domain attribute in supporting browsers. Production must terminate TLS at the trusted reverse proxy. Do not weaken cookie security to accommodate plain-HTTP production access.

The session resolver reads only the cookie for the requested account class and independently verifies the returned session's `account_type`. A subscriber cookie cannot satisfy an administrator dependency, and vice versa. The underlying IdentityStore continues to store only a digest of the opaque session token.

The cookie lifetime must not exceed the server-side session lifetime. Logout must revoke the server-side session and expire only the relevant account-class cookie. Admin login must continue to complete the existing MFA proof step before `IdentityStore.create_session` can succeed.

## CSRF boundary

State-changing browser endpoints must call `require_same_origin_csrf` before performing work. It requires:

1. A syntactically valid HTTP(S) Origin.
2. An exact case-insensitive match between the Origin authority and request Host.
3. A host-only `__Host-ooi_csrf` cookie.
4. A matching `X-Ooi-Csrf-Token` request header, compared in constant time.

The CSRF cookie is Secure, host-only, Path=/, SameSite=Strict, and readable by same-origin JavaScript; it is not an authentication credential. The token must never be placed in URLs or logs. Issue/rotate it on login-page/bootstrap flows and rotate it after authentication transitions. Expire it on logout.

The reverse proxy must preserve the original Host header and must not allow untrusted clients to spoof forwarded host information. Do not add permissive cross-origin exceptions to make this check pass. Browser state-changing requests should send Origin; non-browser service-to-service endpoints require a separate, explicit authentication design rather than bypassing this control.

## Current scope and limitations

- Helpers are unit-tested in isolation.
- No endpoint, middleware, dashboard, Basic Auth behavior, or production deployment has been changed.
- No login, registration, verification, reset, or admin routes are wired yet.
- The helpers are not a complete authentication system by themselves.
- Route dependencies, cookie issuance/revocation, rate limiting, login CSRF coverage, trusted-proxy behavior, and full route-level authorization still require integration tests before activation.
