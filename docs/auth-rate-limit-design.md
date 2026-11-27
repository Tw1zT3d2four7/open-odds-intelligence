# Authentication Rate-Limit and Proxy Trust Design

**Status:** Atomic rate-limit primitives and policy are integrated into the staged `AUTH_MODE=identity` authentication routes. Forwarded-proxy trust remains disabled until the actual deployment peer/CIDR is verified. Reviewed 2026-11-27.

## Goals

Protect subscriber registration, login, email verification, password recovery, and administrator MFA from brute force, credential stuffing, enumeration, and automated abuse. Preserve the current Basic Auth middleware and existing product behavior until the complete identity cutover is reviewed.

## Implemented primitives

- `app/rate_limit.py` provides fixed-window counters in the separate accounts database. It never writes to the odds-history database.
- SQLite `BEGIN IMMEDIATE` transactions make counter increments atomic across application workers sharing the same accounts database.
- Each bucket key is stored as an HMAC-SHA256 digest. A dedicated `RATE_LIMIT_HASH_KEY` of at least 32 bytes is required to instantiate the limiter. It must be generated randomly and stored as a deployment secret, never committed or logged.
- The limiter returns allow/deny, remaining count, and retry-after seconds. `consume_many()` checks and consumes IP/account buckets atomically so a denied identity bucket does not partially consume the IP bucket. `enforce_rate_limit()` maps denials to generic HTTP 429 responses with `Retry-After` and storage failures to generic fail-closed HTTP 503 responses. A lazy app-scoped FastAPI dependency reuses an injected limiter or initializes one from the dedicated secret. Policy thresholds are defined in `docs/auth-rate-limit-policy.md` and applied by the identity route handlers; forwarded headers are not trusted by default.
- Independent bucket dimensions can be generated for the direct client IP and normalized account identifier. Raw identifiers are not persisted.
- SQLite database and active WAL/SHM sidecars are restricted to mode 0600. Rate-limit windows are capped at 30 days, and abandoned bucket rows older than 90 days are pruned using an indexed timestamp.

## Proxy trust

- Forwarded headers are untrusted by default. `X-Forwarded-For` and `X-Real-IP` do not affect the client address unless trusted proxy CIDRs are explicitly supplied.
- When a proxy chain is explicitly trusted, the resolver walks the forwarded chain from right to left and stops at the first untrusted hop.
- Malformed forwarded chains or invalid trusted CIDR configuration fall back to the direct TCP peer; they never cause arbitrary forwarded values to be trusted.
- Do not guess the trusted CIDR for the production Caddy-to-container path. Before configuring one, inspect the actual peer address seen by the application in the deployment topology and trust only the narrow proxy address/range required.
- A deployment with multiple independently trusted proxy hops must document each hop and ensure the edge proxy overwrites or safely appends the forwarding header.

## Policy decisions still required

Before wiring routes, define separate thresholds and windows for:
- Subscriber login, by IP and account identifier.
- Registration, by IP and normalized email.
- Password-reset requests, by IP and normalized email.
- Email-verification resend and token attempts.
- Administrator login and TOTP/recovery-code attempts.
- Any public integration or webhook endpoints, with separate authentication and abuse controls.

Use generic authentication responses to avoid account enumeration. Return HTTP 429 with a bounded `Retry-After` value when a configured limit is exceeded. Avoid permanent account lockouts that attackers could use for denial of service; use short escalating cooldowns and alerting for administrators. Ensure successful authentication does not erase evidence of abusive attempts from other dimensions.

## Deployment and scale constraints

- `RATE_LIMIT_HASH_KEY` must be provisioned before enabling the limiter. Rotation invalidates existing bucket hashes and effectively resets active counters; document this operational effect.
- SQLite counters are suitable only while all workers share the same durable accounts database and locking semantics. A multi-host deployment needs a shared atomic store such as Redis or another purpose-built rate-limit backend before horizontal scaling.
- The limiter is consumed by identity authentication routes only when `AUTH_MODE=identity` is enabled. The default `legacy_basic` behavior is unchanged. `RATE_LIMIT_HASH_KEY` must be provisioned before identity-mode use; do not enable forwarded-header parsing until the real reverse-proxy topology has been observed and narrowly configured.
