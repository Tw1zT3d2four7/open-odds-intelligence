# Legacy Basic Auth Transition and Rollback Plan

**Status:** Design documented; no authentication middleware or production behavior changed.  
**Working branch:** `feature/admin-panel-and-subscriptions`

## Goals

Replace the current optional HTTP Basic Auth boundary with subscriber/admin identity sessions without unexpectedly exposing the dashboard, locking out the operator, or changing the existing odds data and provider pipeline.

The existing middleware currently behaves as follows:

- `/api/health` is public for container health checks.
- If both `AUTH_USERNAME` and `AUTH_PASSWORD` are blank, every other route is currently allowed.
- If both are configured, every other route requires valid HTTP Basic credentials.
- If only one is configured, application routes fail closed with HTTP 503.

This behavior is part of the current deployment contract and must remain unchanged until the replacement is fully tested.

## Required rollout modes

The future implementation should use an explicit, validated authentication mode rather than silently inferring a migration state from whether credentials happen to be present.

| Mode | Intended behavior | Availability |
|---|---|---|
| `legacy_basic` | Preserve the exact existing Basic Auth behavior, including its current fail-closed handling of partial configuration. Identity routes are not activated. | Initial/default compatibility mode |
| `identity` | Use the new identity/session boundary. Public account entry points are narrowly allowlisted; subscriber and administrator routes require their corresponding session and authorization checks. | Only after all cutover gates pass |

No implicit `open`, `hybrid`, or Basic-Auth-bypass mode should be introduced. In particular, a partially configured Basic Auth setup must never cause the app to become public.

## Cutover gates

Do not switch production to `identity` until every item below is true:

1. The operator has run the private one-time administrator bootstrap and securely stored the MFA encryption key and recovery codes.
2. The administrator can complete password plus TOTP/recovery authentication and establish an administrator session.
3. Subscriber registration, email verification, login, logout, and password recovery have working delivery and rate limits.
4. Separate subscriber/admin cookies, CSRF protection, trusted reverse-proxy/HTTPS behavior, and session revocation are integrated and covered by HTTP-level tests.
5. Every existing route is classified and protected by a server-side dependency or explicit public/internal policy. The health endpoint remains minimal and public.
6. Shared refresh operations have a safe operator/scheduler path before they are removed from ordinary subscriber access.
7. Direct API tests prove that a subscriber cannot access admin or internal routes, a subscriber cookie cannot satisfy admin access, inactive/expired/revoked sessions fail, and plan entitlements are checked server-side.
8. Existing dashboard workflows and odds-history reads are regression-tested against a preserved test database.
9. CI passes, deployment image publication is verified, and a backup/rollback procedure is documented.
10. The operator has a verified way to change the mode back to `legacy_basic` without deleting or migrating the identity database.

## Cutover procedure

1. Back up the deployment configuration and persistent data using the established operator backup procedure. Do not replace or reset the data directory.
2. Confirm both legacy Basic Auth values are still available in the protected deployment configuration for rollback.
3. Confirm the private administrator bootstrap and MFA login work in a non-production test deployment using the same reverse-proxy/TLS shape.
4. Deploy the tested image while remaining in `legacy_basic` mode. Confirm the existing dashboard, health check, odds refresh, history, and alert behavior remain intact.
5. Schedule a maintenance window and notify the operator that the authentication boundary will change.
6. Change only the explicit authentication mode to `identity` using the approved deployment mechanism. Do not remove the legacy credentials during the initial observation window.
7. Verify health, administrator login/MFA, subscriber login, route access, dashboard data, and scheduled provider work.
8. Keep the legacy credentials protected until the observation period is complete and rollback is no longer required.

The mode switch must be an explicit operator action. It must not be triggered by a browser request, subscriber registration, or an automatic migration.

## Rollback procedure

1. If any cutover check fails, restore `legacy_basic` mode using the protected operator deployment configuration.
2. Restore or retain the previous Basic Auth values exactly as they were.
3. Restart/recreate only the application service through the approved deployment process; never use destructive volume commands.
4. Confirm the original Basic Auth prompt and existing dashboard/API behavior return.
5. Preserve the separate accounts database and its audit records. Rollback does not require deleting identity records or reverting schema migrations.
6. Record the failure and return to the feature branch for correction. Do not promote a hotfix directly to `main` or `development`.

## Non-negotiable constraints

- Do not remove or weaken the existing Basic Auth middleware before the identity mode is fully implemented and tested.
- Do not allow an empty or invalid mode to fall back to an unauthenticated state; invalid values must fail closed at startup.
- Do not make health checks depend on browser cookies or Basic Auth.
- Do not let a subscriber session or public route invoke shared provider refresh work.
- Do not expose the bootstrap command or administrator provisioning methods over HTTP.
- Do not alter the odds-history database, its volume, or existing provider/alert behavior as part of authentication cutover.
- Do not log passwords, session tokens, MFA seeds, recovery codes, or API keys.
- Do not deploy, restart production, or switch modes without explicit operator approval.

## Current implementation boundary

The session-cookie and CSRF helpers in `app/auth.py` remain isolated and are not wired to FastAPI. This document specifies the intended transition; it does not add an authentication mode, change middleware, expose new routes, or alter production behavior.
