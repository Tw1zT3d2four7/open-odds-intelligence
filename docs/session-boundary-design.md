# HTTP Session and CSRF Boundary

**Status:** Session and CSRF helpers are integrated into the staged identity router and route dependencies in the current `main`/`development` baseline. Reviewed 2026-11-27.

## Session cookies

The helper module `app/auth.py` defines two non-interchangeable session cookies:

- `__Host-ooi_subscriber` for subscriber sessions.
- `__Host-ooi_admin` for administrator sessions.

Both are host-only, `Secure`, `HttpOnly`, `Path=/`, and `SameSite=Lax`. No `Domain` attribute is set. The `__Host-` prefix requires HTTPS, `Path=/`, and no Domain attribute in supporting browsers. Production must terminate TLS at the trusted reverse proxy. Do not weaken cookie security to accommodate plain-HTTP production access.

The session resolver reads only the cookie for the requested account class and independently verifies the returned session's `account_type`. A subscriber cookie cannot satisfy an administrator dependency, and vice versa. The underlying IdentityStore continues to store only a digest of the opaque session token.

The cookie lifetime must not exceed the server-side session lifetime. Logout must revoke the server-side session and expire only the relevant account-class cookie. Admin login must continue to complete the existing MFA proof step before `IdentityStore.create_session` can succeed.

## CSRF boundary

State-changing browser endpoints must call `require_same_origin_csrf` before performing work. It requires:

1. A syntactically valid HTTPS Origin (HTTP origins are rejected).
2. An exact case-insensitive match between the Origin authority and request Host.
3. A host-only `__Host-ooi_csrf` cookie.
4. A matching `X-Ooi-Csrf-Token` request header, compared in constant time.

The CSRF cookie is Secure, host-only, Path=/, SameSite=Strict, and readable by same-origin JavaScript; it is not an authentication credential. The token must never be placed in URLs or logs. Issue/rotate it on login-page/bootstrap flows and rotate it after authentication transitions. Expire it on logout.

The reverse proxy must preserve the original Host header and must not allow untrusted clients to spoof forwarded host information. Do not add permissive cross-origin exceptions to make this check pass. Browser state-changing requests should send Origin; non-browser service-to-service endpoints require a separate, explicit authentication design rather than bypassing this control.

## Current scope and limitations

- Subscriber and administrator session cookies are issued, resolved, revoked, and cleared through the staged identity routes; the server stores only token digests.
- Same-origin CSRF checks are enforced on cookie-authenticated state-changing routes in identity mode.
- Subscriber and administrator sessions remain non-interchangeable; administrator access requires MFA.
- The default `legacy_basic` mode preserves the existing Basic Auth contract and does not activate the identity router.
- Identity mode is CI-tested but remains opt-in. Reverse-proxy/TLS topology, non-production end-to-end acceptance, backup/restore, key recovery, and rollback must be completed before public production activation.
