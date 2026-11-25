# Transactional Email Delivery

**Initial provider:** Resend  
**Implementation:** `app/email_delivery.py`  
**Scope:** Verification and password-recovery messages  
**Status:** Resend adapter, encrypted durable queue, leased worker, bounded idempotent retry, and isolated-route enqueueing implemented; global identity-mode gate remains closed for product access.

## Provider decision

Use Resend for the first transactional-email integration. Its REST API accepts a narrowly scoped sending API key using Bearer authentication and a JSON request to `POST https://api.resend.com/emails`. The implementation uses Python's standard-library HTTPS client, avoiding an additional SDK dependency. The adapter is isolated so another provider can be added without changing identity storage or route handlers.

Create a dedicated **sending-only** API key restricted to the verified sender domain where the provider supports that restriction. Never use a full-access key for the application.

Official API reference: https://resend.com/docs/api-reference/emails/send-email

## Configuration

- `EMAIL_PROVIDER=resend`
- `RESEND_API_KEY=` — keep exact case; never normalize or log it.
- `EMAIL_FROM=` — verified sender identity, such as `Open Odds Intelligence <no-reply@your-verified-domain>`.

Use `EMAIL_PROVIDER=disabled` to disable delivery intentionally. Do not silently fall back to a local mail command or an unconfigured SMTP relay. A missing API key or sender address is a configuration error when the sender is constructed.

## Security and reliability behavior

- HTTPS only; provider API key is sent in the Authorization header and never placed in URLs or response bodies.
- No SDK or new runtime dependency.
- Recipient, subject, body-size, header-injection, timeout, and idempotency-key validation.
- Provider response bodies and transport exception details are not propagated into application errors or logs.
- Resend idempotency keys are supported for `POST /emails`; keys may be up to 256 characters and are retained by the provider for 24 hours. Retry orchestration must reuse the exact same key and identical request payload. The adapter accepts printable ASCII keys and does not retry automatically.
- Provider message ID is recorded in a separate 90-day identity-database delivery history. Records contain identity reference, purpose, status, provider message ID, safe failure code, and timestamp; they never store email addresses, challenge tokens, request bodies, or provider error text.
- Authentication requests enqueue messages in the versioned identity database. The recipient, subject, body, and challenge link are encrypted with a dedicated `EMAIL_QUEUE_ENCRYPTION_KEY`; the queue stores only ciphertext plus non-secret scheduling/lease metadata.
- A background worker claims messages with atomic SQLite leases, performs provider I/O outside write transactions, and uses a stable idempotency key across retries. Transient transport/rate-limit/5xx failures use bounded backoff; permanent failures terminate. Retries stop before challenge expiry or the provider idempotency retention boundary.
- Queue encryption uses a dedicated Fernet key, independent of the MFA encryption key and all sportsbook API keys. Back up this key securely with the identity database; loss of the key makes pending encrypted messages unreadable.
- A persistent provider circuit opens after five consecutive failures within ten minutes, stays open for ten minutes, and then grants only one worker a 60-second half-open probe lease. A successful send resets the circuit. Circuit-state storage failure blocks sends.
- API keys remain case-sensitive and are preserved byte-for-byte from configuration.
- Tests use a fake HTTP transport; CI never contacts the real provider.

## Current activation boundary

The isolated authentication router enqueues verification and password-reset messages; it does not contact Resend in the request path. The worker runs only when `AUTH_MODE=identity` and `EMAIL_PROVIDER=resend`. The global identity-mode middleware still blocks all existing product/dashboard routes until route authorization and entitlement integration is complete. In legacy mode, identity endpoints remain hidden. The queue does not authorize a production identity-mode cutover.

The isolated router retains per-account resend cooldowns, daily destination caps, atomic IP/account rate limits, generic account-enumeration-resistant responses, and the persistent provider circuit breaker. CI uses a fake sender and never contacts Resend.

## Operator setup before a real delivery test

1. Verify a sending domain with Resend and publish the DNS records it provides.
2. Create a sending-only API key restricted to that domain.
3. Set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `EMAIL_FROM`, `PUBLIC_BASE_URL`, and a dedicated `EMAIL_QUEUE_ENCRYPTION_KEY` in the protected host environment. Generate the queue key out of band with `Fernet.generate_key()`; never reuse the MFA key.
4. Test with an operator-controlled mailbox in a non-production deployment with the actual TLS/reverse-proxy topology.
5. Confirm delivery, spam placement, bounce handling, and provider event/webhook strategy before public registration is enabled.
