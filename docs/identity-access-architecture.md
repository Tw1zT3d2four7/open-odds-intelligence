# Identity & Access Architecture

**Status:** Proposed implementation baseline  
**Working branch:** `feature/admin-panel-and-subscriptions`  
**Scope:** Authentication, account storage, sessions, authorization, and route protection  
**Production impact:** None. This document records design only.

## 1. Existing application constraints

The current application is a FastAPI service with a single HTML dashboard at `web/index.html`. Authentication is currently optional HTTP Basic authentication controlled by `AUTH_USERNAME` and `AUTH_PASSWORD`. When both are blank, the middleware allows application routes through; `/api/health` is always public for the container health check.

The current Compose configuration mounts `./data` at `/app/data`, and the existing odds database defaults to `./data/odds_history.sqlite3`. The odds database contains historical snapshots, quotes, alerts, application state, CLV records, and simulation settlements. It must remain intact.

The existing production operations runbook explicitly describes the release as single-owner/self-hosted and not yet suitable for multi-tenant commercial use. Authentication work must not be represented as commercial readiness by itself.

## 2. Identity model

Use one authentication service and a shared identity schema, but enforce two non-interchangeable account classes:

- **Subscriber:** created only through the public subscriber registration flow after email verification.
- **Administrator:** created only through a private, operator-controlled provisioning command or equivalent one-time bootstrap process. Public registration, account APIs, and subscriber profile updates cannot create or promote an administrator.

The identity record has an immutable account class. Application code must never accept an account class, role, entitlement, or MFA status from a browser request as authoritative.

The subscriber login endpoint accepts only subscriber identities. The administrator login endpoint accepts only administrator identities and requires successful MFA. A subscriber session must never satisfy an administrator authorization check.

## 3. Same-domain, separate experiences

Keep one domain and serve distinct experiences:

- `/login`, `/register`, `/account` — subscriber experience.
- `/admin/login`, `/admin` — separate administrator experience.

The administrator frontend has its own layout, navigation, and protected API namespace. It is not a hidden tab in the subscriber dashboard. Knowing or visiting an administrator URL does not confer access.

## 4. Account database boundary

Create a separate SQLite database at `/app/data/accounts.sqlite3` (configurable by a dedicated path setting) for identity and commercial-platform data.

Initial tables should cover:

- identities and immutable account class;
- password credential hashes and password-change timestamps;
- email verification challenges;
- password-reset challenges;
- sessions and session revocation/expiry;
- administrator MFA enrollment and recovery-code hashes;
- security and administrative audit events.

Later milestones may add organizations/tenants, notification destinations, preferences, plans, entitlements, subscriptions, billing references, and delivery records. Those should be introduced with explicit migrations and ownership rules.

Do not add subscriber identity data to the odds-history tables. Do not replace, truncate, or rebuild `odds_history.sqlite3`. SQLite foreign keys cannot span separate database files, so relationships between account and odds data must be handled by application-level identifiers and explicit ownership checks.

Use versioned, transactional migrations for the account database. Migrations must be additive where possible, tested against a fresh database and a copy of a prior-version account database, and must fail closed on unexpected schema state.

## 5. Passwords and sessions

- Store passwords using a maintained Argon2id password-hashing implementation with parameters selected for the deployment and revisited periodically.
- Never store plaintext passwords or reversible password values.
- Use high-entropy, opaque server-side session tokens. Store only a cryptographic hash of each token in the database.
- Use separate cookie names for subscriber and administrator sessions.
- Cookies must be `HttpOnly`, `Secure`, and `SameSite=Lax`, with host-only scope and `Path=/`. Production must use HTTPS.
- Rotate session identifiers after authentication and privilege-sensitive events.
- Enforce server-side idle and absolute session expiry, logout/revocation, and revocation after password reset or account suspension.
- Protect cookie-authenticated state-changing requests with CSRF tokens and origin checks.
- Apply rate limits and generic authentication errors to reduce brute-force and account-enumeration risk.
- Never log passwords, raw session tokens, MFA secrets, recovery codes, or reset/verification tokens.

The exact session lifetimes and rate limits should be configuration-backed, documented, and tested before launch.

## 6. Administrator MFA and provisioning

Administrator MFA is mandatory, not optional. The isolated identity foundation now implements TOTP enrollment, encrypted seed storage, one-time verification, replay protection by accepted time-step, and ten single-use recovery codes stored only as hashes. The seed is encrypted using Fernet with the dedicated `MFA_ENCRYPTION_KEY`; this key must be generated out of band and supplied through the deployment's secret-management mechanism. It must never be committed, logged, or reused from an odds-provider key.

