# Authentication Environment Profiles

**Status:** Implementation specification and staged policy boundary in the current `main`/`development` baseline; not a production switch. Reviewed 2026-11-27.  
**Scope:** Centralized control of subscriber and administrator authentication, verification, MFA, rate limiting, and transactional email for local development, staging, and production.

## Goals

The application must be testable on a self-hosted development instance without purchasing or configuring external email services. Authentication controls must be explicit, independent where appropriate, visible in the protected Admin Console, and validated before changes are applied.

## Profiles

- **development** — local-only testing. May use a local email outbox and explicitly selected test-only authentication relaxations. It must refuse to start when exposed on a non-loopback listener or when the effective public origin is not loopback.
- **staging** — real identity flows with test credentials/providers, email verification, MFA, rate limiting, CSRF, and session protections enabled.
- **production** — secure defaults enforced. Authentication, administrator MFA, CSRF, rate limiting, verified email, and session controls cannot be disabled through the Admin Console.

A profile is selected by deployment configuration, not by an unauthenticated HTTP request. The application must not infer development mode from a Host header, forwarded header, URL parameter, or browser setting.

## Independent controls

The protected configuration surface should expose:

| Control | Development | Staging | Production |
|---|---|---|---|
| Identity authentication | Selectable | Required when identity routes are staged | Required for subscriber service |
| Subscriber registration | On/off | On/off | Operator-controlled |
| Subscriber login | On/off | On/off | On |
| Email verification requirement | Selectable for isolated tests | On | On |
| Email transport | Local outbox / disabled | Resend test or local outbox | Resend |
| Password recovery | Local outbox / disabled | Testable | Enabled |
| Administrator MFA | May use test fixture only | Required | Required |
| Subscriber MFA | Selectable | Selectable | Policy-controlled |
| Rate limiting | Configurable test limits; never silently absent | On | On, mandatory |
| CSRF and same-origin checks | On for browser mutations | On | On, mandatory |
| Session idle/absolute lifetime | Configurable | Configurable | Bounded, mandatory |

## Email modes

- **local_outbox** stores rendered verification and recovery messages in a development-only outbox. The outbox must be inaccessible from non-loopback clients and must never be mounted or enabled in staging/production.
- **disabled** does not send messages. If verification is required, the user-facing flow must explain that delivery is disabled and must not mark the address verified.
- **resend** sends through the existing Resend queue and encrypted payload mechanism.

A local outbox must not expose challenge tokens in application logs, normal public APIs, or the production Admin Console. It should provide a loopback-only development view and must use the existing one-time challenge expiry and consumption logic.

## Fail-closed production invariants

Production startup must fail if any of these invariants are false:

1. Subscriber identity routes cannot be exposed without the identity/session/entitlement route policy.
2. Administrator access requires a password and enrolled TOTP or a single-use recovery code.
3. Email verification is required before subscriber access.
4. CSRF and same-origin checks protect all browser state-changing requests.
5. Authentication rate limiting is enabled and has a stable dedicated HMAC key.
6. Session cookies remain Secure, HttpOnly where applicable, SameSite-restricted, host-only, and revocable.
7. Development bypasses and local outbox are disabled.
8. Configuration changes that weaken a production invariant are rejected before writing the live environment file.

## Admin Console configuration workflow

The Admin Console should have an **Authentication & Security** section under System Configuration. It should display the active profile, effective values, secret presence (never secret contents), and settings requiring restart.

Configuration changes must:
- require an active MFA-authenticated administrator session and CSRF proof;
- validate the complete proposed configuration before saving;
- reject invalid or production-insecure combinations without changing the live file;
- write atomically with restrictive file permissions;
- create a timestamped backup and secret-free audit event;
- identify restart-required changes;
- never return secret values to the browser.

The Admin Console is not a mechanism to switch a public production deployment into development mode. Profile changes require an out-of-band deployment configuration and restart.

## Rollout and acceptance

1. Add typed configuration parsing and profile-specific validation.
2. Implement local outbox delivery and loopback-only access, reusing the existing challenge lifecycle.
3. Apply independent route/feature switches without weakening the production invariants.
4. Add protected read/update APIs and the Admin Console UI.
5. Add tests for every profile, invalid combination, remote access to development-only routes, disabled email, outbox challenge expiry/consumption, and production fail-closed behavior.
6. Verify with the existing identity, route-policy, CSRF, rate-limit, MFA, queue, and billing suites.
7. Keep the live server on its currently approved runtime configuration until the merged baseline has passed isolated non-production acceptance. No production mode change, deployment, or restart is authorized by this specification.

## Current implementation boundary

The initial policy parser and route gates are implemented, but not every proposed control is available yet. Until its complete runtime workflow exists, configuration must fail closed rather than accept a no-op setting:

- Administrator MFA remains mandatory in every profile. `AUTH_ADMIN_MFA_REQUIRED=false` is rejected.
- Subscriber MFA enrollment, verification, recovery, and session enforcement are not implemented. `AUTH_SUBSCRIBER_MFA_REQUIRED=true` is rejected.
- CSRF is always enforced. `AUTH_CSRF_REQUIRED=false` is rejected.
- The protected Admin Console currently displays effective policy values; it does not yet edit deployment environment settings.
- The local outbox is a trusted-terminal development utility, not an HTTP endpoint. Its output contains one-time links and must be treated as secret.
- Staging and production continue to reject the development local-outbox transport.

These restrictions are intentional. A setting is not considered implemented merely because it parses or appears in the policy object; its enforcement, user workflow, audit behavior, and tests must all exist first.
