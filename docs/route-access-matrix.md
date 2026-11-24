# Existing HTTP Route Inventory and Target Access Policy

**Reviewed against:** `app/main.py` on `feature/admin-panel-and-subscriptions`  
**Purpose:** Establish a route-by-route authorization baseline before introducing subscriber or administrator sessions.

This is a design artifact only. It does not change middleware, endpoint behavior, or the existing HTTP Basic Auth compatibility layer.

## Current authentication behavior

The current `optional_basic_auth` middleware protects application routes only when both `AUTH_USERNAME` and `AUTH_PASSWORD` are configured. `/api/health` is intentionally public for container health checks. If both credentials are unset, the current middleware permits requests. This behavior must not be silently changed during identity integration.

## Route inventory and intended future access

| Current route | Method | Current purpose | Target access class | Notes |
|---|---:|---|---|---|
| `/` | GET | Existing odds dashboard | Subscriber | Keep existing dashboard available to authenticated subscribers. |
| `/api/health` | GET | Container/application health | Public / minimal | Return no secrets, provider credentials, or private account data. |
| `/api/limits/refresh` | POST | Trigger market-limit refresh | Admin or internal | Do not let each subscriber trigger shared provider work or consume provider quota. |
| `/api/limits` | GET | Market limits | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/limits/changes` | GET | Market-limit changes | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/limits/history` | GET | Market-limit history | Subscriber + entitlement | Apply sport/feature entitlements server-side. |
| `/api/odds` | GET | Odds data | Subscriber + entitlement | Preserve current query behavior. |
| `/api/refresh` | POST | Trigger odds refresh | Admin or internal | Shared provider operation; avoid subscriber-triggered quota exhaustion. |
| `/api/market-board` | GET | Market board | Subscriber + entitlement | Preserve existing dashboard behavior. |
| `/api/bookmaker-board` | GET | Bookmaker board | Subscriber + entitlement | Preserve existing dashboard behavior. |
| `/api/simulation/leagues` | GET | Simulation league list | Subscriber + entitlement | Read-only. |
| `/api/simulation/games` | GET | Simulation games | Subscriber + entitlement | Read-only. |
| `/api/simulation/games/refresh` | POST | Refresh simulation schedule | Admin or internal | Shared schedule refresh; protect provider/resource usage. |
| `/api/simulation/run` | POST | Run a game simulation | Subscriber + entitlement | Add per-account rate limits and plan limits before multi-tenant launch. |
| `/api/player-props` | GET | Player prop data | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/clv` | GET | Closing-line value data | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/movement` | GET | Line movement | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/movement/history` | GET | Line movement history | Subscriber + entitlement | Apply sport/feature entitlements. |
| `/api/history` | GET | Historical odds data | Subscriber + entitlement | Keep subscriber access scoped to product data, not administrative records. |
| `/api/opportunities` | GET | Qualified opportunities | Subscriber + entitlement | Future alert preferences and plan filters apply separately. |
| `/api/arbitrage` | GET | Arbitrage opportunities | Subscriber + entitlement | Future plan and feature entitlements apply server-side. |

## Access class definitions

- **Public / minimal:** Only the health probe. Must not reveal secrets, user records, provider diagnostics containing credentials, or internal configuration.
- **Subscriber:** Requires an active subscriber identity and valid subscriber session. Feature access is additionally checked against the subscriber's current subscription entitlements.
- **Admin:** Requires a valid administrator identity, a completed MFA challenge, an administrator session, and the specific permission required for the operation.
- **Internal:** Callable only by trusted background workers or narrowly scoped service credentials; never by a browser subscriber session.
- **Subscriber + entitlement:** Subscriber authentication plus a server-side plan/feature check. Client-side hiding is not authorization.

## Compatibility and migration requirements

1. Keep the existing Basic Auth middleware in place until a complete replacement boundary is implemented and tested.
2. Do not let a subscriber cookie satisfy an administrator route, or an administrator cookie silently become a subscriber session.
3. Keep `/api/health` independent of browser authentication for container health checks.
4. When new authentication is integrated, explicitly define how configured legacy Basic Auth behaves during migration. Do not unexpectedly lock out the current operator.
5. Before moving shared refresh endpoints to Admin/Internal, provide an operator workflow or scheduler path that preserves the current refresh capability.
6. Add tests for every route class, including direct API calls, wrong account class, inactive accounts, expired/revoked sessions, missing MFA, and entitlement denial.
7. This inventory must be updated whenever routes are added, removed, or change purpose.

## Explicit non-goals at this stage

- No authentication middleware changes.
- No new login, registration, admin, or account HTTP routes.
- No endpoint behavior or dashboard changes.
- No changes to production configuration, deployment, Docker Compose, or persistent data.


## Inventory verification and session-boundary implementation

The route inventory was rechecked against every `@app.get/post/put/patch/delete` decorator in `app/main.py` on 2026-11-24: all 21 current routes are represented above. There are no additional FastAPI route modules under `app/` at this point.

The isolated helpers in `app/auth.py` now provide separate subscriber and administrator host-only session cookies, account-class-specific session resolution, and same-origin double-submit CSRF validation. Their design and HTTPS/reverse-proxy requirements are documented in `docs/session-boundary-design.md`.

These helpers are not yet connected to middleware or endpoints. The existing optional Basic Auth middleware remains unchanged. The target access classes in this matrix remain policy intent until route dependencies and the compatibility transition are implemented and tested.