The first administrator can be created only through the private interactive command `python -m scripts.bootstrap_admin`. It reads the password through a no-echo prompt, atomically refuses bootstrap if an administrator already exists, starts MFA enrollment, and displays the provisioning URI and recovery codes only in the interactive terminal. Do not run it with output redirected or captured. It is not an HTTP endpoint. The administrator must complete enrollment before any administrator login/session route is introduced.

Generate a Fernet key in a trusted environment with:
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
Store the result as `MFA_ENCRYPTION_KEY` outside Git, in the deployment secret store or protected host environment. Losing this key makes enrolled TOTP seeds undecryptable; maintain a secure backup and a documented rotation/re-enrollment procedure.

Administrator bootstrap must be a private operator action, not an HTTP endpoint exposed to the public internet. The bootstrap process must prevent accidental creation of multiple initial administrators and must not print credentials or MFA secrets into logs. Additional administrator creation and role changes must be explicit, authenticated, audited administrative actions.

Consider WebAuthn/passkeys as a later hardening milestone.

## 7. Authorization and route inventory

Replace the current all-or-nothing Basic Auth middleware only after a complete route inventory and access matrix exist.

Every route must be classified as one of:

- public: registration, login, verification/reset initiation, and health probe;
- subscriber-authenticated: account/profile/preferences and entitled subscriber features;
- administrator-only: all Admin Console pages and APIs;
- internal/service-only: narrowly scoped machine-to-machine or operational endpoints.

Authorization must be enforced server-side at the route/dependency/service boundary, not by hiding frontend controls. Default policy is deny. A route added later must not become public by omission.

The health endpoint must expose only minimal liveness information. It must not disclose provider credentials, environment values, internal paths, subscriber information, or operational secrets. Readiness and detailed diagnostics should be separately protected if needed.

Existing odds APIs need explicit product decisions: which information is public, subscriber-only, plan-gated, or administrator-only. Until that policy is implemented and tested, do not expose the application as a commercial multi-tenant service.

## 8. Tenant isolation

Every subscriber-owned record introduced in later phases must carry an explicit owner/tenant identifier. All reads, updates, deletes, exports, and notification delivery lookups must scope by the authenticated principal on the server. Never trust a user-supplied owner ID.

Add negative tests proving that subscriber A cannot read or mutate subscriber B's records, that a subscriber cannot call administrator APIs, and that an administrator session is not accidentally accepted as a subscriber session unless a specific, documented administrative workflow requires it.

## 9. Compatibility and migration from Basic Auth

Do not remove the existing Basic Auth behavior in the first identity commit. First introduce and test the new identity components in isolation. Then design a controlled transition:

1. Identify current deployments and how Basic Auth is configured.
2. Define an explicit migration/compatibility period and operator bootstrap procedure.
3. Add the new routes and test coverage without weakening protection on existing routes.
4. Move routes to the explicit access matrix in small groups.
5. Keep a safe operator recovery path that does not expose a public privilege-escalation endpoint.
6. Remove legacy Basic Auth only in a separately reviewed change after the new authentication boundary is proven.

No production restart, Compose change, or deployment action is part of this design document.

## 10. Required tests before the first production-facing milestone

- Fresh account database initializes with the expected schema and restrictive file permissions.
- Migrations are transactional and safe on a prior-version account database.
- Subscriber registration cannot set administrator status or roles.
- Subscriber login rejects administrator identities; administrator login rejects subscriber identities.
- Administrator login cannot complete without MFA.
- Invalid, expired, revoked, and rotated sessions are rejected.
- CSRF and origin protections reject forged state-changing requests.
- Rate limiting works without revealing whether an account exists.
- Password reset revokes existing sessions.
- Every route matches the access matrix; unknown/unclassified routes fail closed.
- Health checks remain compatible and reveal only minimal liveness.
- Existing API tests and odds database tests continue to pass unchanged.
- A pre-existing odds database remains readable and its record counts/content remain unchanged by account initialization and migration tests.

## 11. Explicit non-goals for the first milestone

The first implementation milestone is not billing, notification delivery, the Admin Console dashboard, the environment editor, or a production migration. It is a small, isolated identity foundation plus tests. Subscriber registration and admin login UI should follow only after the identity and authorization contracts are reviewed.

