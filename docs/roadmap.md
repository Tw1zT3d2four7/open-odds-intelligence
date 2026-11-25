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

### 2.10 Billing — Stripe selected

Use **Stripe** as the payment processor. Keep payment handling hosted by Stripe rather than storing or processing card data in Open Odds Intelligence.

- Use Stripe Checkout for initial subscription purchases and plan changes where appropriate.
- Use the Stripe Customer Portal for subscriber payment-method updates, invoices/receipts, and self-service cancellation.
- Verify webhook signatures; process events idempotently and tolerate out-of-order/replayed events.
- Stripe is the payment source of truth; Open Odds Intelligence remains the authority for application access and server-side entitlements. Never activate paid access from a browser redirect alone.
- Model subscription lifecycle states explicitly, including trial (only if later approved), active, past-due, grace-period, canceled, unpaid, and refunded/adjusted states as applicable.
- Notify subscribers of failed payments, retry according to Stripe configuration, and use a documented, configurable grace period before paid entitlements are suspended. Preserve accounts and historical data when access is suspended.
- Support monthly and annual automatic recurring billing. Approved customer-facing prices are $10.68/month and $104.05/year, calculated to target $10.00/$100.00 net after standard US domestic card (2.9% + $0.30) and Stripe Billing pay-as-you-go (0.7%) fees. Annual subscribers save $24.11 versus twelve monthly payments. These estimates exclude international-card, currency-conversion, tax, refund, dispute, and custom-pricing fees. Plan names/entitlements, trials, refund rules, grace-period length, and test-mode acceptance remain launch gates.
- Do not store card numbers, CVCs, or other raw payment credentials in our database, logs, audit records, or backups.

### 2.11 Subscriber account experience

Use one subscriber-facing account area on the existing domain, with clear sections for Overview, Subscription, Billing, Security, Notifications, Connected Services, and Alert History. Keep billing self-service simple through Stripe's hosted portal. Keep account identity, password/session controls, notification preferences, and connected-service authorization inside Open Odds Intelligence. Subscriber account routes must remain isolated from the private Admin Console and enforce self-only server-side authorization.

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
- [x] Explicit access policy for all 57 registered HTTP routes (25 product/application and framework-generated routes plus 32 identity/account/admin routes); CI detects unclassified or stale entries. Manual refresh and framework documentation routes are administrator-only in the target policy.
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
- [x] Latest full CI verification for the subscriber session-management milestone: run `45687951512`, commit `f00aa0b` — **870 passed, 1 warning**. The subsequent route-policy assertion is also in a separate CI run.

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
- [x] Draft a non-production identity acceptance checklist covering isolated Compose/volumes, TLS and proxy topology, subscriber/admin flows, route policy, email queue, rate limits, and backup/restore.
- [ ] Execute the checklist in a non-production deployment matching the real TLS/reverse-proxy shape; record actual Caddy-to-app peer topology before configuring trusted proxy CIDRs.

**Exit criteria:** Subscribers cannot access admin routes or APIs; public registration cannot create admins; admin sessions require MFA; all route authorization and entitlement checks pass; existing dashboard, odds behavior, alerts, and history remain intact.

### Phase 2 — Admin Console foundation

