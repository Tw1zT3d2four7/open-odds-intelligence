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
- [x] Make first-admin bootstrap resumable after an interrupted pre-MFA setup only when the existing admin password verifies; already-enrolled MFA cannot be reset through bootstrap, and TOTP codes are entered without terminal echo.
- [x] Separate subscriber/admin Secure host-only cookies, CSRF/origin helpers, and lazy FastAPI session dependencies.
- [x] Persistent atomic rate-limit primitives using HMAC-hashed bucket identifiers and fail-closed behavior; integrated with identity-mode login, registration, verification, and password-reset routes.
- [x] Explicit access policy for all 25 registered HTTP routes (21 application endpoints plus four framework-generated schema/documentation routes); CI detects unclassified or stale entries. Manual refresh and framework documentation routes are administrator-only in the target policy.
- [x] Fail-closed route dependency assembly: subscriber routes require a supplied server-side entitlement dependency; every authenticated non-safe HTTP method requires CSRF; public mutation and unclassified routes are rejected. The helper is wired to all product APIRoutes in `app/main.py`; identity mode enforces server-side session, entitlement, and CSRF dependencies while legacy Basic Auth remains unchanged.
- [x] Legacy Basic Auth behavior covered by HTTP regression tests (including malformed base64, missing separators, invalid UTF-8, mixed-case scheme handling, and product API protection) and left unchanged.
- [x] Subscriber email verification enforced in authentication, session creation, and session resolution; unverified accounts cannot establish usable sessions.
- [x] Session resolution is atomic with revocation/deactivation; session, MFA, and one-time challenge time checks are evaluated after acquiring their database transaction.
- [x] Security audit events cover registration/provisioning, verification, password reset, MFA, and session lifecycle. Actor attribution is validated for cross-account session revocation.
- [x] Bounded, paginated, secret-free audit query supports event-type, actor, and subject filters without returning email addresses.
- [x] Proxy-chain parsing ignores forwarded headers unless explicitly trusted CIDRs are configured; deployment-specific trust remains unconfigured pending topology review.
- [x] Health endpoint exposure and exact Docker health-probe compatibility documented in `docs/health-endpoint-security-design.md`; no live health behavior changed.
- [x] Endpoint-specific authentication rate-limit policy documented in `docs/auth-rate-limit-policy.md` and integrated with identity-mode routes; conservative thresholds remain subject to deployment-traffic and proxy-topology review before public activation.
- [x] HTTP dependency-boundary tests cover subscriber/admin cookie isolation, missing/expired/revoked sessions, inactive identities, storage failure, CSRF on subscriber/admin mutations, and administrator session creation only after MFA proof.
- [x] Latest full CI verification of the identity/Admin Console security baseline: run `45671099424`, commit `81207b4384ac404bca3b540f20efbc269c14fa38` — **858 passed, 1 warning**.

Still required before Phase 1 is complete:

- [x] Select Resend and implement the transactional HTTPS adapter, with case-preserving API-key handling and sanitized provider errors.
- [x] Add explicit `legacy_basic` / `identity` mode configuration and preserve the exact legacy compatibility boundary. Identity mode admits only classified routes and enforces session-based authorization.
- [x] Register subscriber/admin login, logout, registration, verification, and recovery routes behind the identity-mode allowlist, with CSRF and persistent route-specific rate limits.
- [x] Add durable encrypted email queueing with a dedicated Fernet key, atomic worker leases, stable provider idempotency, bounded transient retries, challenge-state/expiry checks, and provider circuit integration.
- [x] Enforce the route access policy on every existing product/API route with explicit mode-aware subscriber/admin dependencies, CSRF checks, and default-deny feature entitlements.
- [x] Add server-side feature entitlement storage, expiry checks, admin-only grant/revoke API, audit attribution, and default-deny enforcement. Commercial plan bundles and billing remain later phases.
- [ ] Verify actual Caddy-to-app peer topology before configuring trusted proxy CIDRs.
- [x] Preserve detailed `/api/health` in legacy mode and provide minimal public liveness in identity mode without breaking the Compose probe.
- [x] Build responsive subscriber sign-in/registration, email verification/recovery, and separate administrator sign-in entry pages with CSRF bootstrap and no-store responses.
- [x] Add encrypted identity-database backup/restore tooling with explicit offline restore confirmation, full version-aware schema/migration and foreign-key validation (including optional rate-limit buckets), bounded encrypted snapshots, encrypted pre-restore snapshots, and odds-database protection; preserve caller-owned directory permissions, reject group/world-writable destinations, and keep backup files mode 0600. CLI tests verify inspection-only default behavior, require both `--apply` and `--confirm-offline` for replacement, and prove a wrong key leaves the restore target untouched.
- [x] Document the non-production backup/restore and key-recovery drill procedure; automated CLI tests cover inspection-only defaults, explicit offline apply, pre-restore snapshots, and wrong-key no-mutation behavior.
- [ ] Execute the backup/restore and key-recovery drill in a non-production deployment; the tooling has not been run against any live volume.
- [ ] Test the complete identity flow in a non-production deployment matching the real TLS/reverse-proxy shape.

