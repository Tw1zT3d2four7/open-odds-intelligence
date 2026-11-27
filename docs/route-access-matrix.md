# Existing HTTP Route Inventory and Target Access Policy

**Reviewed against:** `app/main.py` on `feature/admin-panel-and-subscriptions`  
**Purpose:** Define and verify the route-by-route authorization boundary for subscriber and administrator sessions.

The policy is active in staged `AUTH_MODE=identity`. Default `legacy_basic` behavior remains unchanged; no production mode switch or deployment has occurred.

## Authentication behavior by mode

- `legacy_basic` (default): preserves the existing `optional_basic_auth` contract exactly. Both configured credentials protect application routes; both blank values preserve the existing open self-hosted behavior; partial configuration fails closed with HTTP 503. Identity pages and identity APIs are hidden with 404, even when Basic Auth is configured.
- `identity`: subscriber/admin entry points are admitted by their separate policy. Product routes are protected by explicit FastAPI dependencies. Subscriber routes require an active subscriber session plus a matching server-side feature entitlement; admin refresh routes require an MFA-backed administrator session; all state-changing cookie-authenticated requests require CSRF. Missing sessions return 401, missing entitlements return 403, and identity-storage failures fail closed with 503.
- Safe GET/HEAD health requests remain independent of browser sessions and Basic Auth. The detailed health payload is retained in legacy mode; identity mode returns minimal liveness only. Framework OpenAPI/Swagger/ReDoc routes require an administrator session in identity mode.

## Route inventory and intended future access

