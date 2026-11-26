# Subscriber Feature Entitlements

**Status:** Implemented server-side foundation; commercial plan bundles are a later roadmap phase.  
**Working branch:** `feature/admin-panel-and-subscriptions`

## Purpose

Entitlements are feature grants, not subscription plans. They provide a default-deny authorization layer for existing product routes without deciding plan names, prices, trials, billing terms, or quotas prematurely.

A future plan/subscription service can translate an active plan into a set of these feature keys. Until then, an administrator may grant a specific feature set to an individual subscriber through the protected admin API.

## Feature keys

| Key | Protected capability |
|---|---|
| `dashboard` | Main dashboard page |
| `odds` | Odds/events API |
| `market_limits` | Market limits, changes, and history |
| `market_board` | Market and bookmaker boards |
| `simulation` | Simulation leagues, games, refresh, and run endpoints (refresh itself remains administrator-only) |
| `player_props` | Player props |
| `clv` | Closing-line value |
| `market_movement` | Market movement and movement history |
| `history` | Odds history |
| `opportunities` | Positive expected-value opportunities |
| `arbitrage` | Arbitrage opportunities |
| `premium_data` | Access to provider data classified as premium; does not grant access to a provider capability the platform has not licensed/configured |

The route-to-feature mapping is maintained in `app/entitlements.py`. Tests require every `subscriber_entitled` product route to have exactly one mapping. Unknown routes and unknown feature keys fail closed.

## Storage and evaluation

The versioned identity database stores one row per subscriber/feature grant in `subscriber_entitlements`:

- identity reference;
- feature key;
- UTC start timestamp;
- optional UTC expiry timestamp;
- administrator identity that granted the feature;
- created/updated timestamps.

A feature is active only when the identity is an active subscriber, the grant has started, and its expiry is either absent or in the future. No grant means no access. Deactivating an identity immediately prevents its grants from authorizing requests.

Grant replacement is atomic. Invalid feature keys, non-subscriber identities, non-timezone-aware expiry values, expired timestamps, and non-administrator actors are rejected. Expiry timestamps are normalized to UTC before storage.

## Administrator API

- `GET /admin/subscriber-entitlements?identity_id=<id>` — reads current active grants.
- `PUT /admin/subscriber-entitlements` — atomically replaces the grants for one subscriber.

## Complimentary promotional access

Giveaways, sponsorships, and occasional promotions use the same feature entitlement keys but are explicitly classified as **complimentary** in secret-free audit metadata. They are not Stripe subscriptions, paid plans, payment methods, customers, invoices, or revenue.

- `POST /admin/subscribers/complimentary` creates a subscriber and its selected expiring grants in one identity-database transaction. The MFA administrator must choose a campaign, controlled reason code, features, and a duration from 1–366 days.
- The new identity receives only a random inaccessible initial password hash. A 30-minute single-use activation challenge is sent through the encrypted transactional email queue. The recipient sets their own password; consuming the activation link proves mailbox control and verifies the email.
- `PUT /admin/subscribers/complimentary` adds 1–366 days to the later of the current expiry or now, replaces the feature selection, and records the previous/new expiry, campaign, reason code, and actor.
- The Users directory can filter active complimentary grants and displays campaign, expiry, and remaining days. Expiration is enforced by the same server-side entitlement checks as every other feature grant.
- The ordinary manual entitlement editor cannot clear or extend a complimentary expiry. Use the dedicated audited extension route. Existing accounts without a complimentary grant cannot be converted through the extension endpoint.
- If activation email queueing fails after the account/grant transaction commits, the Admin Console explicitly reports that the account exists but the email was not queued. The administrator can use the existing password-recovery email flow to send a fresh one-time link; retrying account creation is rejected as a duplicate.

All create/update operations require an active MFA administrator session, same-origin CSRF protection, bounded rate limits, strict validation, no-store responses, and actor-attributed audit events. Activation tokens and initial random credentials are never returned to the browser or written to audit metadata.

Both endpoints require an active administrator session created after mandatory MFA. The update additionally requires same-origin CSRF protection. The API never returns subscriber email addresses. Each update records a secret-free `subscriber.entitlements_updated` audit event with the administrator actor, subscriber subject, feature keys, and expiry.

Public registration, subscriber profile changes, and browser-supplied role/entitlement fields cannot grant access.

## Authentication-mode behavior

- `legacy_basic`: mode-aware product dependencies bypass identity storage and preserve the existing Basic Auth behavior exactly.
- `identity`: every product route requires its explicit subscriber/admin session dependency. Subscriber routes also require the mapped feature grant. Missing sessions return 401; missing grants return 403; identity-store errors fail closed.

The production deployment remains on `legacy_basic`. Identity mode must not be enabled publicly until the non-production end-to-end flow, reverse-proxy trust, queue/database recovery, key backup/rotation, and rollback gates pass.

## Deliberately deferred

- plan names, prices, trials, and quotas;
- automatic grants from Stripe or another billing provider;
- organization/team sharing;
- usage metering and per-plan rate limits;
- enforcement of the `premium_data` scope across provider ingestion, derived datasets, caches, APIs, history, and notification delivery (specified in [Provider Data Access Isolation](provider-data-access-isolation.md));
- subscriber self-service plan changes.

These belong to the Plans, Billing, and Subscriber Account roadmap phases.