- [x] Add a distinct responsive `/admin` console shell with separate navigation and no-store/security headers.
- [x] Protect the shell with an active administrator session; subscriber sessions and unauthenticated requests are rejected.
- [x] Build a protected, no-store Admin Overview API with a strict allowlist of live odds, freshness, bookmaker, and alert metrics; omit provider keys and raw errors.
- [x] Add Users, manual feature access (Subscriptions), and Security views backed by reviewed, MFA-admin-only APIs.
- [x] Add the protected subscriber account overview at /account with a self-only account summary, active feature access, nonce-protected scripts, no-store responses, and secure subscriber sign-out.
- [x] Draft the provider-neutral notification architecture for verified email, an Open Odds-owned Discord bot, and consent-based SMS, including subscriber preferences, delivery outbox/retries, opt-out controls, and admin operations; no provider integration is authorized by the draft.
- [ ] Complete detailed design approval for notification providers (including the SMS provider), Discord OAuth scopes/bot permissions and destination model, preference defaults, consent/opt-out wording, and rollout gates before provider integrations or subscriber-facing delivery are implemented. The high-level centralized-engine architecture and channel direction are approved.
- [ ] Add Notifications, Platform, and Communications views backed by reviewed admin APIs.
- [x] Draft the protected Environment Configuration manager design, including a separate host-side trust boundary, strict variable allowlist, staged validation, atomic publication, backups, rollback, audit, and explicit restart gates.
- [ ] Review and approve the System Configuration manager design and initial variable allowlist before implementation; protected `.env` editing remains disabled until the host-side apply and restart architecture is approved.
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
- [x] Add subscriber self-service active-session management with bounded pagination, current-device identification, self-only revocation, CSRF protection, current-session cookie clearing, and actor-attributed audit events; the account page supports loading additional sessions without exposing session tokens.
- [x] Add subscriber sign-out-everywhere with CSRF protection, atomic revocation of all active sessions, cookie clearing, and self-attributed audit history.
- [x] Draft the subscriber email-change security and delivery design, including verified-new-address-first semantics, old-address security notices, atomic outbox requirements, and migration/recovery tests; implementation remains gated on approval of notification timing, expiry, cancellation, and recovery behavior.
- [ ] Complete the detailed email-change workflow approval (verification expiry, old-address notice timing, cancellation, recovery, and failure handling) before implementing the new schema, routes, queue purposes, or account UI.
- [ ] Design account closure after account-data retention rules are approved; do not implement deletion before that policy is settled.
- [ ] Define account-data and account-closure retention rules before implementing deletion or automated retention.

Notification preferences remain in Phase 8; commercial plan and billing state remain in Phases 5 and 9. The account overview deliberately does not label manual feature grants as a paid subscription.

### Phase 4 — Environment Configuration manager

Define the editable-setting allowlist/schema; implement masked secrets, strict access, dotenv and semantic validation, provider tests, safe atomic apply, backup, audit history, rollback, controlled restart, and post-restart health checks.

### Phase 5 — Subscription plans and entitlements

Define Free, Basic, and Premium as the initial proposed tier structure; final names, prices, entitlement bundles, limits, trials, and monthly/annual discount remain subject to explicit approval. Keep entitlements server-side and default-deny. Stripe is the selected payment processor, but plan lifecycle behavior and launch configuration must be tested in Stripe test mode and isolated staging before production use.

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

- [x] Add the additive v10 billing persistence foundation: Stripe customer mapping, subscription lifecycle snapshots, a unique webhook-event ledger, payload fingerprints, bounded attempt counters, hashed processing leases, lease-expiry recovery, and stale-worker rejection. Encrypted backup/restore schema validation includes all billing tables.
- [x] Implement same-domain Stripe webhook intake with exact-raw-body signature verification, bounded request handling, test/live-mode separation, durable event fingerprints, Fernet-encrypted event bodies, duplicate-safe acknowledgements, and lease-authorized payload retrieval.
- [x] Implement atomic, lease-authorized subscription lifecycle reconciliation for create/update/delete events, including local customer mapping, validated status/period/interval fields, and per-subscription event ordering.
- [x] Implement atomic invoice reconciliation for finalized/paid/payment-failed/voided/uncollectible events, per-invoice ordering, customer/subscription mapping validation, and safe invoice snapshot persistence. Invoice payment state remains separate from application entitlements.
- [x] Add an opt-in bounded billing worker for subscription and invoice inbox processing; it is not automatically started by the web application.
- [x] Add a private subscriber billing summary API and account-page section for local subscription status and invoice history. The endpoint is subscriber-only, self-scoped, bounded, and no-store; Checkout and Customer Portal remain unavailable.
- [ ] Complete Stripe hosted Checkout and Customer Portal after final plan/price/entitlement decisions; retain the existing signed webhook and reconciliation foundation, then add payment-failure notification, retry/grace-period policy, refund/adjustment handling, plan changes, and complete test-mode/staging acceptance. No live charges or production activation without explicit approval.
- [x] Add case-preserving Stripe secret-key and monthly/yearly recurring Price ID configuration, with independent Checkout and Customer Portal switches defaulting off. No credentials or actual Price IDs are committed; configuration alone cannot enable a payment flow.

### Phase 10 — Security, reliability, and launch readiness

Complete tenant isolation, secret/configuration security review, notification abuse controls, queue recovery, backup/restore, concurrent-user testing, privacy/compliance review, monitoring, and operational runbooks. No public subscriber launch without explicit approval.

## 4A. Approved subscriber-commerce and notification delivery sequence

The high-level architecture and provider direction below are approved. Detailed provider settings, legal/compliance wording, production credentials, pricing, and launch activation remain separate gates.