| Current route | Method | Current purpose | Target access class | Notes |
|---|---:|---|---|---|
| `/` | GET | Existing odds dashboard | Subscriber | Keep dashboard available to authenticated subscribers. |
| `/api/health` | GET | Container/application health | Public / minimal | Minimize public response at identity cutover; preserve successful probe response. |
| `/api/limits/refresh` | POST | Trigger market-limit refresh | Administrator only | Background refresh loops call service methods directly. |
| `/api/limits` | GET | Market limits | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/limits/changes` | GET | Market-limit changes | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/limits/history` | GET | Market-limit history | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/odds` | GET | Odds data | Subscriber + entitlement | Preserve query behavior. |
| `/api/refresh` | POST | Trigger odds refresh | Administrator only | Scheduled service loops do not use this HTTP route. |
| `/api/market-board` | GET | Market board | Subscriber + entitlement | Preserve dashboard behavior. |
| `/api/bookmaker-board` | GET | Bookmaker board | Subscriber + entitlement | Preserve dashboard behavior. |
| `/api/simulation/leagues` | GET | Simulation league list | Subscriber + entitlement | Read-only. |
| `/api/simulation/games` | GET | Simulation games | Subscriber + entitlement | Read-only. |
| `/api/simulation/games/refresh` | POST | Refresh simulation schedule | Administrator only | Background synchronization uses internal service methods. |
| `/api/simulation/run` | POST | Run a game simulation | Subscriber + entitlement | Add per-account and plan limits. |
| `/api/player-props` | GET | Player prop data | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/clv` | GET | Closing-line value data | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/movement` | GET | Line movement | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/movement/history` | GET | Line movement history | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/history` | GET | Historical odds data | Subscriber + entitlement | Keep product data separate from administrative records. |
| `/api/opportunities` | GET | Qualified opportunities | Subscriber + entitlement | Alert preferences and plan filters apply separately. |
| `/api/arbitrage` | GET | Arbitrage opportunities | Subscriber + entitlement | Plan and feature entitlements apply server-side. |
| `/openapi.json` | GET | OpenAPI schema | Administrator only | Framework-generated route; never implicitly public in identity mode. |
| `/docs` | GET | Swagger UI | Administrator only | Framework-generated route; review whether to disable in production. |
| `/docs/oauth2-redirect` | GET | Swagger OAuth redirect | Administrator only | Framework-generated route associated with Swagger UI. |
| `/redoc` | GET | ReDoc UI | Administrator only | Framework-generated route; review whether to disable in production. |

## Access class definitions

- **Public / minimal:** Only the health probe. It must not reveal secrets, user records, provider credentials, or detailed operational diagnostics.
- **Subscriber:** Requires an active subscriber identity and valid subscriber session. Feature access is additionally checked against current server-side entitlements.
- **Admin:** Requires a valid administrator identity, completed MFA, an administrator session, and the specific permission required.
- **Internal:** Callable only by trusted background workers or narrowly scoped service credentials; never by a browser subscriber session.
- **Subscriber + entitlement:** Subscriber authentication plus server-side plan/feature authorization. Client-side hiding is not authorization.

## Compatibility and migration requirements

1. Keep Basic Auth until a complete replacement boundary is implemented and tested.
2. A subscriber cookie must never satisfy an administrator route, or vice versa.
3. Keep health checks independent of browser cookies and Basic Auth.
4. Explicitly define configured Basic Auth behavior during migration; do not unexpectedly lock out the operator.
5. Preserve a safe operator/scheduler workflow before moving manual refresh endpoints to administrator-only access.
6. Test every route class, direct API access, wrong account class, inactive accounts, expired/revoked sessions, missing MFA, and entitlement denial.
7. Update this inventory whenever routes are added, removed, or change purpose.

## Current safeguards

- Product-route dependencies are mode-aware and bypass identity storage entirely in `legacy_basic`, preserving existing Basic Auth behavior.
- Subscriber entitlements default to deny. Only an active MFA-enrolled administrator session can update them through the protected admin API; changes are actor-attributed in the security audit log.
- The existing odds-history database and provider/alert pipeline remain separate from the identity database.
- No production configuration, deployment, Compose, or persistent-data changes have been made.

## Reusable dependency boundary (implemented and wired in identity mode)

The dependencies in `app/dependencies.py` and `app/entitlements.py` provide:

- `require_subscriber` — only an active subscriber session from the subscriber cookie.
- `require_administrator` — only an active administrator session from the administrator cookie.
- `require_authenticated_csrf` — HTTPS same-origin double-submit validation for state-changing browser requests.
- `get_identity_store` — lazy app-scoped identity storage; initialization/session lookup failures fail closed with generic HTTP 503.

The identity store creates administrator sessions only after consuming a recent, one-time TOTP or recovery-code proof. The session cookie itself does not grant administrator privileges without a matching active administrator record in the identity store. Mode-aware wrappers preserve legacy behavior without opening the identity database; identity mode attaches explicit session, entitlement, administrator, and CSRF dependencies to all 21 product application routes.

HTTP dependency tests now cover:
- Subscriber/admin cookie separation and cross-class rejection.
- Missing, expired, revoked, and inactive identity sessions.
- Identity-store initialization and lookup failures without leaking internal errors.
- CSRF rejection/acceptance for subscriber and administrator mutations.
- Administrator session creation rejected without MFA proof; valid TOTP proof permits the admin session.
- Subscriber session cannot authenticate an administrator route.

The identity dependencies are now attached to all product APIRoutes in staged identity mode. Mode-aware wrappers bypass identity storage and CSRF for `legacy_basic`, preserving the existing Basic Auth contract; in identity mode, subscriber routes require feature entitlements, admin refresh routes require administrator sessions, and mutating requests require CSRF. Server-side grants default to deny and can be changed only through the MFA-protected admin entitlement API, with actor-attributed audit records. No production mode switch or deployment has occurred.

## Machine-checked policy inventory (implemented and active in identity mode)

`app/route_policy.py` records an explicit access class for all 25 product/framework HTTP routes: 21 application endpoints plus four framework-generated schema/documentation endpoints. The separate `IDENTITY_ROUTE_ACCESS_POLICY` classifies 18 identity entry, authentication, and admin entitlement routes; the combined application manifest contains 43 routes. Classes include `public_minimal` (only health), `subscriber_entitled` (product/data routes), and `administrator_only` (manual shared provider/schedule refresh routes).

CI compares the manifest with FastAPI's route registry, including framework-generated Starlette routes such as OpenAPI, Swagger UI, its OAuth redirect, and ReDoc (not only APIRoute endpoints). New routes cannot silently inherit public access, and removed/renamed routes cannot leave stale policy entries. Unknown method/path pairs return no classification; they are never implicitly public.

The manifest drives explicit dependencies on all product APIRoutes in identity mode. Framework-generated docs routes are protected in middleware because they do not expose the same FastAPI dependency hooks. The separate identity manifest gates the identity router and is also covered by dependency-conformance tests.

The helper in `app/route_authorization.py` assembles FastAPI dependencies from this manifest and is used directly by all product APIRoute decorators. It requires an explicit server-side entitlement dependency for every `subscriber_entitled` route, adds CSRF protection to every authenticated non-safe HTTP method, refuses public mutation routes, rejects unknown policy classes, and requires the entitlement dependency to be callable. It deliberately has no default entitlement allow path. Mode-aware wrappers bypass these checks only in the explicitly selected legacy Basic Auth mode. HEAD requests inherit their corresponding GET access class, preventing a method-based access-policy gap; an HTTP-level test verifies subscriber authentication and entitlement are enforced on an actual HEAD endpoint. Tests verify dependency assembly for every route in the 25-route manifest and cover uncommon non-safe verbs. HTTP integration tests exercise subscriber entitlement denial, administrator session/CSRF enforcement, and the public health route. Legacy Basic Auth HTTP regression tests cover the dashboard, product API, and framework documentation routes, including malformed authorization headers, case-insensitive scheme parsing, and method-scoped health-path exemptions.

## Health endpoint compatibility

The current `GET /api/health` response includes application/provider and alert-delivery diagnostics. The exact Compose health probe in `docker-compose.yml` calls this route and only requires a successful HTTP response; it does not parse the JSON body. At identity-mode cutover, minimize the public response and move reviewed operational details behind administrator authorization while retaining a successful response for the probe. See `docs/health-endpoint-security-design.md`.

## Authentication rate-limit building blocks (implemented and active on identity endpoints)

`app/rate_limit.py` provides persistent fixed-window counters in the accounts database. `BEGIN IMMEDIATE` makes multi-bucket checks/consumption atomic across workers sharing the database. Bucket identifiers are HMAC-SHA256 digests keyed by `RATE_LIMIT_HASH_KEY` (minimum 32 bytes); raw email/IP identifiers are not persisted.

Forwarded client-IP headers are ignored unless explicit trusted proxy CIDRs are provided. The actual Caddy-to-app peer must be inspected before selecting a narrow trusted range. The rate-limit primitives are attached to the isolated authentication endpoints; product-specific rate policies and trusted proxy CIDRs remain pending. Generic enforcement returns HTTP 429 with `Retry-After`; storage failures return generic HTTP 503. Proposed thresholds are documented in `docs/auth-rate-limit-policy.md` and remain inactive.
