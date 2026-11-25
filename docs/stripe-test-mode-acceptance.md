# Stripe test-mode acceptance runbook

This runbook is for an isolated non-production environment only. It does not authorize production configuration, live charges, public signup, or a deployment. Keep all production Stripe settings untouched.

## 1. Create the Stripe test catalog

In the Stripe Dashboard, switch to **test mode** and create two active USD recurring Prices:

| Internal interval | Amount | Recurrence |
|---|---:|---|
| Monthly | $10.68 USD | Every 1 month |
| Annual | $104.05 USD | Every 1 year |

Copy each exact, case-sensitive `price_...` identifier. The application retrieves each Price from Stripe before creating Checkout and rejects inactive, live-mode, wrong-currency, wrong-amount, or wrong-interval objects.

Do not create additional paid tiers or trials yet. Paid-plan names, entitlement bundles, trials, refund policy, and failed-payment grace-period rules remain product decisions.

## 2. Configure an isolated test environment

Use only Stripe test credentials and test Price IDs. Configure these values in the test environment's secret store (never commit them):

- `STRIPE_SECRET_KEY=sk_test_...`
- `STRIPE_PRICE_MONTHLY_ID=price_...`
- `STRIPE_PRICE_YEARLY_ID=price_...`
- `STRIPE_CHECKOUT_ENABLED=true`
- `STRIPE_CUSTOMER_PORTAL_ENABLED=true`
- `STRIPE_WEBHOOK_LIVEMODE=false`
- `STRIPE_TEST_MODE_ACKNOWLEDGED=true` (explicit operator acknowledgement that this isolated environment must use test mode only)
- `STRIPE_WEBHOOK_SECRET=whsec_...` (test-mode endpoint/CLI signing secret)
- `STRIPE_WEBHOOK_ENCRYPTION_KEY=<Fernet key>`
- `PUBLIC_BASE_URL=https://<isolated-test-host>`

Generate a dedicated webhook encryption key in the isolated environment with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('ascii'))"
```

Keep the Stripe API key's case exactly as issued. Do not reuse a live signing secret, API key, Price ID, customer, or webhook endpoint. The webhook endpoint must be reachable over HTTPS in the test environment. If using Stripe CLI forwarding, use the test-mode signing secret printed by the CLI and forward only to the isolated test app.

Before enabling the subscriber actions, run the read-only catalog/configuration check:

```bash
python -m app.billing_preflight
```

It performs only Stripe Price GET requests, validates both approved recurring Price objects, and prints no secrets or Price IDs. It must exit successfully before proceeding. It does not create a customer, Checkout Session, Portal Session, subscription, or charge.

## 3. Start the isolated webhook listener and worker

Configure the test webhook endpoint to deliver these events:

- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.finalized`
- `invoice.paid`
- `invoice.payment_failed`
- `invoice.voided`
- `invoice.marked_uncollectible`

The webhook handler verifies the signature and mode, then stores the encrypted event in the durable inbox. It does not reconcile events inline. Run the bounded worker separately after the test webhook has been delivered:

```bash
python -m app.billing_worker --limit 50 --max-attempts 8
```

The worker is deliberately opt-in and is not started automatically by the web application.

## 4. Checkout acceptance

Use a dedicated verified test subscriber account.

1. Sign in and open `/account`.
2. Confirm the server-displayed prices are $10.68/month and $104.05/year, both marked automatically recurring, with the annual savings and all-inclusive pricing disclosure.
3. Confirm Checkout and Portal buttons appear only in the isolated environment with all test-mode configuration valid.
4. Start monthly Checkout and complete it with a Stripe-documented test payment method.
5. Confirm the browser returns to the same account. A return URL alone is not proof of payment or entitlement.
6. Deliver and process the subscription and invoice webhooks.
7. Confirm `/auth/billing` shows the mapped customer subscription and invoice state. Confirm the private Admin Billing view shows only aggregate counts.
8. Confirm the application has **not** granted any paid entitlement merely because Checkout completed or the subscription is active.
9. Repeat with a separate test subscriber for annual billing.

## 5. Idempotency and concurrency acceptance

For one test subscriber:

- Retry the same Checkout request with the same idempotency key. It must reuse the same provider idempotency key/session.
- Attempt a second Checkout with a different key while the first session is pending. It must be rejected.
- Attempt concurrent Checkout requests. Only one active reservation/session may be created.
- Confirm the reservation expires with the 35-minute Checkout Session. A new attempt can then be started.
- Confirm an active, trialing, incomplete, past-due, unpaid, or paused subscription blocks another Checkout until the account's subscription lifecycle is resolved.

## 6. Customer Portal acceptance

For a subscriber with a mapped Stripe customer:

1. Open Manage Billing from `/account`.
2. Confirm the Portal opens for that subscriber's mapped customer.
3. Confirm a browser-supplied customer ID cannot change the customer selected by the server.
4. Do not enable or test cancellation, plan switching, proration, or payment-failure access changes until the corresponding product policies are approved.

## 7. Webhook and recovery acceptance

- Deliver duplicate copies of a verified event; the inbox must acknowledge duplicates without double-applying state.
- Deliver an older subscription/invoice event after a newer one; the newer snapshot must remain authoritative.
- Deliver an invalid signature or wrong-mode event; it must be rejected and not enter the inbox.
- Deliver a payment-failed invoice followed by a paid invoice; the failure timestamp must be set and then cleared.
- Stop the worker after claiming an event, allow the lease to expire, then rerun it; recovery must not let a stale worker overwrite the new lease.
- Confirm no webhook body, API key, signing secret, or raw provider error appears in logs or browser responses.

## 8. Exit criteria and shutdown

Acceptance is complete only when the full monthly and annual flows, signed webhook delivery, worker reconciliation, duplicate handling, Portal self-scoping, and recovery tests pass in the isolated environment.

After acceptance, set both feature switches back to `false` until plan entitlements, failed-payment grace-period/access rules, trials/refunds, cancellation semantics, legal disclosures, and production approval are complete. Do not switch to `sk_live_...`, set `STRIPE_WEBHOOK_LIVEMODE=true`, or run live payments as part of this runbook.
