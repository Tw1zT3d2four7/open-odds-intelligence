# Isolated Identity HTTP Router

**Implementation:** `app/identity_routes.py`  
**Tests:** `tests/test_identity_routes.py`, `tests/test_route_policy.py`, `tests/test_route_authorization.py`  
**Status:** Implemented, separately policy-classified, and CI-tested. Registered in FastAPI but exposed only when `AUTH_MODE=identity`.  
**Production impact:** No behavior change in default `legacy_basic` mode; identity mode remains authentication-only staging.

## Route inventory

| Method | Path | Target access class | Behavior |
|---|---|---|---|
| GET | `/auth/csrf` | Public safe | Issues the double-submit CSRF cookie. |
| POST | `/auth/register` | Public + CSRF | Creates an unverified subscriber and sends a verification email. |
| POST | `/auth/verification/resend` | Public + CSRF | Issues a replacement verification challenge when an active unverified subscriber exists. |
| POST | `/auth/verify-email` | Public + CSRF | Consumes a one-time email verification challenge. |
| POST | `/auth/password-reset/request` | Public + CSRF | Requests a recovery email with a generic response. |
| POST | `/auth/password-reset/consume` | Public + CSRF | Consumes a one-time recovery challenge and revokes all account sessions. |
| POST | `/auth/login` | Public + CSRF | Subscriber password login; verified email is required by the identity store. |
| POST | `/auth/logout` | Subscriber + CSRF | Revokes only the subscriber session and clears its cookie. |
| POST | `/admin/auth/login` | Public + CSRF | Administrator password plus TOTP or single-use recovery code. |
| POST | `/admin/auth/logout` | Administrator + CSRF | Revokes only the administrator session and clears its cookie. |

Public registration can create only a subscriber. Administrator provisioning remains exclusively in the private interactive bootstrap command.

## Security behavior

- All state-changing routes require the HTTPS same-origin double-submit CSRF cookie/header pair.
- Subscriber and administrator sessions use separate host-only Secure cookies. Cross-class cookies cannot satisfy the other account type's dependencies.
- Successful login revokes any prior cookie session in that account-class slot before issuing a fresh opaque session.
- Administrator sessions are created only after a fresh TOTP or single-use recovery code is accepted. The identity store consumes the MFA proof atomically with session creation.
- Registration, login, verification resend, and password-reset request responses avoid disclosing whether an account exists or is verified.
- Rate limits use atomic persistent IP and account/destination buckets. Verification and password-reset email requests include a 60-second per-destination cooldown and a daily cap. Bucket values are HMAC-hashed before persistence.
- Forwarded client-IP headers are ignored until trusted proxy CIDRs are explicitly configured after deployment topology review.
- Email links carry challenge tokens in the URL fragment (`#token=...`), which browsers do not send in HTTP requests or referrer headers. The page must read the fragment and POST the token in the request body.
- Email provider exceptions are sanitized. The API key is case-preserving and is never returned to the browser or written to audit events.
- Delivery outcomes are recorded in a separate 90-day table with no recipient address or challenge token. A persistent Resend circuit opens after five consecutive failures within ten minutes, remains open for ten minutes, and permits one 60-second half-open probe.

## Rate-limit starting values

| Operation | IP bucket | Account/token bucket |
|---|---:|---:|
| Subscriber login | 20 / 15 min | 8 / 15 min |
| Administrator login (password + MFA) | 8 / 15 min | 5 / 15 min |
| Registration | 10 / hour | 3 / hour |
| Verification resend | 5 / hour | 3 / hour |
| Password-reset request | 10 / hour | 3 / hour |
| Verify-email consume | 10 / hour | 5 / hour |
| Password-reset consume | 10 / hour | 5 / hour |

Email resend operations additionally allow at most one request per destination per 60 seconds and five per destination per 24 hours. These are initial policy values and remain subject to load, provider quota, shared-NAT, and abuse review before public launch.

## Not ready to mount

The router is registered in FastAPI, while middleware hides all identity paths with 404 in `legacy_basic` and exposes only the classified auth paths in staged `identity` mode. In identity mode, all existing product/dashboard routes return 503 and the public health response is minimal. A separate `IDENTITY_ROUTE_ACCESS_POLICY` classifies all ten routes, and dependency-assembly tests verify that the router's actual security dependencies match that policy. Before enabling product access:

1. Keep the combined product/identity route manifest complete and verify no route is unclassified or stale.
2. Attach the route-policy dependency assembly and explicit entitlement checks to all existing subscriber product routes.
3. Add a durable send queue and safe retry workflow. Delivery history and the persistent provider circuit breaker are implemented, but a provider failure currently leaves a valid challenge available only for a later user-requested resend; raw challenge tokens are not retained for background retries.
4. Test the exact Caddy-to-app peer topology and configure only the narrow trusted proxy CIDR.
5. Build subscriber/admin frontend entry points and ensure they bootstrap CSRF before any mutation.
6. Run the complete non-production flow with a verified sender domain and operator-controlled mailbox.
7. Keep `AUTH_MODE=identity` fail-closed until the complete route set is integrated and rollback gates pass.

The default `legacy_basic` mode remains the live compatibility boundary and hides these routes. Selecting `AUTH_MODE=identity` explicitly enables only these authentication endpoints; it does not enable odds/product access and is not a production cutover. No deployment or mode switch has been performed.
