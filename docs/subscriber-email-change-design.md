# Subscriber Email Change — Design Draft

**Status:** Draft only. No email-change route, schema migration, or delivery behavior is authorized by this document.

## 1. Safety invariant

The subscriber's existing verified email address remains the account's canonical address until the new address has been verified successfully. A request to change an address must never lock the subscriber out merely because the new address is mistyped, unavailable, or controlled by someone else.

## 2. Proposed flow

1. The authenticated subscriber opens Account and submits the current password plus the proposed new email address.
2. The API requires the subscriber session, same-origin CSRF token, current-password re-authentication, and operation-specific IP/account rate limits.
3. The server normalizes the proposed address and checks uniqueness without revealing whether another account owns it. The public response remains generic.
4. The server creates a short-lived, one-time email-change challenge. Only a token digest is stored in the database.
5. A verification message is queued to the proposed new address. The existing verified address remains unchanged while verification is pending.
6. The subscriber follows the link. The API validates the challenge, expiry, account state, and new-address uniqueness again inside one write transaction.
7. On successful verification, the transaction updates the canonical email, keeps email verification true, consumes all outstanding email-change and password-reset challenges, revokes every active session, records a self-attributed audit event, and creates the required notification outbox entries.
8. The subscriber signs in again using the new email address. The old address receives a security notification that an email change completed.

## 3. Data model

Prefer a dedicated email-change challenge table rather than overloading initial registration verification:

- subscriber identity ID
- normalized pending email address
- token digest
- created/expiry/consumed timestamps

Add the table through a versioned additive identity-database migration. Update backup schema validation and migration tests in the same change.

The email address is personal data. Do not include old or new addresses in security-audit metadata. The audit event should identify the subject and action without duplicating contact information.

## 4. Transactional delivery requirements

The existing transactional email queue encrypts recipient addresses and message payloads, but it currently supports only registration verification and password-reset purposes. Email change requires an explicit queue extension:

- A new-address verification message linked to the email-change challenge.
- An old-address security notice that is not invalidated when the verification challenge is consumed.
- A shared transaction/outbox insertion path so the email update, challenge consumption, session revocation, audit event, and notification queue records cannot partially commit.
- A queue worker rule that validates challenge state only for challenge-linked messages; non-challenge security notices need their own expiry and idempotency rules.

Do not send provider requests while holding a SQLite write transaction. Queue records must be encrypted at rest and provider I/O must remain asynchronous.

## 5. Security controls

- Require the current password before issuing a challenge.
- Keep the old email active until the new address is verified.
- Use a cryptographically random, short-lived, single-use token; persist only its digest.
- Rate-limit requests by source IP and subscriber identity, with a cooldown and daily cap.
- Return a generic response so account existence and address ownership are not disclosed.
- Re-check new-address uniqueness in the final write transaction to close race conditions.
- On completion, revoke every session and require a fresh login.
- Invalidate outstanding password-reset challenges so an older reset link cannot overwrite the new account state.
- Audit the change with actor=subject and no email, token, or password in metadata.
- Send a security notice to the old address without including the new address in full; provide a support/recovery path if the subscriber did not initiate the change.

## 6. Subscriber UI

The Account page should:

- Show the current verified email.
- Require current password and new email.
- Explain that the existing email remains active until verification completes.
- Show a generic “check your new address” result.
- Allow a subscriber to request a replacement link subject to cooldown/rate limits.
- After successful verification, require sign-in with the new address.

Do not offer a “change email immediately” or administrator override that bypasses verification.

## 7. Required tests

- A valid request queues verification only to the proposed address and leaves the canonical address unchanged.
- Invalid current password, invalid email, duplicate email, and rate limits do not disclose account existence.
- Expired, consumed, replayed, or wrong-identity tokens fail closed.
- A concurrent registration or email change cannot claim the same address.
- Successful verification changes the address, preserves verified status, revokes sessions, invalidates reset tokens, records one audit event, and queues the old-address notice atomically.
- Queue failure, provider outage, and worker retry do not partially change account identity or send duplicate notices.
- Backup/restore validation recognizes the new schema version.
- Existing registration verification and password-reset behavior remains unchanged.

## 8. Approval gates

Before implementation, approve:

- Whether the old address receives a notice immediately on request, only after successful completion, or both.
- How long the verification link remains valid and how often a replacement may be requested.
- Whether a subscriber can cancel a pending email change from the existing address.
- The support/recovery path for an email change the subscriber did not initiate.

Until these decisions are approved, email change remains design-only.
