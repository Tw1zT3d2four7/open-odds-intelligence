# Admin Console, Subscribers & Commercial Platform Roadmap

**Status:** Foundation implementation in progress  
**Working branch:** `feature/admin-panel-and-subscriptions`  
**Base branch:** `development`  
**Production branch:** `main`  
**Document purpose:** Preserve the agreed product direction, requirements, safeguards, and implementation sequence before feature development begins.

> This roadmap is a planning document, not authorization to implement every item immediately. Work proceeds milestone by milestone, with design review, tests, and explicit approval before risky changes or promotion.

## 1. Project goals

Evolve Open Odds Intelligence from a single-owner, self-hosted odds-monitoring application into a polished, secure, subscription-based sports odds intelligence platform while preserving existing odds-monitoring functionality and historical data.

The product must feel like a mature commercial SaaS application. Avoid rushed, improvised, or visually inconsistent UI. Agree on the experience and architecture before implementation.

## 2. Confirmed product and architecture decisions

### 2.1 One domain, separate experiences

Keep the existing application domain; do not require a second domain or subdomain. Provide distinct routes and interfaces:

- `/login`, `/register`, `/account` — subscriber experience.
- `/admin/login`, `/admin` — separate private administrator experience.

The Admin Console must have its own layout, navigation, login flow, and server-side authorization. It is not merely a hidden dashboard tab. Knowing or directly visiting an admin URL must never grant access.

### 2.2 Administrator security

- Administrator accounts are provisioned through a trusted private process; public registration must never create or promote an administrator.
- Enforce roles and permissions on the server/API, not just by hiding UI controls.
- Administrator authentication requires MFA.
- Reject unauthorized access to every administrative page and endpoint.
- Record important administrative actions in an audit log.
- Protect secrets and sensitive configuration from subscribers and ordinary application endpoints.

### 2.3 Subscriber accounts and subscriptions

Subscribers must be able to register, verify email, sign in, recover access, manage account/notification preferences, view plan status, and access only features permitted by their plan. Free/Basic/Premium are candidate tiers; pricing, entitlements, trials, and limits remain open. Enforce entitlements server-side.

### 2.4 Subscriber-selected notification channels

Initial channels: Discord, SMS, and email. Potential later channels: browser notifications and Telegram. Subscribers should independently select channels, destinations, alert categories, sports/leagues, thresholds, quiet hours, and limits where supported by plan. Platform qualification rules and subscriber preferences must work together.

### 2.5 Official Open Odds Intelligence Discord bot

Create a first-party Discord application/bot under the operator's Discord Developer account. Subscribers connect via OAuth2, invite the bot to servers they control/administer, select channels, and configure preferences. This remains separate from the operator's private internal/admin Discord server. Never expose bot tokens or private webhook URLs.

### 2.6 SMS and email delivery

SMS requires verified numbers, explicit consent, STOP/HELP handling, consent records, delivery status, and cost monitoring. Email requires verified addresses, delivery/bounce handling, and unsubscribe controls. Provider choices and compliance requirements remain open.

### 2.7 Centralized Notification Engine

Use a centralized pipeline: Odds Intelligence → Qualified Alert → Notification Engine → Subscriber Preferences → Subscription Check → Delivery Queue → Discord/SMS/Email → Delivery Tracking.

It must enforce entitlements, idempotency, duplicate prevention, retries, provider rate limits, delivery history, and failure isolation while preserving the current odds pipeline.

### 2.8 Environment Configuration manager

The Admin Console must eventually provide a protected manager for supported environment variables/API keys. Requirements include masked secrets, controlled reveal, case-preserving API keys, explicit allowlist/schema, full dotenv and semantic validation, provider tests where supported, temporary-file validation, recoverable backup, atomic replacement, restrictive permissions, audit history without secret values, rollback, and a separate save/apply/restart workflow with post-restart health checks.

Do not expose unrestricted host filesystem or Docker socket access to the application. Current Compose reads host `.env` at container creation; a file change alone does not update the running process environment. The safe privileged apply mechanism still needs design. This manager is a roadmap requirement and must not be implemented until its design is approved.

### 2.9 Admin Console information architecture

Proposed sections: Overview, Users, Subscriptions, Billing, Notifications, Platform, System Configuration, Communications, and Security. The interface should be a separate, professional administrative workspace consistent with the product's visual identity.

### 2.10 Billing

Stripe is a candidate, not selected. Use hosted checkout, verified signed webhooks, idempotent event handling, and server-side entitlement updates. Define renewals, cancellation, failures, refunds, grace periods, and plan changes before launch.

## 3. Current architecture findings