**Exit criteria:** Subscribers cannot access admin routes or APIs; public registration cannot create admins; admin sessions require MFA; all route authorization and entitlement checks pass; existing dashboard, odds behavior, alerts, and history remain intact.

### Phase 2 — Admin Console foundation

- [x] Add a distinct responsive `/admin` console shell with separate navigation and no-store/security headers.
- [x] Protect the shell with an active administrator session; subscriber sessions and unauthenticated requests are rejected.
- [x] Build a protected, no-store Admin Overview API with a strict allowlist of live odds, freshness, bookmaker, and alert metrics; omit provider keys and raw errors.
- [x] Add Users, manual feature access (Subscriptions), and Security views backed by reviewed, MFA-admin-only APIs.
- [x] Add the protected subscriber account overview at /account with a self-only account summary, active feature access, nonce-protected scripts, no-store responses, and secure subscriber sign-out.
- [x] Draft the provider-neutral notification architecture for verified email, an Open Odds-owned Discord bot, and consent-based SMS, including subscriber preferences, delivery outbox/retries, opt-out controls, and admin operations; no provider integration is authorized by the draft.
- [ ] Approve notification providers, Discord linking/delivery mode, preference defaults, and rollout gates before implementation.
- [ ] Add Notifications, Platform, and Communications views backed by reviewed admin APIs.
- [ ] Design and approve the System Configuration manager before implementation; protected `.env` editing remains disabled until its allowlist, validation, atomic apply, backup, rollback, and restart design is approved.
- [x] Add a protected, bounded, paginated, no-store administrator audit API and a Security view that renders audit events without account email addresses.
- [x] Expose bounded event-type, actor-identity, and subject-identity filters in the Security view; audit records remain free of account email addresses and credentials.
- [x] Add MFA-admin-only, CSRF-protected subscriber suspension/reactivation with required controlled reason codes, atomic session revocation on suspension, and actor-attributed audit events; expose guarded controls in the Users directory.
- [x] Add a protected, paginated, searchable, read-only subscriber directory and Users view; expose only account identity, email, active/verification state, creation time, and active entitlements, never authentication material.
- [x] Add a protected manual feature-access manager in the Admin Console using the MFA-admin-attributed, CSRF-protected entitlement API; every grant/revoke replacement requires a controlled reason code recorded with the exact grant set and expiry; plan bundles and billing remain separate work.
- [x] Add MFA-admin-only, CSRF-protected individual session revocation and subscriber-wide session revocation; enforce subscriber-only targets, controlled reason codes, atomic revocation, current-admin session protection, and actor-attributed audit events.
- [x] Apply `no-store` to authentication, subscriber-account, administrator, and protected API responses before authorization or identity-store failures; protect inline authentication/Admin Console scripts with a fresh per-response CSP nonce instead of `unsafe-inline` script permission.
- [x] Complete the route-by-route threat review of all enabled Admin Console routes: cross-account suspension/reactivation, session revocation, and feature-access changes require an MFA-admin session, CSRF, controlled reason codes, and atomic actor-attributed audit events; logout remains self-service and audited. No other privileged write routes are exposed.
- [ ] Define and approve an audit-retention policy before implementing retention administration or automated deletion.

The initial shell now includes working Security, Users, and manual Feature Access views backed by bounded, no-store, administrator-only APIs. The subscriber directory is searchable and exposes only the minimum account fields needed for operator support; subscriber suspension/reactivation, individual/subscriber-wide session revocation, and manual feature-access changes are backed by audited, MFA-admin-only mutations with CSRF protection and controlled reason codes; bulk revocation is available directly from the subscriber directory, while the Security view retains individual session controls. Other account lifecycle writes remain unavailable until their safeguards and audit flows are implemented. Other unimplemented sections remain labeled as planned and do not display fabricated metrics or offer nonfunctional write controls. It is exposed only in staged `AUTH_MODE=identity`; production remains on `legacy_basic` until Phase 1 deployment gates and rollback drills pass.

