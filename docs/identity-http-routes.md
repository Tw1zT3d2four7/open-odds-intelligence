# Isolated Identity HTTP Router

**Implementation:** `app/identity_routes.py`  
**Tests:** `tests/test_identity_routes.py`  
**Status:** Implemented and CI-tested, but deliberately not mounted by `app.main`.  
**Production impact:** None.

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

The router is intentionally not included in the live FastAPI application. Before mounting it:

1. Add every new route to the explicit route manifest and verify no route is unclassified or stale.
2. Attach the route-policy dependency assembly and explicit entitlement checks to all existing subscriber product routes.
3. Add durable delivery status/retry handling and a provider circuit breaker; a provider failure currently leaves a valid challenge available for a later resend but does not yet create a durable delivery record.
4. Test the exact Caddy-to-app peer topology and configure only the narrow trusted proxy CIDR.
5. Build subscriber/admin frontend entry points and ensure they bootstrap CSRF before any mutation.
6. Run the complete non-production flow with a verified sender domain and operator-controlled mailbox.
7. Keep `AUTH_MODE=identity` fail-closed until the complete route set is integrated and rollback gates pass.

The existing Basic Auth middleware remains the live boundary. No authentication endpoint described here is reachable through the production app until the router is explicitly mounted in a later reviewed change.