- FastAPI serves a static web dashboard.
- Existing authentication is optional HTTP Basic Auth configured by `AUTH_USERNAME` and `AUTH_PASSWORD`; it is not a subscriber identity system.
- Persistent odds/application data lives in SQLite under `/app/data`.
- Docker Compose reads host `.env` when creating the container; it is not mounted into the application container.
- Existing SQLite stores odds history, snapshots, quotes, alerts, CLV, and simulation settlements. Commercial account/subscription records are not yet integrated.
- The current deployment is single-owner/self-hosted and is not yet certified as a multi-tenant subscription service.

These are discovery findings, not reasons for rushed changes.

## 4. Phased implementation roadmap

### Phase 0 — Architecture discovery and product design

Inventory routes, middleware, configuration, SQLite schema, Compose, volumes, background tasks, alert pipeline, and providers. Define threat model, account/session model, migration strategy, UI wireframes, initial entitlements, notification preferences, configuration apply design, and backup/rollback strategy.

**Exit criteria:** Architecture, security boundaries, data model, initial UI design, and migration plan reviewed before feature implementation.

### Phase 1 — Authentication and authorization foundation

Implemented on the feature branch so far:

- [x] Separate SQLite identity database; odds-history database remains separate. Identity DB files are created/tightened to mode 0600 before SQLite opens them, with symlink paths rejected and WAL/SHM sidecars restricted.
- [x] Versioned identity schema with subscriber/admin account classes.
- [x] Argon2 password hashing and account-type-scoped authentication.
- [x] Opaque server-side sessions with hashed tokens, bounded absolute lifetime, idle expiry, and revocation.
- [x] One-time email-verification and password-reset challenges, hashed at rest, expiring, and replay-protected.
- [x] Password reset revokes all sessions; security audit events exclude challenge/password values.
- [x] Private interactive one-time administrator bootstrap; no public administrator registration.
- [x] Administrator TOTP enrollment, encrypted seed storage, fresh-step replay prevention, and ten single-use hashed recovery codes.
- [x] Separate subscriber/admin Secure host-only cookies, CSRF/origin helpers, and lazy FastAPI session dependencies.
- [x] Persistent atomic rate-limit primitives using HMAC-hashed bucket identifiers and fail-closed behavior; not yet attached to production routes.
- [x] Explicit access policy for all 25 registered HTTP routes (21 application endpoints plus four framework-generated schema/documentation routes); CI detects unclassified or stale entries. Manual refresh and framework documentation routes are administrator-only in the target policy.
- [x] Fail-closed route dependency assembly: subscriber routes require a supplied server-side entitlement dependency; authenticated mutations require CSRF; public mutation and unclassified routes are rejected. The helper remains isolated and is not wired to live routes.
- [x] Legacy Basic Auth behavior covered by HTTP regression tests and left unchanged.
- [x] Proxy-chain parsing ignores forwarded headers unless explicitly trusted CIDRs are configured; deployment-specific trust remains unconfigured pending topology review.
- [x] Health endpoint exposure and exact Docker health-probe compatibility documented in `docs/health-endpoint-security-design.md`; no live health behavior changed.
- [x] Proposed endpoint-specific authentication rate-limit policy documented in `docs/auth-rate-limit-policy.md`; thresholds remain inactive pending route integration and deployment review.
- [x] HTTP dependency-boundary tests cover subscriber/admin cookie isolation, missing/expired/revoked sessions, inactive identities, storage failure, CSRF on subscriber/admin mutations, and administrator session creation only after MFA proof.
- [x] CI run `45504487596` passed **673 tests** on commit `02cc3f9e19dc511319e4b215a3a5afe86e7127e3`; follow-up HTTP absolute-expiry coverage passed in run `45504775274` with 674 tests on commit `bc4f0568dbc414d664639822b2689b3e900954e5`; pre-open identity database permission hardening and its tests passed in run `45505030744` with **676 tests** on commit `19f023b4915dce7a26e30dbd8999e131a3201508`.

Still required before Phase 1 is complete:

- [ ] Select and integrate an email delivery provider for verification and recovery.
- [ ] Implement authentication-mode configuration and explicit `legacy_basic` / `identity` migration boundary only after compatibility and rollback tests are complete.
- [ ] Wire subscriber/admin login, logout, registration, verification, and recovery routes with route-specific rate limits.
- [ ] Enforce the route access policy on every existing API route; currently it is a tested inventory, not active middleware.
- [ ] Add server-side subscription entitlement checks and tests before subscriber commercial access is enabled.
- [ ] Verify actual Caddy-to-app peer topology before configuring trusted proxy CIDRs.
- [ ] Preserve current detailed `/api/health` while legacy mode is active; at identity cutover, provide minimal public liveness and protected operational diagnostics without breaking the Compose probe.
- [ ] Test the complete identity flow in a non-production deployment matching the real TLS/reverse-proxy shape.