1. **Account security and subscriber self-service:** finish the account foundation, including password change/recovery, session visibility/revocation, security notices, and the separately gated email-change workflow. Add the account navigation structure without exposing unfinished controls.
2. **Plans and entitlement model:** define Free/Basic/Premium candidate bundles, limits, lifecycle states, and server-side access checks. Manual administrator grants remain distinct from paid subscriptions.
3. **Stripe billing:** integrate Checkout, Customer Portal, signed/idempotent webhooks, subscription reconciliation, failed-payment handling, and a configurable grace period. Use Stripe test mode first; never store card data.
4. **Central notification engine:** implement one durable event/outbox/dispatch pipeline with entitlement checks, subscriber preferences, idempotency, bounded retries, provider rate limits, delivery history, test mode, and global/channel pause controls. Preserve the existing odds-alert lifecycle and isolate delivery failures from odds processing.
5. **Email delivery:** use the already-selected Resend transactional email provider for account/security and opted-in alert delivery, with verified sender configuration, bounce/suppression handling, and unsubscribe controls where applicable.
6. **Official Discord integration:** use the first-party Open Odds Intelligence Discord application/bot and OAuth-based account linking. Subscribers authorize their own server/channel destinations; never reuse the operator's private admin Discord server or expose bot tokens/webhook secrets.
7. **Optional SMS integration:** select and evaluate a provider before implementation. Require verified phone numbers, explicit consent records, STOP/HELP support, opt-out enforcement, delivery/cost monitoring, and appropriate compliance review. SMS remains optional and can be disabled per subscriber.
8. **Subscriber preferences and account UI:** expose channel, category, sports/league, threshold, quiet-hour, and delivery-limit controls according to plan. Provide test delivery and bounded delivery history. Do not show nonfunctional controls before their backing APIs are ready.
9. **Staging acceptance:** exercise the complete subscriber lifecycle, billing events, notification preferences, channel failures/retries, opt-outs, access revocation, backup/restore, and rollback in isolated staging before any production approval.

The Environment Configuration manager remains a separate protected-admin workstream and is not a prerequisite for silently activating any integration. Its existing design-approval and host-side apply/restart gates remain in force. Email-change behavior likewise remains gated on its detailed workflow approval. No production credentials, real charges, public signups, external messages, production restarts, or cutovers are authorized by this architecture approval.

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

- Plan entitlement bundles, final paid-tier names, and trials. Approved subscriber pricing: $10.00 monthly net target and $100.00 annual net target; using Stripe's published US domestic card rate (2.9% + $0.30) plus Stripe Billing pay-as-you-go (0.7%), customer-facing catalog prices are $10.68/month and $104.05/year. Both renew automatically at their selected interval. The annual price saves $24.11 versus twelve monthly payments. These fee gross-ups are estimates for standard domestic card processing and exclude international-card, currency-conversion, tax, refund, dispute, and custom-pricing fees.
- Email/SMS providers and compliance requirements.
- Discord OAuth scopes, bot permissions, and server/channel UX.
- Queue technology and expected scale.
- Safe configuration allowlist and privileged apply mechanism.
- Billing provider and lifecycle rules.
- Data retention, privacy, account closure, and audit retention.

## 7. Current verified milestone

**Latest user-confirmed green implementation commit:** `255b963`  
**CI run:** `45676162939` — **865 passed, 1 warning**.  
**Roadmap architecture update:** `007510b3696c265de506152edcc68801fafbf3a3` (documentation-only).

The identity backup tooling now publishes completed encrypted backups atomically without overwriting existing files. Failure-path tests verify that an interrupted publication cannot leave a partial final backup; restore remains an explicit offline-only operation with an encrypted pre-restore snapshot. This is CI-verified tooling only: no production backup or restore has been run.

Middleware now resolves parameterized URLs to their registered FastAPI route template and HTTP method before applying the identity access policy; regression tests cover subscriber suspension, subscriber-wide session revocation, and individual session revocation in both identity and legacy modes.

The subscriber account foundation now includes a protected read-only `/account` page and `/auth/account` API. Subscriber login redirects to the account overview, while administrator login redirects to the separate Admin Console. Account data is subscriber-only, minimal, non-cacheable on success and authorization failure, and rendered with a fresh CSP script nonce. The account page now also supports subscriber-initiated password change: the current password is verified, the update is atomic, outstanding reset links and all sessions are invalidated, the local cookies are cleared, and a secret-free self-attributed audit event is recorded.