### Phase 3 — Subscriber account foundation

- [x] Implement subscriber registration, email verification, login/logout, and account-enumeration-resistant password recovery with one-time challenges and session revocation.
- [x] Add a protected, read-only `/account` overview and subscriber-only `/auth/account` summary API; show only the signed-in subscriber's verified/active state and currently active feature grants.
- [x] Implement subscriber-initiated password changes with current-password re-authentication, rate limits, CSRF protection, session revocation, invalidation of outstanding reset links, self-attributed audit events, and a confirmed password-change form on the account page.
- [x] Draft the subscriber email-change security and delivery design, including verified-new-address-first semantics, old-address security notices, atomic outbox requirements, and migration/recovery tests; implementation remains gated on approval of notification timing, expiry, cancellation, and recovery behavior.
- [ ] Approve the email-change workflow decisions before implementing the new schema, routes, queue purposes, and account UI.
- [ ] Design account closure after account-data retention rules are approved; do not implement deletion before that policy is settled.
- [ ] Define account-data and account-closure retention rules before implementing deletion or automated retention.

Notification preferences remain in Phase 8; commercial plan and billing state remain in Phases 5 and 9. The account overview deliberately does not label manual feature grants as a paid subscription.

### Phase 4 — Environment Configuration manager

Define the editable-setting allowlist/schema; implement masked secrets, strict access, dotenv and semantic validation, provider tests, safe atomic apply, backup, audit history, rollback, controlled restart, and post-restart health checks.

### Phase 5 — Subscription plans and entitlements

Define plans, entitlements, lifecycle, and limits; enforce access server-side in APIs and notification dispatch. Final names/prices/limits remain subject to approval.

### Phase 6 — Centralized Notification Engine

- [x] Draft the provider-neutral outbox/dispatcher architecture, official Open Odds-owned Discord bot linking flow, SMS consent requirements, and admin operations model.
- [x] Document recommended defaults for Discord DM/server destinations, real-time/digest delivery, quiet hours, alert categories, multi-channel behavior, and explicit fallback consent.
- [ ] Approve the notification data model, channel defaults, provider evaluation, and subscriber preference UX before implementation.
- [ ] Define alert events and subscriber preferences; add durable queueing, idempotency, retries, delivery history, entitlement checks, test mode, and global/channel pause. Preserve existing alert lifecycle behavior.

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

**Latest verified implementation commit:** `db8cc80ca250dbe50c35a2e862e052fb4a6c2e37`  
**CI run:** `45673514308` — **863 passed, 1 warning**.

The identity backup tooling now publishes completed encrypted backups atomically without overwriting existing files. Failure-path tests verify that an interrupted publication cannot leave a partial final backup; restore remains an explicit offline-only operation with an encrypted pre-restore snapshot. This is CI-verified tooling only: no production backup or restore has been run.

Middleware now resolves parameterized URLs to their registered FastAPI route template and HTTP method before applying the identity access policy; regression tests cover subscriber suspension, subscriber-wide session revocation, and individual session revocation in both identity and legacy modes.

The subscriber account foundation now includes a protected read-only `/account` page and `/auth/account` API. Subscriber login redirects to the account overview, while administrator login redirects to the separate Admin Console. Account data is subscriber-only, minimal, non-cacheable on success and authorization failure, and rendered with a fresh CSP script nonce. The account page now also supports subscriber-initiated password change: the current password is verified, the update is atomic, outstanding reset links and all sessions are invalidated, the local cookies are cleared, and a secret-free self-attributed audit event is recorded.

