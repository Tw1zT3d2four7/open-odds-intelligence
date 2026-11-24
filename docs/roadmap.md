# Admin Console, Subscribers & Commercial Platform Roadmap

**Status:** Foundation implementation in progress  
**Working branch:** `feature/admin-panel-and-subscriptions`  
**Base branch:** `development`  
**Production branch:** `main`  
**Document purpose:** Preserve the agreed product direction, requirements, safeguards, and implementation sequence before feature development begins.

> This roadmap is a planning document, not authorization to implement every item immediately. Work proceeds milestone by milestone, with design review, tests, and explicit approval before risky changes or promotion.

## 1. Project goals

Evolve Open Odds Intelligence from a single-owner, self-hosted odds-monitoring application into a polished, secure, subscription-based sports odds intelligence platform while preserving the existing odds-monitoring functionality and historical data.

The product must feel like a mature commercial SaaS application. Avoid rushed, improvised, or visually inconsistent UI. Agree on the experience and architecture before implementation.

## 2. Confirmed product and architecture decisions

### 2.1 One domain, separate experiences

Do **not** require a second domain or subdomain. Keep the existing application domain and provide distinct routes and interfaces:

- `/login` — subscriber login.
- `/register` — subscriber registration.
- `/account` — subscriber account and preferences.
- `/admin/login` — private administrator login.
- `/admin` — separate Admin Console.
  
The Admin Console must be a genuinely separate administrative experience with its own layout, navigation, login flow, and server-side authorization. It is not merely a hidden dashboard tab. Knowing or directly visiting an admin URL must never grant access.

### 2.2 Administrator security

- Administrator accounts are provisioned through a trusted, private process; public registration must never create an administrator.
- Subscribers cannot promote themselves or other users to administrator.
- Enforce roles and permissions on the server/API, not just by hiding UI controls.
- Use secure authentication and session handling, password recovery, and multi-factor authentication (MFA) for administrators.
- Reject unauthorized access to every administrative page and endpoint.
- Record important administrative actions in an audit log.
- Protect secrets and sensitive configuration from subscribers and ordinary application endpoints.
- Consider separate administrator identity/credential policy even if authentication components are shared internally with subscriber accounts.

### 2.3 Subscriber accounts and subscriptions

Provide individual subscriber identities and account management. Subscribers should be able to:

- Register, verify their email, sign in, and recover access.
- View account and subscription status.
- Manage their profile and notification preferences.
- See available subscription plans and billing status.
- Access only the features and alert categories permitted by their plan.

Proposed initial plan tiers (names and entitlements are not final):

- **Free** — limited access to selected features.
- **Basic** — standard odds intelligence features.
- **Premium** — full platform access and advanced features.

Pricing, exact entitlements, trial policy, and plan limits remain to be designed. Enforce subscription entitlements server-side.

### 2.4 Subscriber-selected notification channels

Notification preferences are a core product capability. Initial delivery channels:

1. **Discord**
2. **SMS text messages**
3. **Email**

Potential later channels:

- Browser notifications.
- Telegram.

Subscribers should independently choose enabled channels and eligible alert categories. They should be able to manage destinations, verify ownership, and change preferences from their account area.

Potential alert categories include:

- Arbitrage opportunities.
- Expected-value opportunities.
- Significant line movement.
- Game-start lifecycle updates.
- Verified final-result lifecycle updates.
- Platform announcements.

Potential subscriber controls include minimum EV, minimum arbitrage, sports/leagues, quiet hours, daily notification limits, and whether to receive every qualifying opportunity or only selected/highest-ranked opportunities. Exact controls and defaults require product design.

Platform-wide qualification thresholds and subscriber preferences must work together. A subscriber must never receive an alert for a feature or category excluded by their subscription.

### 2.5 Official Open Odds Intelligence Discord bot

Create and own an official Open Odds Intelligence Discord application and bot under the operator's Discord Developer account. This is a new first-party bot, not a third-party bot.

Intended subscriber flow:

1. Subscriber chooses Connect Discord in account settings.
2. Subscriber authorizes the official application using Discord OAuth2.
3. Subscriber invites the bot to a server they control or administer.
4. Subscriber selects an available server/channel where the bot has permission to post.
5. Subscriber saves alert preferences.
6. The platform delivers eligible alerts to that configured destination.