**Exit criteria:** Subscribers cannot access admin routes or APIs; public registration cannot create admins; admin sessions require MFA; all route authorization and entitlement checks pass; existing dashboard, odds behavior, alerts, and history remain intact.

### Phase 2 — Admin Console foundation

Build the separate Admin Console shell, navigation, responsive layout, protected admin APIs, administrator security controls, and privileged-action audit views.

### Phase 3 — Subscriber account foundation

Implement registration, email verification, login, password recovery, account lifecycle, user management, and retention rules with migration/backup tests.

### Phase 4 — Environment Configuration manager

Define the editable-setting allowlist/schema; implement masked secrets, strict access, dotenv and semantic validation, provider tests, safe atomic apply, backup, audit history, rollback, controlled restart, and post-restart health checks.

### Phase 5 — Subscription plans and entitlements

Define plans, entitlements, lifecycle, and limits; enforce access server-side in APIs and notification dispatch. Final names/prices/limits remain subject to approval.

### Phase 6 — Centralized Notification Engine

Define alert events and subscriber preferences; add durable queueing, idempotency, retries, delivery history, entitlement checks, test mode, and global/channel pause. Preserve existing alert lifecycle behavior.

### Phase 7 — Discord, SMS, and email integrations

Implement official Discord OAuth/bot connection for subscriber-owned servers, selected SMS provider with verification/consent/compliance, and verified email delivery. Add end-to-end delivery and failure tests.

### Phase 8 — Subscriber preferences

Build subscriber settings for channels, categories, sports/leagues, thresholds, quiet hours, limits, connect/disconnect, and plan eligibility.

### Phase 9 — Billing

Select payment provider; implement secure checkout, signed/idempotent webhooks, renewals, cancellation, failed payments, refunds, plan changes, and grace periods.

### Phase 10 — Security, reliability, and launch readiness

Complete tenant isolation, secret/configuration security review, notification abuse controls, queue recovery, backup/restore, concurrent-user testing, privacy/compliance review, monitoring, and operational runbooks. No public subscriber launch without explicit approval.

## 5. Non-negotiable safeguards

- All work stays on `feature/admin-panel-and-subscriptions`, based on `development`.
- Do not commit directly to `main` or `development`.
- Do not merge/promote, deploy, or restart production without explicit approval.
- No local Docker image builds.
- Preserve existing dashboard/provider/alert behavior unless separately reviewed.
- Never delete, replace, or reset persistent volumes or the odds-history database.
- Never use destructive Compose commands such as `docker compose down -v`.
- Use additive, reversible database migrations and backups.
- Keep secrets out of Git, logs, browser responses, screenshots, and audit records.
- Preserve case-sensitive API key values exactly.
- Verify CI and image publication before asking the operator to pull an image.
- Keep `:dev` associated with development and `:latest` with main/production.
- Make small, reviewable changes and test each milestone.
- Pause for explicit discussion where a decision affects security, data, deployment, or existing functionality.

## 6. Open decisions

- Final plan names, prices, entitlements, and trials.
- Email/SMS providers and compliance requirements.
- Discord OAuth scopes, bot permissions, and server/channel UX.
- Queue technology and expected scale.
- Safe configuration allowlist and privileged apply mechanism.
- Billing provider and lifecycle rules.
- Data retention, privacy, account closure, and audit retention.

## 7. Current verified milestone

**Latest verified implementation commit:** `151e56a0277232e64f567b345c286bc93990c32f`  
**CI run:** `45508158978` — **704 passed**.

The authentication foundation remains isolated and is not wired into existing production routes. Existing Basic Auth and the current health response remain unchanged. No production deployment, restart, merge, or branch promotion has occurred.

### Immediate next work

Continue designing the route-protection integration and explicit identity-mode middleware behind the documented compatibility boundary. The current test suite now covers the legacy Basic Auth contract (including partial-configuration fail-closed behavior and framework documentation routes), server-enforced subscriber email verification, atomic session resolution/revocation, all 25 registered route policies, fail-closed dependency assembly across the complete route manifest, CSRF enforcement for every non-safe HTTP method, and legacy Basic Auth protection of both dashboard and product API routes. Do not wire identity mode into the existing application until the email-delivery choice, trusted-proxy topology, entitlement model, complete route coverage, and rollback path are resolved.