The identity foundation is integrated behind a staged authentication boundary. An explicit validated `AUTH_MODE` setting defaults to `legacy_basic`; the existing Basic Auth behavior remains unchanged in that mode, including avoiding identity-database initialization on product requests. The recognized `identity` mode admits separately policy-classified identity routes and applies explicit session, administrator, CSRF, and entitlement dependencies to every existing product route. Unauthenticated product requests return 401; authenticated subscribers without the matching feature grant receive 403; storage failures fail closed with 503. Unknown mode values fail application configuration loading. The exact safe GET/HEAD health bypass and detailed health response remain unchanged. Resend is now the selected initial transactional-email provider, with a standard-library HTTPS adapter, case-preserving API-key handling, bounded input validation, sanitized provider failures, and provider idempotency-key support. An isolated subscriber/administrator HTTP router now implements CSRF bootstrap, registration, email verification/resend, password recovery, subscriber login/logout, and administrator password-plus-TOTP/recovery-code login. It includes operation-specific IP/account rate limits, resend cooldown/daily caps, generic account-enumeration-resistant responses, separate session cookies, session rotation, and MFA-gated administrator sessions. The identity router is now registered with FastAPI and exposed only in explicitly selected staged `AUTH_MODE=identity`; in default `legacy_basic` mode its paths return 404 and the prior Basic Auth behavior remains unchanged. In staged identity mode, the 29 policy-classified identity routes are reachable. All 25 product application routes have explicit dependencies; feature grants default to deny and can be changed only by an MFA-backed administrator through the protected entitlement API. The public health endpoint becomes minimal in identity mode, while the legacy-mode detailed response and Docker liveness contract remain intact. The identity database is now at schema v8 with secret-free 90-day email delivery history, a persistent Resend circuit breaker, and a durable transactional email queue whose recipient/body/challenge payload is Fernet-encrypted (five consecutive failures within ten minutes opens for ten minutes; one worker may claim a 60-second half-open probe). HTTP tests cover successful/failed registration, verification, recovery, session creation/rotation/logout, CSRF rejection and no-store bootstrap behavior, account-enumeration resistance, resend cooldowns, admin MFA/recovery-code use, subscriber/admin cookie separation, delivery status, circuit opening, and half-open concurrency. The 29 identity/page/admin routes have a separate explicit `IDENTITY_ROUTE_ACCESS_POLICY` merged into the complete 54-route application manifest; the fail-closed dependency assembler supports public-CSRF and subscriber-only classes, and tests verify every router endpoint's actual security dependencies match its policy. Authentication endpoints are excluded from general OpenAPI docs. The queue worker runs only in identity mode, uses atomic leases and stable Resend idempotency keys, retries transient failures with bounded backoff, and never sends a challenge after its one-time token is consumed or expired. The staged identity exposure and route inventory are documented in `docs/identity-http-routes.md`. The current Admin Console branch includes the protected subscriber directory, audited suspension/reactivation, manual feature-access management, and filtered security audit history. The latest fully tested Admin Console implementation, including the protected live Overview diagnostics, passed CI run `45621975183`: 838 passed, 1 existing Starlette/httpx deprecation warning. No production deployment, restart, merge, or branch promotion has occurred.

### Immediate next work

The route-by-route threat review of the enabled Admin Console routes is complete: privileged subscriber status, session, and entitlement mutations require an active MFA administrator, CSRF, controlled reason codes, atomic changes, and actor-attributed audit events. The responsive subscriber sign-in/registration, verification/recovery, and separate administrator sign-in pages are implemented and bootstrap CSRF before mutations. The encrypted durable transactional-email queue and default-deny feature entitlement system are implemented. Encrypted identity backup/restore tooling is implemented and CI-verified, including schema/integrity validation, wrong-key rejection, encrypted pre-restore snapshots, explicit offline restore flags, and atomic no-overwrite backup publication.

Remaining Phase 1 deployment gates are operational and must be completed only against a non-production copy: verify the exact Caddy-to-app peer topology before configuring any trusted proxy CIDRs; perform backup/restore, queue/database recovery, and key-rotation drills; run the full identity flow with a verified sender domain and protected keys; and validate rollback. Do not switch production from `legacy_basic`, enable public subscriber access, or deploy identity mode until every gate passes and the operator explicitly approves.

The current test suite covers the legacy Basic Auth contract (including partial-configuration fail-closed behavior and framework documentation routes), server-enforced subscriber email verification, atomic session resolution/revocation, all 25 product/framework route policies plus the 29 identity routes, fail-closed dependency assembly across the complete route manifest, CSRF enforcement for every non-safe HTTP method, legacy Basic Auth protection of dashboard/product API/framework docs, HTTP-level integration of assembled route policies, secret-free actor-attributed audit events, MFA-admin-only cross-account session revocation, bounded filtered audit queries, and encrypted backup/restore safety.