Requirements:

- Subscribers connect their own Discord servers; they are not required to use the operator's private Discord server.
- The operator's private Discord server remains separate for internal administration/testing.
- Never expose the bot token, private webhook URLs, or other platform credentials to subscribers.
- Validate server/channel access and bot permissions.
- Handle bot removal, revoked authorization, missing permissions, delivery errors, and reconnection.
- Store destination identifiers and authorization metadata securely.
- Follow Discord OAuth2, bot, and platform requirements.

### 2.6 SMS and email delivery

**SMS:** Evaluate a provider such as Twilio or another suitable provider. Require number verification and explicit consent before sending. Support opt-out/STOP and HELP handling, consent records, delivery status, provider errors, and usage/cost monitoring. Account for per-message costs when designing plans and limits.

**Email:** Verify subscriber email addresses. Support notification preferences, delivery status, bounce/failure handling, and unsubscribe controls where applicable.

Provider choice, legal/compliance requirements, message templates, and cost controls must be reviewed before implementation.

### 2.7 Centralized Notification Engine

Do not make the odds-monitoring engine independently send messages to each delivery provider. Build a centralized notification pipeline:

```text
             ODDS INTELLIGENCE
                     |
              QUALIFIED ALERT
                     |
              NOTIFICATION ENGINE
                     |
           SUBSCRIBER PREFERENCES
                     |
             SUBSCRIPTION CHECK
                     |
               DELIVERY QUEUE
                     |
          +----------+----------+
          |          |          |
        DISCORD      SMS       EMAIL
          |          |          |
          +----------+----------+
                     |
             DELIVERY TRACKING
```

Responsibilities:

- Resolve eligible subscribers and destinations.
- Apply plan entitlements and per-user preferences.
- Queue and dispatch messages independently by channel.
- Prevent duplicate delivery.
- Retry transient failures safely and respect provider rate limits.
- Track delivery status and errors.
- Keep delivery history and usage/cost information.
- Isolate provider outages so one channel does not block another.
- Preserve the current odds collection and analysis pipeline.

### 2.8 Environment Configuration manager (.env settings)

The Admin Console must include a protected **System Configuration / Environment Configuration** area for managing supported environment settings, including API keys and provider credentials.

Expected capabilities:

- View supported settings grouped by category.
- Add, edit, and remove supported variables.
- Mask sensitive values by default, with controlled reveal behavior.
- Preserve secret/key capitalization exactly; API keys are case-sensitive and must never be automatically case-converted.
- Identify settings requiring a service restart.
- Show when a setting was changed and by whom, without logging secret values.
- Offer provider connection tests where safe and supported.
- Provide a **Test Configuration** step before applying proposed changes.

Mandatory validation and safety behavior:

1. Validate dotenv syntax, quoting, separators, duplicate variable names, and malformed entries.
2. Validate required values, types, formats, and allowed values against an explicit configuration schema.
3. Validate provider credentials through a provider-specific test when available; distinguish connectivity/authentication failures from syntax errors.
4. Validate the complete proposed configuration before saving.
5. Reject invalid changes, identify the affected setting, and explain the failure.
6. If validation fails, do not alter the existing working configuration.
7. Write a proposed configuration to a restricted temporary file; validate it before applying.
8. Create a recoverable backup before replacement.
9. Apply a successful replacement atomically and preserve restrictive file ownership/permissions.
10. Keep an audit trail of changes without storing secret values in logs.
11. Provide rollback to the previous known-good configuration.
12. Distinguish saving configuration from applying it/restarting services.
13. After an approved restart, verify service health and provide a recovery path if startup fails.

Security/architecture constraints:

- Do not expose the host `.env` file or unrestricted host filesystem access to ordinary application routes.
- Do not give the web application unrestricted Docker socket access merely to implement configuration editing or restarts.
- The current deployment reads `.env` through Docker Compose at container creation; changing a file alone does not necessarily change the running process environment.
- Inspect the actual deployment and configuration-loading model before selecting a safe, narrowly scoped privileged helper or operator-controlled apply mechanism.
- Never return secret values in API responses, browser state, error messages, audit logs, or diagnostic output.
- Define an allowlist/schema of settings that can be edited; do not permit arbitrary host environment mutation.

