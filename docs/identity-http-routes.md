# Isolated Identity HTTP Router

**Implementation:** `app/identity_routes.py`  
**Tests:** `tests/test_identity_routes.py`, `tests/test_route_policy.py`, `tests/test_route_authorization.py`  
**Status:** Implemented and CI-verified in the current `main`/`development` baseline as of 2026-11-27. Registered in FastAPI but exposed only when `AUTH_MODE=identity`; the default remains `legacy_basic`.  
**Production impact:** No behavior change in default `legacy_basic` mode. In staged `identity` mode, subscriber and administrator authentication, protected account/admin workflows, and product-route entitlement enforcement are active. Identity mode is still opt-in and is not approved for public production cutover.

## Route inventory

| Method | Path | Target access class | Behavior |
|---|---|---|---|
| GET | `/login` | Public safe | Subscriber sign-in page. |
| GET | `/register` | Public safe | Subscriber registration page. |
| GET | `/admin/login` | Public safe | Separate private administrator sign-in page. |
| GET | `/account` | Subscriber only | Protected subscriber account overview; requires an active, verified subscriber session. |
| GET | `/auth/account` | Subscriber only | Returns only the signed-in subscriber's email, active/verified state, and currently active feature grants. |
| GET | `/admin` | Administrator only | Separate no-store Admin Console shell; requires an active MFA-authenticated administrator session. |
| GET | `/admin/overview` | Administrator only | No-store, sanitized operational metrics from the shared odds service; provider key material and raw errors are never returned. |
| GET | `/admin/audit-events` | Administrator only | Bounded, paginated, no-store security audit history with event, actor, and subject filters; excludes account email addresses and credential material. |
| GET | `/admin/sessions` | Administrator only | Bounded active-session inventory; exposes session metadata only, never bearer tokens, token hashes, or subscriber email addresses. |
| POST | `/admin/sessions/{session_id}/revoke` | Administrator + CSRF | Revokes a selected session with a controlled reason code and MFA-admin audit attribution; the current administrator session cannot revoke itself. |
| GET | `/admin/subscribers` | Administrator only | Bounded, paginated, searchable, no-store subscriber directory with minimal account and active-entitlement fields plus complimentary campaign/expiry metadata and an active complimentary-access filter. |
| POST | `/admin/subscribers/complimentary` | Administrator + CSRF | Creates a no-payment complimentary subscriber, time-limited feature grants, a hashed 30-minute activation challenge, and a secret-free actor-attributed audit event; queues an encrypted activation email. |
| PUT | `/admin/subscribers/complimentary` | Administrator + CSRF | Adds a bounded number of days to an existing complimentary grant and replaces its feature selection; records previous/new expiry, campaign, reason, and MFA administrator actor. |
| GET | `/forgot-password` | Public safe | Password-recovery request page. |
| GET | `/verify-email` | Public safe | Reads the challenge token from the URL fragment and submits it in the request body. |
| GET | `/reset-password` | Public safe | Reads the recovery token from the URL fragment and submits it in the request body. |
| GET | `/auth/csrf` | Public safe | Issues the double-submit CSRF cookie. |
| POST | `/auth/register` | Public + CSRF | Creates an unverified subscriber and queues an encrypted verification email for asynchronous delivery. |
| POST | `/auth/verification/resend` | Public + CSRF | Issues a replacement verification challenge when an active unverified subscriber exists. |
| POST | `/auth/verify-email` | Public + CSRF | Consumes a one-time email verification challenge. |
| POST | `/auth/password-reset/request` | Public + CSRF | Requests a recovery email with a generic response. |
| POST | `/auth/password-reset/consume` | Public + CSRF | Consumes a one-time recovery challenge and revokes all account sessions. |
| POST | `/auth/password/change` | Subscriber + CSRF | Requires the current password; atomically updates the password, invalidates outstanding reset links, revokes all sessions, clears local cookies, and records a secret-free self-attributed audit event. |
| POST | `/auth/login` | Public + CSRF | Subscriber password login; verified email is required by the identity store. |
| POST | `/auth/logout` | Subscriber + CSRF | Revokes only the subscriber session and clears its cookie. |
| POST | `/admin/auth/login` | Public + CSRF | Administrator password plus TOTP or single-use recovery code. |
| POST | `/admin/auth/logout` | Administrator + CSRF | Revokes only the administrator session and clears its cookie. |
| POST | `/admin/subscribers/{identity_id}/status` | Administrator + CSRF | Suspends/reactivates a subscriber with a controlled reason code; suspension revokes active sessions in the same transaction and records the MFA administrator actor. |
| POST | `/admin/subscribers/{identity_id}/sessions/revoke` | Administrator + CSRF | Revokes all active sessions for one subscriber atomically, with a controlled reason code and actor-attributed audit event. |
| GET | `/admin/subscriber-entitlements` | Administrator only | Reads feature grants for one subscriber identity; never returns account email. |
| PUT | `/admin/subscriber-entitlements` | Administrator + CSRF | Replaces a subscriber's feature grants; requires an active MFA-backed administrator session and records an audit event. |

