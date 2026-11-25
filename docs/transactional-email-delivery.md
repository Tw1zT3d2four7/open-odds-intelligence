# Transactional Email Delivery

**Initial provider:** Resend  
**Implementation:** `app/email_delivery.py`  
**Scope:** Verification and password-recovery messages  
**Status:** Provider adapter implemented; no account routes send mail yet.

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
- No blind automatic retry after a network timeout. The provider may have accepted the message before the connection failed; retry orchestration must use a stable idempotency key to avoid duplicate sends.
- Provider message ID is returned to the caller for later delivery tracking.
- API keys remain case-sensitive and are preserved byte-for-byte from configuration.
- Tests use a fake HTTP transport; CI never contacts the real provider.

## Not yet enabled

The adapter is not called by public HTTP routes. Registration and recovery must not create a challenge and then silently lose the token. Route integration must add per-account resend cooldowns, daily destination caps, atomic IP/account rate limits, generic account-enumeration-resistant responses, and a provider circuit breaker before email delivery is exposed.

## Operator setup before a real delivery test

1. Verify a sending domain with Resend and publish the DNS records it provides.
2. Create a sending-only API key restricted to that domain.
3. Set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, and `EMAIL_FROM` in the protected host environment.
4. Test with an operator-controlled mailbox in a non-production deployment.
5. Confirm delivery, spam placement, bounce handling, and provider event/webhook strategy before public registration is enabled.