The identity foundation is integrated behind a staged authentication boundary. An explicit validated `AUTH_MODE` setting defaults to `legacy_basic`; the existing Basic Auth behavior remains unchanged in that mode, including avoiding identity-database initialization on product requests. The recognized `identity` mode admits separately policy-classified identity routes and applies explicit session, administrator, CSRF, and entitlement dependencies to every existing product route. Unauthenticated product requests return 401; authenticated subscribers without the matching feature grant receive 403; storage failures fail closed with 503. Unknown mode values fail application configuration loading. The exact safe GET/HEAD health bypass and detailed health response remain unchanged. Resend is now the selected initial transactional-email provider, with a standard-library HTTPS adapter, case-preserving API-key handling, bounded input validation, sanitized provider failures, and provider idempotency-key support. An isolated subscriber/administrator HTTP router now implements CSRF bootstrap, registration, email verification/resend, password recovery, subscriber login/logout, and administrator password-plus-TOTP/recovery-code login. It includes operation-specific IP/account rate limits, resend cooldown/daily caps, generic account-enumeration-resistant responses, separate session cookies, session rotation, and MFA-gated administrator sessions. The identity router is now registered with FastAPI and exposed only in explicitly selected staged `AUTH_MODE=identity`; in default `legacy_basic` mode its paths return 404 and the prior Basic Auth behavior remains unchanged. In staged identity mode, the 32 policy-classified identity routes are reachable. All 25 product application routes have explicit dependencies; feature grants default to deny and can be changed only by an MFA-backed administrator through the protected entitlement API. The public health endpoint becomes minimal in identity mode, while the legacy-mode detailed response and Docker liveness contract remain intact. The identity database is now at schema v9 with secret-free 90-day email delivery history, a persistent Resend circuit breaker, and a durable transactional email queue whose recipient/body/challenge payload is Fernet-encrypted (five consecutive failures within ten minutes opens for ten minutes; one worker may claim a 60-second half-open probe). HTTP tests cover successful/failed registration, verification, recovery, session creation/rotation/logout, CSRF rejection and no-store bootstrap behavior, account-enumeration resistance, resend cooldowns, admin MFA/recovery-code use, subscriber/admin cookie separation, delivery status, circuit opening, and half-open concurrency. The 32 identity/page/admin routes have a separate explicit `IDENTITY_ROUTE_ACCESS_POLICY` merged into the complete 57-route application manifest; the fail-closed dependency assembler supports public-CSRF and subscriber-only classes, and tests verify every router endpoint's actual security dependencies match its policy. Authentication endpoints are excluded from general OpenAPI docs. The queue worker runs only in identity mode, uses atomic leases and stable Resend idempotency keys, retries transient failures with bounded backoff, and never sends a challenge after its one-time token is consumed or expired. The staged identity exposure and route inventory are documented in `docs/identity-http-routes.md`. The current Admin Console branch includes the protected subscriber directory, audited suspension/reactivation, manual feature-access management, and filtered security audit history. The latest full subscriber/Admin Console regression suite passed CI run `45687951512`: 870 passed, 1 existing Starlette/httpx deprecation warning. No production deployment, restart, merge, or branch promotion has occurred.

### Immediate next work

The route-by-route threat review of the enabled Admin Console routes is complete: privileged subscriber status, session, and entitlement mutations require an active MFA administrator, CSRF, controlled reason codes, atomic changes, and actor-attributed audit events. The responsive subscriber sign-in/registration, verification/recovery, and separate administrator sign-in pages are implemented and bootstrap CSRF before mutations. The encrypted durable transactional-email queue and default-deny feature entitlement system are implemented. Encrypted identity backup/restore tooling is implemented and CI-verified, including schema/integrity validation, wrong-key rejection, encrypted pre-restore snapshots, explicit offline restore flags, and atomic no-overwrite backup publication.

Remaining Phase 1 deployment gates are operational and must be completed only against a non-production copy: verify the exact Caddy-to-app peer topology before configuring any trusted proxy CIDRs; perform backup/restore, queue/database recovery, and key-rotation drills; run the full identity flow with a verified sender domain and protected keys; and validate rollback. Do not switch production from `legacy_basic`, enable public subscriber access, or deploy identity mode until every gate passes and the operator explicitly approves.