Proposed configuration categories:

- Environment Variables
- API Providers
- Discord Configuration
- SMS Configuration
- Email Configuration
- Payment Provider Configuration
- System Settings
- Configuration History

These may be specialized views over one controlled configuration system.

## 3. Proposed Admin Console information architecture

Use a professional, enterprise-style administrative layout consistent with the existing product's visual identity, but with its own refined navigation and workspace.

Proposed navigation:

- **Overview** — active subscribers, new registrations, subscription activity, revenue (when billing exists), notification activity, and system health.
- **Users** — searchable subscriber directory; view/edit accounts; activate, suspend, and manage access; safe account deletion/deactivation policy.
- **Subscriptions** — plans, entitlements, subscription state, renewals, cancellations, and plan changes.
- **Billing** — payment history, failed payments, refunds, and billing-provider status.
- **Notifications** — delivery volume, success/failure rates, channel status, usage, errors, global limits, test sends, and emergency pause.
- **Platform** — provider/API status, application health, and operational settings.
- **System Configuration** — protected Environment Configuration manager described above.
- **Communications** — platform announcements and subscriber notifications.
- **Security** — administrator accounts, roles/permissions, MFA status, and audit logs.

Admin notification controls should include the ability to:

- Monitor successful and failed deliveries by channel.
- Monitor SMS usage and costs.
- Enable/disable individual delivery providers.
- Set platform-wide limits.
- Inspect delivery errors.
- Pause outgoing notifications globally or by channel.
- Send controlled test notifications.
- See which subscribers have connected Discord, verified SMS, and enabled email, subject to privacy/access controls.

## 4. Proposed subscriber experience

The subscriber application remains the customer-facing odds intelligence product. It should include:

- Secure registration, login, email verification, and password recovery.
- Subscription selection and account status.
- Account/profile management.
- Notification channel connections and verification.
- Per-category notification preferences.
- Sports/league and threshold preferences where supported by plan.
- Quiet hours and notification limits where supported.
- Clear subscription entitlements and upgrade/cancellation flows.

Do not expose platform-wide secrets, provider API credentials, internal webhooks, administrative functions, or host/server access to subscribers.

## 5. Billing integration

Stripe is a candidate payment/subscription provider, subject to evaluation and approval.

Potential flow:

1. Subscriber chooses a plan.
2. Subscriber completes hosted/secure checkout.
3. Payment provider sends a verified event/webhook.
4. Platform updates subscription state and entitlements.
5. Renewals, cancellations, failed payments, and refunds update access according to documented rules.

Do not trust browser redirects as proof of payment. Verify provider webhook signatures and make event processing idempotent. Define grace periods, failed-payment handling, cancellation timing, refunds, and plan changes before launch.

## 6. Current architecture findings to account for

Initial read-only review of the `development` branch found:

- The application uses FastAPI and serves a static web dashboard.
- Existing authentication is optional HTTP Basic authentication configured through `AUTH_USERNAME` and `AUTH_PASSWORD`; it is not a subscriber identity/account system.
- Persistent application data is stored in SQLite under `/app/data`.
- Docker Compose reads the host `.env` file when creating the container; the host `.env` is not currently mounted inside the application container.
- The current SQLite database contains odds history and application state such as snapshots, quotes, sent alerts, CLV records, and simulation settlements. It does not yet provide the commercial user/subscription system described here.
- The production operations runbook explicitly describes the current deployment as single-owner/self-hosted and says it is not certified as a multi-tenant subscription service.

These are discovery findings, not reasons to make rushed changes. Before implementation, inspect the complete authentication, configuration, database, deployment, and notification paths and design safe migrations and boundaries.

## 7. Phased implementation roadmap

Each milestone must have a written design, bounded scope, implementation review, automated tests, and acceptance criteria. Do not start the next milestone until the current one is reviewed and accepted.

### Phase 0 — Architecture discovery and product design

- Inventory current routes, API endpoints, authentication middleware, configuration loading, SQLite schema, Docker Compose deployment, persistent volumes, background tasks, alert pipeline, and provider integrations.
- Document current behavior that must not regress.
- Define threat model and trust boundaries for public subscribers, administrators, application process, host configuration, and third-party providers.
- Design user/role/session model and database migration strategy.
- Design Admin Console and subscriber navigation/wireframes.
- Define initial subscription entitlements and notification preference model.
- Decide safe configuration apply/restart architecture.
- Document backup, rollback, and deployment strategy.

