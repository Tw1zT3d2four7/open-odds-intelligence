# Durable Transactional Email Queue

**Status:** Implemented in identity schema v7; CI-tested with the isolated identity routes  
**Working branch:** `feature/admin-panel-and-subscriptions`  
**Scope:** Subscriber verification and password-recovery email only  
**Production impact:** None. This design does not enable public registration or change the live odds pipeline.

## Goals

Move provider delivery out of the authentication request path while preserving one-time challenge semantics, generic public responses, provider circuit protection, and secret-free delivery history.

The queue must survive process/container restarts, tolerate concurrent workers, avoid duplicate delivery after uncertain network outcomes, and never persist a usable verification or reset token in plaintext.

## Queue record

Add an additive identity-database migration for `transactional_email_queue` with:

- opaque queue ID primary key;
- SHA-256 challenge-token digest used only to verify that the corresponding one-time challenge is still active (never the raw token);
- provider constrained to the configured transactional provider;
- identity foreign key with `ON DELETE CASCADE`;
- purpose constrained to `verification` or `password_reset`;
- SHA-256 digest of the associated one-time challenge token, used only to confirm the challenge is still active before delivery;
- encrypted payload BLOB containing recipient, subject, plain-text body, optional HTML body, and challenge expiry;
- stable provider idempotency key;
- state constrained to `pending`, `leased`, `sent`, `failed`, or `expired`;
- attempt count and bounded maximum attempts;
- next-attempt timestamp;
- lease token hash and lease expiry;
- created, updated, sent, and expired timestamps;
- last safe failure code only (never provider response text).

Indexes support due-message claiming, challenge validation, identity cleanup, and retention. Terminal queue rows are pruned after 30 days. Do not store raw challenge tokens, recipient addresses, email bodies, provider credentials, or provider exception text in searchable columns.

## Encryption boundary

Use a dedicated `EMAIL_QUEUE_ENCRYPTION_KEY` Fernet key, independent from `MFA_ENCRYPTION_KEY` and all odds-provider API keys.

- Validate the Fernet key before creating or enqueueing a message.
- Encrypt the complete delivery payload before insertion.
- Resolve the recipient from the normalized identity record before encrypting the payload; never use the raw registration, resend, or recovery request string as the delivery address.
- Restrict the identity database and WAL/SHM sidecars to mode 0600 as already required.
- Never log ciphertext, plaintext payloads, keys, or decrypted challenge links.
- If the key is missing or invalid, do not enqueue and do not send inline as a fallback.
- Document secure backup and rotation/re-encryption requirements before enabling the queue in a deployment.

## Claiming and delivery

Workers claim due messages in a short `BEGIN IMMEDIATE` transaction. The worker validates the dedicated Fernet key before claiming any job, so a missing/invalid key cannot consume or permanently fail pending messages:

1. Expire messages whose challenge expiry has passed.
2. Select one due pending message or a message whose lease expired.
3. Discard queued messages whose challenge digest no longer matches an active, unconsumed challenge.
4. Assign a cryptographically random lease token (store only its hash), increment attempts, and set a short lease expiry.
5. Commit before making network requests.
6. Decrypt and validate the payload, then call the provider outside the database transaction.
7. Mark sent or schedule a retry only when the worker still owns an unexpired lease.

A crashed worker must not hold a message indefinitely. Lease expiry makes the message claimable by another worker.

## Idempotency and retry policy

- Generate the provider idempotency key once when enqueueing and reuse it unchanged for every retry.
- Use the same payload on every retry for that idempotency key.
- Retry only transient transport, provider rate-limit, and provider 5xx failures.
- Treat invalid configuration, authentication/authorization failures, malformed requests, and permanent provider rejection as terminal failures that require operator action.
- Use bounded exponential backoff with jitter and a small maximum attempt count; never retry beyond challenge expiry. The implementation uses eight maximum attempts and a 23-hour idempotency window.
- Do not automatically retry an uncertain outcome after the provider's idempotency retention window. Mark it for operator review instead of risking duplicate delivery.
- Continue using the existing persistent provider circuit breaker. A closed circuit permits normal delivery; an open circuit defers messages; only one worker may perform the half-open probe.
- A provider success followed by a database failure is recovered by retrying with the exact same idempotency key and identical payload.

## Queue worker lifecycle

Run the worker only when `AUTH_MODE=identity` and the transactional provider is configured. It must be an asyncio-managed background task that performs SQLite and provider work off the ASGI event loop, has bounded per-cycle work, handles cancellation cleanly, and never blocks application startup waiting for email delivery.

Do not start a second worker from each request. Multiple application workers may run safely because claiming and leases are database-serialized.

## Public request behavior

Registration, verification resend, and password-reset request handlers should:

- validate CSRF, request bounds, and rate limits first;
- create the one-time challenge and queue record atomically where practical;
- return the existing generic response without revealing whether an address exists;
- never send synchronously or expose provider errors;
- not create an account if the required queue encryption/provider configuration is unavailable.

Verification and reset token consumption remain one-time, expiring, and replay-protected. A queued email whose challenge has already been consumed or expired must be discarded without delivery.

## Delivery history and cleanup

Keep the existing 90-day secret-free delivery history and circuit state. Queue rows should be removed or reduced to a minimal terminal record after a short operational retention period. Do not turn queue payloads into permanent audit records. Identity deletion cascades queue payload deletion.

## Required tests before enabling

- Fresh schema and migration from every supported prior schema version.
- Payload encryption at rest; plaintext recipient, body, and challenge token absent from SQLite bytes/rows.
- Missing, malformed, and rotated encryption keys fail closed.
- Concurrent workers cannot claim the same active lease.
- Expired leases are reclaimable; stale workers cannot complete a lease they no longer own.
- A wrong/missing queue key never clears or permanently fails pending ciphertext; the worker pauses safely until the original key is restored.
- Challenge expiry prevents delivery.
- Restart/recovery preserves pending messages and attempt state.
- Transient retry backoff, maximum attempts, permanent failure, and circuit-open deferral.
- Stable idempotency key and byte-equivalent payload across retries.
- Provider success followed by DB write failure does not create duplicate provider sends.
- No raw email, challenge token, provider response, or key appears in logs, HTTP responses, audit events, or delivery history.
- Existing authentication, Basic Auth compatibility, health probe, odds API, dashboard, and odds-history regression tests remain green.

## Rollout boundary

The encrypted queue, worker, and account endpoints are now available only in staged `AUTH_MODE=identity`; default `legacy_basic` behavior remains unchanged. Existing product/dashboard routes still return 503 in identity mode until server-side subscriber/admin authorization and entitlements are integrated. Challenge creation and queue insertion are currently sequential rather than one SQLite transaction; a queue insert failure leaves the account/challenge in place and can be recovered by requesting a fresh verification/recovery email. Close this small reliability gap before public launch. The queue is not permission to switch production to identity mode. Keep product access fail-closed until all route authorization, entitlements, proxy trust, and rollback gates pass. Do not deploy, restart production, or change the operator's `.env` without explicit approval.
