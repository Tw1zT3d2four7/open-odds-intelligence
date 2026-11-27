# Subscription lifecycle policy proposal

**Status: proposal only — not approved and not implemented.**

This document is intended to make the remaining billing decisions concrete. It does not change Checkout, Customer Portal, subscription reconciliation, application entitlements, or production behavior. The existing hosted-billing switches remain default-off and live mode remains rejected.

## Recommended initial launch shape

1. Keep account registration and the free-access baseline separate from paid billing.
2. Start with one paid subscription product offering the already-approved monthly and annual intervals. Keep additional Basic/Premium differentiation for a later release unless the product owner explicitly wants multiple paid tiers at launch.
3. Keep the approved all-inclusive prices: **$10.68 monthly** and **$104.05 annually**. Both renew automatically. Do not silently change prices or introduce additional fees.
4. Do not offer a free trial in the first release. Revisit trials after fraud, abuse, conversion, and support behavior are measurable.
5. Keep all paid access controlled by explicit local entitlement records. A Stripe subscription or invoice status is evidence for a policy decision, not an entitlement by itself.

## Payment failure and recovery

**Recommendation: a three-calendar-day grace period** after the first confirmed failed renewal payment.

| Lifecycle point | Proposed behavior |
|---|---|
| First `invoice.payment_failed` | Record the failure and notify the subscriber through their opted-in billing channel(s). Keep existing paid access during grace. |
| During grace | Allow Stripe's configured retry schedule to operate. Send a bounded reminder after 24 hours if still unpaid. Do not send duplicate notices for repeated deliveries of the same invoice event. |
| `invoice.paid` before grace expires | Clear the failure marker, close the recovery state, and retain/restore the entitlement only after the reconciled subscription remains eligible. |
| Grace expires with the invoice still unpaid | Suspend paid-only entitlements. Preserve the account, billing history, and any free-tier access. |
| Stripe reports `unpaid` or the subscription is canceled/ended | Keep paid-only access suspended unless a later verified paid subscription event and approved policy restore it. |

The three-day duration and notification schedule are proposals. They must be approved before an automated entitlement-suspension worker is implemented. Stripe retry settings must be coordinated with this application policy rather than assumed to match it.

## Cancellation and plan changes

**Recommendation: cancel at the end of the current paid period.**

- Subscriber-initiated cancellation should stop the next renewal while retaining the paid entitlement through the recorded `current_period_end`.
- The account page should show the scheduled cancellation date and the Customer Portal should use the same setting.
- Immediate cancellation should be reserved for support/security intervention or a separately approved exception.
- Do not enable self-service plan switching, proration, quantity changes, or subscription pauses in the first release. Add them only after upgrade/downgrade and billing-date rules are approved.

## Refunds and disputes

**Recommendation: manual, case-by-case refunds initially.**

- Do not promise automatic prorated refunds.
- Record refund decisions and their actor/reason in the audit trail.
- A refund must not silently recreate or extend entitlements.
- Define a separate response for disputes and chargebacks before enabling live billing; do not infer a universal access action from an invoice event.

## Required approvals before implementation

- [ ] Confirm whether launch is one paid product or multiple paid tiers.
- [ ] Approve or change the three-day grace period and notification timing.
- [ ] Approve end-of-period cancellation and the immediate-cancellation exception.
- [ ] Approve the manual refund approach and dispute/chargeback handling.
- [ ] Confirm no free trial for initial launch.
- [ ] Approve the exact mapping from each Stripe Price ID to local plan code and entitlement keys.

Until these items are approved, the system continues to record subscription/invoice state without granting or suspending paid feature access automatically.