**Exit criteria:** Architecture, data model, security boundaries, initial UI design, and migration plan reviewed and approved before feature implementation.

### Phase 1 — Authentication and authorization foundation

**Implementation status (feature branch only):**
- [x] Separate SQLite identity database path; odds-history database remains separate.
- [x] Versioned identity schema with subscriber/admin account classes.
- [x] Argon2id password hashing and account-type-scoped authentication.
- [x] Opaque server-side sessions with hashed tokens, bounded absolute lifetime, idle expiry, and revocation.
- [x] One-time email-verification challenges (hashed at rest, expiry, replay protection).
- [x] One-time password-reset challenges (hashed at rest, expiry, replay protection, session revocation).
- [x] Security audit events for email verification and password reset, without challenge/password values.
- [x] Existing HTTP route inventory and target access policy documented in `docs/route-access-matrix.md`.
- [x] Added a machine-checked target access-policy manifest for all 21 live routes; CI verifies there are no unclassified routes or stale policy entries. This is not active middleware.
- [x] Full CI test suite passed after challenge-flow implementation (run 45445121174); schema migration v2 and the complete MFA/bootstrap test suite also passed on run 45447733756. The latest bootstrap-specific tests are in CI.

**Still required before Phase 1 is complete:**
- [x] Private interactive first-administrator bootstrap command; atomic one-time provisioning, no-echo password prompt, MFA enrollment before admin authentication exists.
- [x] Administrator TOTP enrollment, Fernet-encrypted seed storage, verification, accepted-time-step replay prevention, and ten single-use hashed recovery codes.
- [x] Dedicated `MFA_ENCRYPTION_KEY` configuration and operator setup documentation.
- [x] Documented the explicit legacy Basic Auth to identity-mode cutover gates and rollback procedure in `docs/basic-auth-transition.md`.
- [ ] Email delivery integration for verification and recovery links.
- [ ] Wire login/session routes only after email delivery, rate limits, trusted-proxy handling, and production route integration tests are complete.
- [x] Isolated subscriber/admin host-only Secure/HttpOnly cookies, CSRF/origin helpers, and lazy FastAPI authentication dependencies implemented; dependency-level HTTP tests cover account-class isolation, missing sessions, CSRF, and fail-closed store errors. These dependencies are not wired into existing application routes.
- [ ] Production route-level authorization tests, including wrong account class, inactive users, missing MFA at session creation, and entitlement denial. Rate-limit policy thresholds, HTTP integration, deployment-specific trusted-proxy verification, and email delivery remain outstanding.
- [ ] Review `/api/health`: current payload includes operational/provider and alert diagnostics; separate minimal public liveness from protected detailed diagnostics before identity-mode cutover, preserving existing health-check behavior.

**Exit criteria:** Subscribers cannot access admin routes or APIs; public registration cannot create admins; administrator sessions require MFA; authentication and authorization tests pass. Existing odds dashboard behavior and history remain intact.

### Phase 2 — Admin Console foundation

- Build the separate Admin Console shell, navigation, responsive layout, and overview placeholders.
- Add protected admin-only routes and API boundaries.
- Add administrator profile/security controls.
- Add audit-log foundation for privileged actions.

**Exit criteria:** Admin interface is inaccessible without valid administrator authorization and does not alter existing odds dashboard behavior.

### Phase 3 — Subscriber account foundation

- Add registration, email verification, login, password recovery, and account management.
- Add user status lifecycle (pending, active, suspended, closed/deactivated).
- Add user search and management in Admin Console.
- Define safe account deletion/retention rules and preserve financial/audit integrity.
- Add migration and backup/restore tests.

**Exit criteria:** Individual subscriber accounts work; admin user management is permission-protected; existing odds data remains intact.

### Phase 4 — Environment Configuration manager

- Define an explicit editable-setting schema/allowlist.
- Implement masked secret handling and strict access control.
- Implement dotenv syntax and semantic validation.
- Implement Test Configuration and provider tests where supported.
- Implement temporary-file validation, backup, atomic apply, permissions, audit history, and rollback.
- Implement controlled restart/apply workflow and post-restart health checks.
- Test malformed syntax, duplicate keys, invalid values, provider failures, interrupted writes, restart failures, and rollback.

