# Notification Delivery Architecture — Design Draft

**Status:** Draft for review. This document does not authorize provider integration, production messaging, or changes to subscriber contact data.

## 1. Goals

Open Odds Intelligence should let each subscriber choose where eligible alerts are delivered, without coupling alert generation to a specific messaging provider.

Initial channel targets:

- **Email:** verified subscriber email address.
- **Discord:** an Open Odds-owned Discord application and bot, with subscriber-authorized delivery to a direct message or a selected server channel.
- **SMS:** verified mobile number, explicit consent, and provider-managed opt-out handling.

A subscriber may enable more than one channel and choose channel-specific alert categories. No destination receives alerts until it is verified/linked and explicitly enabled.

## 2. Recommended architecture

Use an outbox-and-dispatcher design:

1. The odds/alert pipeline creates a durable notification event with a stable event ID and idempotency key.
2. The notification preference resolver checks the subscriber's enabled channels, alert categories, quiet hours, and frequency limits.
3. The dispatcher creates one delivery record per eligible destination.
4. Provider adapters send the delivery and return a normalized result.
5. Delivery attempts, retry scheduling, provider references, and sanitized failure codes are recorded.
6. The admin console exposes queue health and delivery outcomes without exposing provider credentials or full destination values.

Alert generation must never wait for a provider API call. Provider outages must not block odds refreshes or model processing.

### Provider adapter contract

Each adapter should implement the same small interface:

- Validate that a destination is linked, verified, opted in, and eligible.
- Send a normalized notification payload with a stable idempotency key where the provider supports it.
- Return one of: accepted, delivered (only when confirmed), retryable_failure, permanent_failure, or suppressed.
- Return a sanitized provider failure code; never persist raw secrets or unnecessary message content.
- Support a health check and an explicit global pause.

## 3. Subscriber preferences and destinations

Keep destination verification separate from alert preferences.

Suggested subscriber controls:

- Enable/disable each verified channel.
- Choose alert categories for each channel (for example: market movement, odds opportunities, arbitrage, and closing-line alerts).
- Choose whether a channel is primary or fallback when multiple channels are enabled.
- Configure quiet hours and a timezone.
- Set a per-channel frequency cap or digest mode.
- Send a test notification only after verification and explicit user action.

Suggested data model (names are provisional):

- notification_destinations: subscriber ID, channel, normalized destination reference, verification/link state, masked display value, consent state, created/verified/disabled timestamps.
- notification_preferences: subscriber ID, channel, enabled categories, quiet-hours settings, timezone, frequency/digest settings.
- notification_events: immutable event ID, event type, minimal payload, idempotency key, created timestamp.
- notification_deliveries: event ID, subscriber ID, destination ID, state, attempt count, next attempt, provider reference, sanitized failure code, timestamps.
- notification_provider_state: provider health and circuit-breaker state.

Do not put full phone numbers, email addresses, Discord tokens, or message secrets in audit metadata.

## 4. Discord — Open Odds-owned bot

The Discord integration should use an application and bot owned by Open Odds Intelligence—not a third-party notification bot.

Recommended initial linking flow:

1. Subscriber opens the Notifications page and requests a one-time, short-lived pairing code.
2. Subscriber invites the official Open Odds bot to their Discord server, or opens a direct message with it.
3. Subscriber uses a bot slash command to submit the pairing code in the intended destination context.
4. The bot validates the code, expiry, one-time use, Discord user/guild/channel identifiers, and the bot's send permissions.
5. The subscriber sees a masked destination and confirms it in the Open Odds account before enabling alerts.

For server-channel delivery, require an authorized server administrator or a user with the appropriate channel-management permission to link the channel. The bot must verify its own ability to send messages in the selected channel. For direct messages, the user must initiate the link and the bot must verify that the DM can be opened.

Keep the bot token server-side only. Store Discord IDs and link state, not bot credentials. Do not request broad OAuth permissions unless the final linking design demonstrates they are necessary.

## 5. SMS — consent and provider gate

SMS should be a separate adapter, not a shortcut around subscriber verification.

Required controls:

- Verify ownership of a phone number with a short-lived one-time code.
- Record explicit, timestamped consent for the relevant message category.
- Support STOP, HELP, and provider-required opt-out behavior.
- Suppress opted-out numbers immediately and prevent administrative retries from bypassing opt-out.
- Mask phone numbers in subscriber and admin views.
- Apply per-number, per-subscriber, and global sending limits.
- Register and configure any required US A2P 10DLC campaign/brand before production traffic.

Twilio and Telnyx are possible providers, but neither is selected by this draft. Provider choice must consider pricing, throughput, deliverability, compliance support, and account ownership.

## 6. Reliability and abuse controls

- Use a durable outbox and idempotency key to prevent duplicate alerts after retries or worker restarts.
- Retry only transient failures, with exponential backoff and jitter.
- Stop retrying permanent failures and mark the destination for subscriber review when appropriate.
- Use provider circuit breakers and a global notification pause.
- Enforce subscriber/channel/category frequency caps before delivery creation.
- Never retry after a verified opt-out or destination unlink.
- Keep delivery history bounded and retention policy explicit; do not silently delete audit events.
- Separate transactional authentication email from optional alert notifications. The existing authentication email queue must not become the general notification queue.

## 7. Admin Console operations

The future Notifications area should expose:

- Provider readiness and health (never provider secrets).
- Queue depth, oldest pending delivery, retry volume, and failure rates.
- Searchable delivery history with masked destinations and sanitized failure codes.
- Global pause/resume and provider-specific pause controls.
- Controlled retry of eligible failed deliveries, respecting opt-outs and idempotency.
- Aggregate channel usage and suppression counts.

Administrative actions that pause providers, retry deliveries, or change global policy must be MFA-admin-only, CSRF-protected, reason-coded, and audit-attributed.

## 8. Suggested rollout

1. Approve the data model, subscriber preference UX, and delivery semantics.
2. Implement the outbox, dispatcher, provider adapter contract, and admin queue visibility with a non-production fake provider.
3. Add verified email alerts as the first real channel, keeping authentication email separate.
4. Add the official Discord bot and pairing flow; test DM and server-channel delivery in a private test server.
5. Select an SMS provider, complete consent/compliance setup, and run a non-production SMS test before enabling any subscriber traffic.

## 9. Decisions required before implementation

- Which SMS provider should be evaluated first: Twilio or Telnyx?
- Should Discord default to a server channel, direct message, or let the subscriber choose both?
- Should alerts be real-time only, or should subscribers also be able to choose digest delivery?
- What default quiet-hours behavior and timezone should new subscribers receive?
- Which alert categories should be independently selectable?

Until those decisions are approved, this remains a design document only. No bot, SMS provider, notification queue, or subscriber messaging integration is activated by this draft.
