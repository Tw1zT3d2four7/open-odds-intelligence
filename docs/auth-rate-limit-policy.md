# Authentication Rate-Limit Policy

**Status:** Implemented on the staged `AUTH_MODE=identity` authentication routes; thresholds are conservative defaults and must be reviewed against real deployment traffic before public activation.  
**Implementation:** `app/identity_routes.py::_rate_limit` applies persistent atomic fixed-window counters through `app/rate_limit.py`, using HMAC-hashed bucket identifiers, multi-dimension consumption, and generic fail-closed HTTP responses. Identity routes remain hidden in `legacy_basic` mode.

## Policy goals

- Slow credential stuffing, password guessing, verification-token abuse, and notification-provider abuse.
- Avoid storing raw email addresses, phone numbers, IP addresses, or submitted credentials in rate-limit tables.
- Enforce IP and account-identifier limits atomically so a blocked request does not consume only one dimension.
- Return generic errors that do not disclose whether an account exists, is verified, or is an administrator.
- Fail closed with a generic service-unavailable response if the rate-limit store cannot be used.
- Keep administrator authentication stricter than subscriber authentication.
- Do not trust X-Forwarded-For until the exact reverse-proxy peer and narrow trusted CIDR are verified.

## Proposed starting thresholds

These are the active conservative starting values for a small self-hosted deployment, not a final capacity promise. Review them against expected signup volume, shared-NAT users, email provider quotas, and operational recovery procedures before public activation.

| Operation | IP bucket | Account/destination bucket | Notes |
|---|---:|---:|---|
| Subscriber login | 20 / 15 min | 8 / 15 min | Account bucket keyed by normalized email; same generic failure for unknown email and wrong password. |
| Administrator login (password + TOTP or recovery code) | 8 / 15 min | 5 / 15 min | One combined administrator-login bucket covers password and second-factor attempts; there is no separate MFA verification endpoint or bucket. |
| Subscriber registration | 10 / hour | 3 / hour | Account bucket keyed by normalized email; verification delivery is separately capped. |
| Resend email verification | 5 / hour | 3 / hour | Always return a generic response; do not reveal whether an address is registered. |
| Request password reset | 10 / hour | 3 / hour | Same generic response for known and unknown accounts. |
| Consume password reset | 10 / hour | 5 / hour | Token is one-time and expires; password policy still enforced. |
| Consume email verification | 10 / hour | 5 / hour | Token is one-time and expires. |
| Admin bootstrap | Not an HTTP route | Not an HTTP route | Keep the private interactive one-time operator command; never expose bootstrap over the web. |

Notation: maximum requests per fixed-window duration. Both dimensions must be checked atomically for operations that have both an IP and account/destination bucket. The account bucket must use a stable namespace that separates operation and account class, for example auth:subscriber-login:identity:<normalized-email>; raw identifiers must only be passed to the HMAC digest layer and never stored or logged.

## Additional abuse controls

1. **Email delivery cooldown:** Rate limits above do not replace a per-account resend cooldown. Apply a minimum 60-second cooldown between verification/reset messages and a daily cap per destination. Do not send a message when the request is rate-limited.
2. **Provider circuit breaker:** Pause email sends on provider authentication/configuration failures or sustained provider errors. Do not repeatedly retry permanent failures.
3. **Generic responses:** Registration, login, verification resend, and password-reset request responses must not disclose account existence or account status.
4. **Credential hygiene:** Never include passwords, tokens, OTPs, recovery codes, raw email/phone values, or full IPs in logs or audit events.
5. **MFA recovery:** Recovery codes are one-time; rate-limit attempts and record only the identity and outcome in the security audit log.
6. **Session operations:** Login success rotates to a fresh opaque session; logout/revocation is idempotent. Rate limiting must not prevent a user from clearing their local cookies.
7. **Request bounds:** Apply body-size limits, field length limits, and strict token/OTP formats before expensive work.
8. **No lockout-by-attacker:** Avoid permanent account lockouts. Temporary throttling should expire automatically; provide a safe recovery path and monitor repeated targeted abuse.
9. **Shared network fairness:** IP limits are intentionally higher than account limits to reduce collateral damage for households, offices, and mobile carrier NAT.
10. **Observability:** Record aggregate counts and policy identifiers, not raw bucket keys or secret material.

## Implementation notes and limitations

- `SQLiteRateLimiter.consume_many()` uses a transaction and `BEGIN IMMEDIATE` to atomically check all dimensions and increment counters.
- Route integration is active for subscriber login, administrator login, registration, verification resend/consume, and password-reset request/consume. The administrator login bucket covers both TOTP and recovery-code attempts.
- Bucket identifiers are HMAC-SHA256 digests using RATE_LIMIT_HASH_KEY; the key must be configured with at least 32 bytes.
- The rate-limit database is the separate accounts database, not the odds-history database.
- The current limiter uses fixed windows. A request burst can occur at a window boundary; if observed abuse warrants it, consider a token-bucket/sliding-window implementation as a separately tested change.
- request_peer_ip() ignores forwarded headers by default. Do not configure broad trusted networks or assume Caddy's address. Inspect the live network topology and trust only the exact proxy hop(s).
- Identity-mode route integration has regression coverage for account-enumeration resistance, resend cooldown, and fail-closed limiter behavior; lower-level tests cover bucket atomicity, independent dimensions, expiry, and concurrency.
- Identity mode is still a staged deployment mode. Production remains on `legacy_basic`; do not enable identity mode publicly until reverse-proxy trust, backup/restore, TLS, and end-to-end deployment gates are reviewed.

## Required tests before public activation

- IP and account limits independently deny requests at their configured thresholds.
- A denial in either dimension does not increment the other dimension.
- Window expiry restores access with a correct bounded Retry-After.
- Concurrent requests cannot exceed a bucket limit.
- Unknown and known account paths return indistinguishable response shapes and status codes.
- Raw identifiers, IP addresses, passwords, tokens, and OTPs never appear in the database or logs.
- Rate-limit database/storage errors return a generic 503 and do not bypass authentication.
- Spoofed forwarding headers cannot change the client identity when no trusted proxy is configured.
- Correct trusted-proxy chain behavior is tested against the actual deployment topology before enabling forwarded-header parsing.
- Subscriber and administrator policies use distinct namespaces and thresholds.