**Exit criteria:** Invalid changes never replace the working configuration; secrets never leak; valid changes can be safely applied and recovered.

### Phase 5 — Subscription plans and entitlements

- Define and implement plan catalog, entitlements, feature access, and limits.
- Add subscription state and lifecycle model.
- Enforce access server-side in APIs and notification dispatch.
- Build plan and subscription administration.
- Keep final plan names, prices, and exact limits configurable and subject to approval.

**Exit criteria:** Plan permissions are enforced consistently and tested against direct API access.

### Phase 6 — Centralized Notification Engine

- Define alert event contract and subscriber preference schema.
- Add durable delivery queue, idempotency, retries, rate-limit handling, and delivery history.
- Apply subscription entitlement checks and per-subscriber preferences.
- Preserve current platform-level alert qualification and existing Discord lifecycle behavior.
- Ensure a failure in one channel does not block other channels.
- Add test mode and global/channel emergency pause.

**Exit criteria:** Notification dispatch is subscriber-aware, duplicate-safe, observable, and does not regress current odds monitoring or alert lifecycle behavior.

### Phase 7 — Discord, SMS, and email integrations

- Build first-party Open Odds Intelligence Discord application/bot and OAuth2 connection flow.
- Support subscriber-owned Discord servers/channels and permission validation.
- Integrate chosen SMS provider with number verification, consent, STOP/HELP, and cost tracking.
- Integrate verified email delivery and unsubscribe/bounce handling.
- Add delivery status, error handling, retries, and provider health visibility.

**Exit criteria:** Each channel passes end-to-end tests, subscriber destinations remain isolated, and secrets are protected.

### Phase 8 — Subscriber preferences and notification settings

- Build subscriber notification settings UI.
- Add per-channel and per-category preferences.
- Add supported sports/leagues and alert threshold controls.
- Add quiet hours and per-day limits if approved.
- Make plan eligibility clear to subscribers.
- Add connection verification, disconnect, and recovery flows.

**Exit criteria:** Subscribers can reliably control their own destinations and eligible alerts without affecting other users.

### Phase 9 — Billing and subscription automation

- Select and integrate payment provider (Stripe is the current candidate).
- Implement secure checkout and signed webhook processing.
- Handle renewals, cancellation, failed payments, refunds, plan changes, and grace periods.
- Keep payment data with the provider; store only necessary provider references and subscription state.
- Add billing administration and subscriber billing views.

**Exit criteria:** Billing events are verified, idempotent, auditable, and reliably update entitlements.

### Phase 10 — Security, reliability, and launch readiness

- Conduct authorization, tenant-isolation, secret-handling, and configuration-editor security reviews.
- Test notification abuse controls, rate limits, queue recovery, and provider outages.
- Test database migration, backup, restore, and rollback.
- Test application behavior under multiple subscribers and concurrent activity.
- Review privacy, SMS consent, email requirements, Discord platform requirements, and payment compliance.
- Complete production monitoring, incident response, and operational runbooks.
- Do not publicly accept subscribers until commercial-readiness gates are met.

**Exit criteria:** Documented security and operational acceptance, passing automated/integration tests, recoverable deployment, and explicit launch approval.

## 8. Non-negotiable development and deployment safeguards

- All work for this initiative stays on `feature/admin-panel-and-subscriptions`, based on `development`.
- Do not commit directly to `main` or `development` for this initiative.
- Do not merge or promote without explicit user approval.
- Do not make production changes or restart the running deployment during planning/design work.
- Do not run local Docker image builds or ask the operator to build images locally.
- Preserve the existing odds dashboard and monitoring behavior unless a separately reviewed change requires otherwise.
- Never delete, replace, or reset persistent data volumes or the existing SQLite database as part of this work.
- Never use destructive Compose commands such as `docker compose down -v`.
- Use additive, reversible database migrations with backups and migration tests.
- Keep secrets out of Git, logs, browser responses, screenshots, and audit records.
- Preserve case-sensitive API key values exactly.
- Use GitHub Actions for CI and image publishing; verify both CI and image publication before asking the operator to pull a new image.
- Keep `:dev` associated with the development workflow and `:latest` associated with the main/production workflow.
- Make small, reviewable changes and test each milestone before proceeding.
- Pause for discussion whenever a design choice could materially affect security, data, deployment, or existing functionality.