Public registration can create only a subscriber. Administrator provisioning of administrators remains exclusively in the private interactive bootstrap command. The separate complimentary-subscriber workflow is available only to an active MFA administrator and never creates a Stripe customer, payment method, Checkout Session, invoice, or paid subscription.

## Security behavior

- All state-changing routes require the HTTPS same-origin double-submit CSRF cookie/header pair.
- Subscriber and administrator sessions use separate host-only Secure cookies. Cross-class cookies cannot satisfy the other account type's dependencies.
- Successful login revokes any prior cookie session in that account-class slot before issuing a fresh opaque session.
- Subscriber password changes require the current password and CSRF proof, are rate-limited by IP and internal identity ID, invalidate outstanding password-reset links, atomically update the Argon2 hash and revoke every active session, then clear the current session and CSRF cookies. The audit event contains no password or email.
- Administrator sessions are created only after a fresh TOTP or single-use recovery code is accepted. The identity store consumes the MFA proof atomically with session creation.
- Registration, login, verification resend, and password-reset request responses avoid disclosing whether an account exists or is verified.
- Rate limits use atomic persistent IP and account/destination buckets. Verification and password-reset email requests include a 60-second per-destination cooldown and a daily cap. Bucket values are HMAC-hashed before persistence.
- Forwarded client-IP headers are ignored until trusted proxy CIDRs are explicitly configured after deployment topology review.
- Email links carry challenge tokens in the URL fragment (`#token=...`), which browsers do not send in HTTP requests or referrer headers. The page must read the fragment and POST the token in the request body.
- Complimentary subscriber activation uses the existing encrypted password-reset queue with a dedicated activation email and a 30-minute single-use challenge. Setting a password through that link proves mailbox control and marks the subscriber email verified; the initial random password is never disclosed. The challenge digest, not the token, is stored outside the encrypted email payload.
- Complimentary grants are local expiring feature entitlements. The grant/extension APIs require MFA administrator authorization, CSRF, bounded rate limits, controlled reason codes, and audit attribution. Manual feature editing cannot remove or extend a complimentary expiry; use the dedicated extension endpoint.
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

## Current activation boundary

The router is registered in FastAPI. Middleware resolves concrete paths and HTTP methods to registered route templates before policy lookup, so parameterized subscriber/session endpoints are classified by their explicit manifest entries. It hides identity paths with 404 in `legacy_basic` and admits the classified identity endpoints in staged `identity` mode. In identity mode, all product routes use explicit session and entitlement dependencies and the public health response is minimal. A separate `IDENTITY_ROUTE_ACCESS_POLICY` classifies all 38 routes, and dependency-conformance tests verify that the router's actual security dependencies match that policy. Subscriber and administrator authentication dependencies set `Cache-Control: no-store` and `Pragma: no-cache` before session or CSRF validation, so rejected authentication responses are non-cacheable as well as successful responses.

Verification and password-reset requests now enqueue messages in the versioned identity database. Recipient addresses and challenge links are encrypted with a dedicated Fernet key. A background worker claims messages with atomic leases, reuses stable Resend idempotency keys, retries only transient failures with bounded backoff, and discards messages whose one-time challenge has expired or already been consumed. The worker runs only in identity mode. Tests use a fake sender and never contact Resend.

Before enabling subscriber access publicly:

1. Keep the combined product/identity route manifest complete and verify no route is unclassified or stale.
2. Complete the route-by-route review of all 21 product routes, especially manual refresh and simulation mutations.
3. Define plan bundles, limits, trials, and billing lifecycle separately; the current feature-grant API is a secure foundation, not a commercial plan system.
4. Test the exact Caddy-to-app peer topology and configure only the narrow trusted proxy CIDR.
5. Run the complete non-production flow with a verified sender domain, dedicated sending-only API key, queue encryption key, and operator-controlled mailbox.
6. Complete queue recovery, backup/restore, and key-rotation tests before enabling public registration.
7. Verify the existing dashboard, odds history, scheduled provider work, and alerts against a preserved test database, then complete the documented rollback drill.

The default `legacy_basic` mode remains the production compatibility boundary and hides identity routes. Selecting `AUTH_MODE=identity` exposes authentication, the protected Admin Console, subscriber directory, audited account lifecycle, audit history, and entitlement endpoints, and activates subscriber/admin session checks on product routes; subscriber product access remains default-deny until an MFA administrator grants the required feature keys. This staged mode is not a public launch or production cutover. No deployment or mode switch has been performed.


## Admin Console foundation

The first Phase 2 deliverable is a distinct `/admin` workspace shell. It is served separately from the subscriber dashboard and is protected directly by the administrator session dependency. Subscriber cookies, missing sessions, and expired/revoked sessions cannot open it. The page is returned with `Cache-Control: no-store`, a restrictive same-origin Content Security Policy, frame denial, and no-referrer policy.

The shell establishes separate navigation for Overview, Users, Feature Access, Billing, Notifications, Platform, System Configuration, Communications, and Security. Overview displays live, allowlisted operational metrics through `GET /admin/overview`; provider key details and raw provider errors are never exposed. Users provides a searchable, paginated subscriber directory, guarded suspend/reactivate controls, and subscriber-wide session revocation. Feature Access manages manual entitlement grants and optional expiry through the existing MFA-admin and CSRF-protected API; it does not represent billing or commercial subscription plans. Security provides paginated audit history with event-type, actor-identity, and subject-identity filters, active-session inventory, and individual session revocation. Sections without completed protected APIs remain explicitly marked as planned; no operational metrics are fabricated. Administrator logout uses the existing CSRF-protected endpoint.

### Subscriber account overview

The subscriber sign-in flow now redirects to `/account`. The protected, read-only account overview displays the signed-in email address, active account and email-verification state, and current feature grants through `GET /auth/account`. Both the page and summary API require a subscriber session; administrator cookies cannot authorize them. The page uses a fresh per-response CSP script nonce and no-store headers. It does not imply a paid plan, billing status, or entitlement bundle. The account page supports current-password-confirmed password changes; profile, email-change, and notification-preference mutations remain unavailable pending their own reviewed designs. The shell is available only in staged `AUTH_MODE=identity`; the default `legacy_basic` experience is unchanged.


### Administrator subscriber directory

- `GET /admin/subscribers` — administrator-only, MFA-backed, bounded read-only subscriber directory. Supports `limit` (1–100), `offset`, and a bounded `email_query`. Returns identity ID, email, active/verification state, creation time, and currently active entitlement keys only. Password hashes, sessions, recovery/verification challenges, and credential material are never returned. Responses are marked no-store.


### Administrator account lifecycle

- `POST /admin/subscribers/{identity_id}/status` — requires an active MFA-backed administrator session and the double-submit CSRF token. Accepts only a boolean active state and one of the controlled reason codes `support_request`, `policy_violation`, `security_concern`, `billing_issue`, or `other`; free-form text is not written into audit metadata. Suspension and revocation of all active subscriber sessions occur atomically. Reactivation does not verify email or change entitlements. Both state changes are actor-attributed audit events.

### Administrator audit and feature access

- `GET /admin/audit-events` — administrator-only, bounded and paginated; supports event type, actor identity ID, and subject identity ID filters. No email addresses or credential material are joined into the response.
- `GET /admin/subscriber-entitlements` and `PUT /admin/subscriber-entitlements` — administrator-only read and CSRF-protected mutation. Responses are no-store. The write replaces the subscriber's complete active grant set, optionally assigning one expiration to all selected grants; each update is attributed to the MFA administrator in the security audit history.
