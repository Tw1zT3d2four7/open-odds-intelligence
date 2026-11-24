# Open Odds Intelligence — Product Roadmap

## Project direction

Evolve Open Odds Intelligence from a single-owner, self-hosted sportsbook market monitor into a secure, multi-user subscription platform while preserving the existing odds engine, market intelligence, and operator workflows.

The initial commercial product is a subscriber dashboard with configurable alert delivery. A separate private administrator experience manages accounts, subscriptions, notifications, and platform operations. The subscriber and administrator experiences share one domain but have distinct routes, sessions, authorization boundaries, and navigation.

## Confirmed product decisions

- Subscriber routes: `/login`, `/register`, and `/account`.
- Administrator routes: `/admin/login` and `/admin`; administrator access is private, separately authenticated, and requires MFA.
- No second domain is required for the admin experience.
- Subscriber registration can never create or promote an administrator.
- Subscribers may select supported notification channels such as SMS, Discord, and email, with per-channel and per-category preferences.
- The official first-party Discord bot/application will be owned through the operator's Discord Developer account. It is distinct from the private internal/admin Discord server.
- Free/Basic/Premium-style plans are candidates; pricing, entitlements, and limits remain open decisions.
- A protected Environment Configuration manager is a roadmap requirement. It must validate the complete proposed configuration before saving, preserve case-sensitive secrets, mask credentials, retain backups/audit history without secret values, and separate saving from applying/restarting. Do not implement until its design is approved.
- Stripe and Twilio are candidates, not selected providers. Email provider is also undecided.
- Existing odds history remains isolated from identity/account records.
- Existing Basic Auth stays in place until the identity cutover is fully implemented, tested, and explicitly approved.

## Implementation phases

1. **Architecture and safeguards** — document current application behavior, product boundaries, migration strategy, and non-negotiable deployment protections.
2. **Identity foundation** — separate subscriber/admin identities, password hashing, sessions, administrator MFA, verification/recovery primitives, and security audit records.
3. **HTTP authorization boundary** — explicit route policy, account-class-specific session dependencies, CSRF, rate limits, trusted proxy review, and Basic Auth compatibility.
4. **Authentication experience** — private admin bootstrap/login/MFA and subscriber registration/login/verification/recovery, after email delivery is selected and integrated.
5. **Subscriber experience** — dashboard/account surfaces, preferences, and entitlement enforcement.
6. **Notification engine** — normalized alert events, preference/entitlement checks, idempotent queueing, retries, delivery tracking, and Discord/SMS/email adapters.
7. **Admin console** — private administrative interface for users, subscriptions, notifications, platform, system configuration, communications, and security.
8. **Billing and plans** — provider-backed checkout/webhooks, subscription lifecycle, and server-side entitlement updates.
9. **Launch readiness** — security, privacy, reliability, backup/restore, operational monitoring, and explicit launch approval.

## Non-negotiable development and deployment safeguards

- All work stays on `feature/admin-panel-and-subscriptions`.
- Do not commit to `main` or `development` for this initiative.
- Do not merge/promote without explicit user approval.
- Do not change production deployment or restart services without explicit approval.
- No local Docker image builds.
- Preserve the existing odds dashboard, provider behavior, and alert delivery unless a separately reviewed change requires otherwise.
- Never delete, replace, or reset persistent data volumes or the odds-history SQLite database.
- Never use destructive Compose commands such as `docker compose down -v`.
- Use additive, reversible database migrations with backups and tests.
- Keep secrets out of Git, logs, browser responses, screenshots, and audit records.
- Preserve case-sensitive API key values exactly.
- Use GitHub Actions for CI and image publishing; verify both before asking the operator to pull a new image.
- Make small, reviewable changes and test each milestone.
- Pause for explicit discussion when a choice could materially affect security, data, deployment, or existing functionality.

## Open decisions

- Final plan names, pricing, entitlements, and trial policy.
- Email and SMS providers, consent/compliance requirements, and delivery limits.
- Discord OAuth scopes, bot permissions, and subscriber server/channel UX.
- Queue technology and expected scale.
- Safe configuration editor allowlist and privileged apply mechanism.
- Billing provider and lifecycle rules.
- Data retention, privacy, account closure, and audit retention.

## Current implementation status

### Architecture and identity foundation

- [x] Existing FastAPI, frontend, authentication, SQLite, Compose, and CI architecture reviewed.
- [x] Identity/access architecture documented; account database isolated from odds history.
- [x] Versioned identity schema, Argon2 password hashing, opaque hashed sessions, revocation, expiry, verification/reset challenges, administrator MFA, and security audit primitives implemented.
- [x] Private first-administrator bootstrap command implemented; no public administrator registration.
- [x] Separate subscriber/admin cookies, CSRF/origin helpers, and lazy session dependencies implemented and tested, but not wired to existing routes.
- [x] Explicit access policy for every current HTTP route; CI detects unclassified and stale entries.
- [x] Persistent atomic rate-limit primitives with HMAC-hashed identifiers and fail-closed behavior implemented, but not wired to production routes.
- [x] Legacy Basic Auth behavior protected by HTTP regression tests and left unchanged.
- [x] Trusted proxy chain resolution hardened; no proxy CIDRs configured pending deployment topology verification.
- [x] Health endpoint exposure reviewed. A design-only proposal documents a minimal public liveness response and future administrator-only operational diagnostics while preserving current health-probe compatibility. See `docs/health-endpoint-security-design.md`.

**Latest verified implementation commit:** `9ea77e3da16dc52a1472c8f5d8ccf3bb71b01e23` (CI run `45498641805` passed).

### Next work

Continue HTTP-level authorization and compatibility coverage, inspect the actual container health-check contract, and select endpoint-specific rate-limit policies. Verify the Caddy-to-app peer topology before configuring trusted proxies. Keep Basic Auth and the existing health response unchanged until the full identity-mode migration boundary and rollback path are proven. Do not implement subscriber/admin login routes until email delivery and deployment security requirements are settled.

The identity foundation is not yet wired into the application and is not a production-ready subscriber authentication system.