## 9. Open decisions to resolve before implementation

- Final authentication/session technology and administrator provisioning procedure.
- Exact subscriber/admin UI design and visual system.
- Initial plan names, prices, features, limits, and trial policy.
- Which notification categories and subscriber controls launch first.
- SMS provider, email provider, consent and compliance requirements.
- Discord OAuth scopes, bot permissions, and server/channel selection UX.
- Queue technology appropriate to the current deployment and expected scale.
- Configuration helper/apply mechanism that avoids exposing Docker socket or unrestricted host access.
- Which environment variables are safe to edit through the UI and which must remain operator-managed.
- Payment provider choice and billing lifecycle rules.
- Data retention, privacy, account closure, and audit retention policies.

## 10. Current implementation status

### Phase 0 — Architecture and design
- [x] Product roadmap and confirmed decisions documented.
- [x] Existing FastAPI, frontend, authentication, SQLite, Compose, and CI architecture reviewed.
- [x] Identity and access architecture documented in `docs/identity-access-architecture.md`.
- [x] Separate account database selected to isolate subscriber/admin data from odds history.

### Phase 1 — Identity foundation (in progress; dependency, route-policy, and rate-limit primitives tested)
- [x] Added a dedicated `ACCOUNTS_DATABASE_PATH` setting; default is `./data/accounts.sqlite3`.
- [x] Added versioned, transactional schema initialization for identities, sessions, verification/reset challenges, administrator MFA storage, and security audit events.
- [x] Added Argon2 password hashing and fixed account-class-specific authentication primitives.
- [x] Added opaque server-side sessions with hashed tokens, revocation, absolute expiry, and idle expiry.
- [x] Added tests for database isolation, schema versioning/idempotence, email/password validation, account-class separation, session revocation, and idle expiry.
- [x] GitHub Actions CI passed on the current feature-branch head.
- [x] Added the private administrator bootstrap command and MFA enrollment/verification.
- [x] Added one-time identity lifecycle methods for email verification and password reset.
- [x] Reviewed the route access matrix and documented the Basic Auth compatibility/cutover plan before wiring authentication into FastAPI.
- [x] Preserved the existing Basic Auth middleware; isolated session-cookie, CSRF, and lazy FastAPI subscriber/admin dependencies are tested but not yet wired into existing routes.
- [x] Added an explicit access-class manifest for every current route and CI tests that fail on an unclassified new route or stale policy entry.
- [x] Added isolated persistent fixed-window rate-limit primitives with atomic SQLite counters, HMAC-hashed bucket identifiers, and atomic IP/account bucket consumption. Includes generic HTTP 429/Retry-After and fail-closed 503 helpers, plus a lazy app-scoped FastAPI dependency. Requires a dedicated 32-byte `RATE_LIMIT_HASH_KEY`; no production route integration yet.
- [x] Added identity-session regression coverage proving that deactivating an identity immediately invalidates its existing sessions.
- [x] Added spoof-resistant proxy-chain resolution that ignores forwarded headers unless explicit trusted CIDRs are supplied; deployment-specific proxy CIDRs remain unconfigured pending topology verification.
- [x] Added HTTP regression tests locking down legacy Basic Auth compatibility: unset credentials preserve current behavior, configured credentials are required and case-sensitive, partial configuration fails closed, and `/api/health` remains available.

**Latest verified implementation commit:** `a23aff8af15ae3e40b39965770cd4ca264449e18` (CI run `45498133450` passed: 669 tests).

### Next step
Complete route-level authorization tests, choose endpoint-specific rate-limit thresholds and HTTP responses, verify the actual Caddy-to-app peer topology before configuring trusted proxies, and preserve the current Basic Auth middleware. Then implement subscriber and administrator login flows behind the explicit, tested migration boundary. Do not remove legacy Basic Auth or change production deployment until every cutover gate is met and the operator explicitly approves.

## 11. Continuing safeguards

All safeguards in Section 8 remain in force. The current identity foundation is not yet wired into the application and is not a production-ready subscriber authentication system.
