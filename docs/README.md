# Documentation Index

Reviewed against the current `main` baseline on 2026-11-27. The same application baseline is synchronized to `dev`.

This index distinguishes implemented behavior from planning documents. A document marked **Design**, **Draft**, **Proposal**, or **Checklist** is not proof that the proposed work is implemented or approved for production.

## Product, operations, and providers

- [Admin Console, Subscribers & Commercial Platform Roadmap](roadmap.md) — current baseline, product decisions, remaining launch gates, and implementation history.
- [Production Operations Runbook](PRODUCTION_OPERATIONS.md) — persistent data, backup/restore, image updates, health checks, security boundary, and commercial launch gates.
- [Free-tier Provider API Keys](FREE_PROVIDER_KEYS.md) — provider setup, case-sensitive key handling, account groups, quotas, and free-tier limits.
- [Controlled Testing Plan](TESTING_PLAN.md) — odds integrity, calculations, provider behavior, alerts, CLV, and identity/billing regression coverage.

## Identity, authentication, and authorization

- [Identity & Access Architecture](identity-access-architecture.md) — subscriber/admin separation, sessions, MFA, authorization, and migration boundary.
- [Identity HTTP Router](identity-http-routes.md) — implemented identity, subscriber, and administrator route inventory.
- [Route Access Matrix](route-access-matrix.md) — application route classifications and server-side access policy.
- [Subscriber Feature Entitlements](entitlement-model.md) — default-deny feature grants, Free bundle, premium-data entitlement, and complimentary access.
- [Legacy Basic Auth Transition and Rollback](basic-auth-transition.md) — `AUTH_MODE` behavior, cutover gates, and rollback.
- [Authentication Environment Profiles](authentication-environment-profiles.md) — development/staging/production policy requirements and current implementation limits.
- [Authentication Rate-Limit Design](auth-rate-limit-design.md) — atomic limiter implementation and trusted-proxy boundary.
- [Authentication Rate-Limit Policy](auth-rate-limit-policy.md) — active staged identity-route thresholds and abuse controls.
- [Health Endpoint Security Design](health-endpoint-security-design.md) — public liveness compatibility and protected-diagnostics proposal.
- [HTTP Session and CSRF Boundary](session-boundary-design.md) — separate session cookies, CSRF rules, and staged runtime behavior.
- [Local Authentication Testing](local-authentication-testing.md) — local identity test setup and development-only message inspection.
- [Identity Staging Acceptance Checklist](identity-staging-acceptance-checklist.md) — non-production deployment and end-to-end acceptance gates.
- [Identity Database Backup and Restore](identity-backup-restore.md) — encrypted backup/restore tooling and key custody.
- [Identity Backup and Key-Recovery Drill](identity-backup-drill.md) — non-production recovery rehearsal; never run against production.

## Subscriber billing and transactional email

- [Transactional Email Delivery](transactional-email-delivery.md) — Resend adapter, encrypted queue, worker, and operator setup.
- [Durable Transactional Email Queue](transactional-email-queue-design.md) — queue schema, encryption, leasing, retries, and remaining reliability gap.
- [Stripe Test-Mode Acceptance](stripe-test-mode-acceptance.md) — isolated test catalog, webhook, Checkout, Portal, and recovery verification.
- [Subscription Lifecycle Policy Proposal](subscription-lifecycle-policy-proposal.md) — **proposal only** for payment failures, cancellation, refunds, and disputes.
- [Subscriber Email Change Design](subscriber-email-change-design.md) — **design draft only**; email-change flow requires separate approval.

## Provider data access and notifications

- [Free and Premium Provider Data Isolation](provider-data-access-isolation.md) — source provenance, account-group classification, subscriber scoping, and remaining acceptance tests.
- [Notification Delivery Architecture](notification-delivery-design.md) — **design draft only** for subscriber-selected email, Discord, and SMS notifications.
- [Environment Configuration Manager](environment-configuration-design.md) — **design draft only** for a protected, validated, audited environment editor; implementation is not authorized.

## Simulation and historical validation

- [Historical Backtesting Engineering Design](BACKTESTING_DESIGN.md) — data requirements and paper-only strategy replay design.
- [Simulator Historical Validation](SIMULATOR_VALIDATION.md) — verified results, chronological train/calibration/holdout workflow, metrics, and limitations.
- [Simulation Results Import](simulation-results-import.md) — dry-run-first CSV import of verified final scores without provider polling.

## Important deployment boundary

The codebase contains the subscriber/admin identity and billing implementation, but the default `AUTH_MODE` remains `legacy_basic`. Identity mode is a staged opt-in and public commercial launch remains gated on non-production acceptance, proxy/TLS verification, backup/restore and key recovery, provider licensing/data-isolation acceptance, approved billing policy, and remaining operational work. Stripe Checkout/Portal activation switches remain default-off. Do not treat a green CI run or merged code as approval to enable these production features.


## Container image channels

The repository publishes exactly two rolling image tags: `ghcr.io/tw1zt3d2four7/open-odds-intelligence:latest` from `main`, and `ghcr.io/tw1zt3d2four7/open-odds-intelligence:dev` from `dev`. The standard `docker-compose.yml` uses `latest`. Feature branches do not publish images.