The current test suite covers the legacy Basic Auth contract (including partial-configuration fail-closed behavior and framework documentation routes), server-enforced subscriber email verification, atomic session resolution/revocation, all 25 product/framework route policies plus the 32 identity routes, fail-closed dependency assembly across the complete route manifest, CSRF enforcement for every non-safe HTTP method, legacy Basic Auth protection of dashboard/product API/framework docs, HTTP-level integration of assembled route policies, secret-free actor-attributed audit events, MFA-admin-only cross-account session revocation, bounded filtered audit queries, and encrypted backup/restore safety.


### Stripe billing foundation — current boundary

The Stripe billing foundation remains persistence-first. Schema v9 introduced the local subscriber-to-Stripe-customer mapping, subscription snapshots, and event ledger. Schema v10 added atomic event claims with hashed leases, expiry recovery, bounded attempts, stale-worker rejection, and sanitized failure codes. Schema v11 adds a Fernet-encrypted event body alongside its SHA-256 fingerprint; raw webhook bodies are never stored in plaintext. Schema v12 adds a per-subscription Stripe event timestamp watermark for out-of-order protection. Schema v13 adds an invoice snapshot ledger with per-invoice event ordering, validated payment amounts/currency, and optional links to known local subscriptions. Schema v14 adds a durable payment-failure timestamp so failed invoices remain distinguishable from ordinary open invoices and successful payment clears the failure marker. All changes are additive and included in encrypted identity backup validation.

The webhook intake verifies the exact raw request body and explicit test/live mode before persisting the encrypted event. Duplicate event IDs with identical content are acknowledged idempotently; mismatched reuse is rejected. The handler fails closed unless both the dedicated signing secret and separate webhook-encryption key are configured. Legacy inbox entries without recoverable bodies are marked for safe redelivery rather than being treated as processable.

A lease-authorized reconciliation primitive handles `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. A parallel invoice processor handles `invoice.finalized`, `invoice.paid`, `invoice.payment_failed`, `invoice.voided`, and `invoice.marked_uncollectible`. Both resolve the subscriber only through the pre-existing local Stripe-customer mapping, validate provider identifiers and state, ignore older snapshots per object, and update the local snapshot plus webhook completion in one SQLite transaction while rechecking lease ownership and expiry. Invoice subscription references must match the existing local subscription/customer mapping. Invoice payment status is recorded independently and never changes subscription entitlements or grants application features. The opt-in worker dispatches invoice events to the invoice processor; it remains disabled unless an operator explicitly runs it. Monthly/yearly intervals remain supported as `month` / `year`; plan codes, prices, and entitlement bundles remain unset pending explicit product decisions. The subscriber `/account` page now includes a private read-only subscription/invoice view backed by `GET /auth/billing`; purchases and portal management explicitly report unavailable until launch configuration and acceptance are complete. Latest full CI verification after invoice reconciliation, subscriber billing summary, schema-v14 payment-failure tracking, and payment-recovery regression coverage: run `45714614405`, commit `efc2631` — **910 passed, 1 warning**. No Checkout/Portal, live payment, or production behavior has been enabled.

No Stripe SDK/API checkout calls, Checkout/Portal routes, production signing secret, price ID, entitlement bundle, or live payment behavior is activated. Application entitlements remain the sole authority for product access; subscription status must never grant features by itself.

The private Admin Console now includes a read-only `GET /admin/billing` operations view. It is protected by the MFA administrator policy and reports aggregate customer, subscription, invoice, payment-failure, and webhook-processing counts only; it does not return subscriber identities, Stripe object IDs, webhook bodies, or lease material. The Billing section renders those aggregates with an explicit disabled-checkout/portal/live-billing boundary. CI run `45715925398`, commit `5c8ef67` — **911 passed, 1 warning**; Python compilation, dashboard JavaScript syntax, Docker Compose validation, and simulator CLI checks also passed. This is operational visibility only, not a payment activation milestone.


### Subscriber pricing disclosure — approved

The customer-facing subscription catalog uses one all-inclusive displayed price per billing interval: **$10.68/month** or **$104.05/year**, each automatically recurring. The displayed amount includes an allowance for standard domestic card-processing and Stripe Billing pay-as-you-go costs; the customer is not charged a separate processing surcharge at checkout. The account/pricing presentation must disclose the total price before purchase, automatic renewal, and cancellation terms before confirmation. The wording must not promise that the estimated fee allowance exactly offsets every possible payment cost: international-card, currency-conversion, tax, refund, dispute, and other exceptional charges may differ from the baseline assumptions. Checkout remains disabled until test-mode acceptance and entitlement/access rules are complete.
