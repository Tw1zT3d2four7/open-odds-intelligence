# Free and Premium Provider Data Isolation

**Status:** Approved architecture with core source provenance, provider-account scoping, and subscriber query enforcement implemented in the current `main`/`development` baseline. Remaining cross-surface acceptance and staging gates are required before external subscriber testing. Reviewed 2026-11-27.

## Product rule

Open Odds Intelligence has two independent authorization dimensions:

1. **Subscriber entitlement** answers what a customer is allowed to access.
2. **Provider capability** answers what the platform is licensed and technically able to retrieve from each configured provider account.

A subscriber must satisfy both. A paid subscription cannot manufacture provider coverage that the platform has not purchased or configured. A premium provider credential must never make premium data visible to a free subscriber.

## Customer access matrix

| Customer access | Eligible source classes |
|---|---|
| Free | Fixed server-owned Free feature bundle; Free provider data only |
| Monthly paid | Free + Premium provider data while the configured monthly Stripe price is active |
| Annual paid | Free + Premium provider data while the configured annual Stripe price is active |
| Complimentary grant | Exactly the explicitly granted features and data classes, with a mandatory expiry |

Free must remain useful and operational with no premium credentials, paid provider accounts, or paid provider spend configured. Monthly and annual plans have the same provider-data class; billing cadence changes price and renewal, not data classification. The approved monthly/yearly prices remain governed by the billing roadmap.

Complimentary grants are separate from Stripe. A complimentary account receives premium data only when the administrator explicitly grants the premium-data entitlement, with the existing bounded expiry and audit controls. The UI and audit record must make this grant's potential provider-cost impact clear.

## Provider-side classification

Classification belongs to a configured provider account/credential capability, not merely a provider brand. A provider may offer different endpoints, markets, leagues, history windows, or refresh limits under different plans. Therefore:

- Every configured provider account or endpoint capability must have an explicit access class: `free` or `premium`.
- Missing, invalid, or unknown classification fails closed; it must never default to premium visibility or silently elevate a source.
- Record provider identity, account-group identity (non-secret), endpoint/capability, access class, provider plan/capability metadata where known, and fetch timestamp as provenance.
- Keep provider API keys case-sensitive and never include raw keys in provenance, logs, UI, cache keys, or audit records.
- The provider's actual terms, quota, and endpoint capabilities must be verified before a capability is advertised. Do not infer plan access from a provider name.

The same provider can contribute free-class and premium-class data through distinct account/endpoint configurations. Do not collapse those sources into a single provider-level trust label.

## Data and delivery enforcement

Classification must survive the complete data lifecycle:

- ingestion and normalization;
- quote-level source attribution and provider merging;
- immutable snapshots and historical records;
- derived market aggregates, best prices, consensus/fair values, opportunities, arbitrage, movement, limits, CLV, and simulation inputs/results where source data contributes;
- HTTP APIs, dashboard server rendering, WebSocket or streaming updates if added, notification/Discord/email/SMS payloads, exports, and future integrations;
- caches, background jobs, precomputed summaries, and admin diagnostics.

Enforce authorization on the server at the data/query boundary. Frontend hiding is not a security boundary. For derived records, preserve the highest source classification required by any contributing input (a result combining premium and free inputs is premium). Where a safe free-only recomputation is possible, compute it independently from free-class inputs rather than downgrading a mixed aggregate.

Premium data must not contaminate free responses through a shared cache, precomputed aggregate, historical endpoint, or alert. Cache keys and materialized datasets must include the effective access class or be isolated by subscriber entitlement. Never serve a premium-populated cache to a free caller.

Legacy records without reliable source classification must be treated as unknown/restricted until safely backfilled from trustworthy provenance. Do not assume old provider labels prove the plan tier.

## Runtime access decision

The effective data scope is the intersection of:

- active subscriber status;
- active plan or complimentary entitlement for premium data;
- the route/feature entitlement;
- the configured provider-source classification;
- current provider availability and licensed capability.

Free users receive free-class records only. Paid users receive free and premium records. A paid user still receives free-only results when premium sources are unavailable or not configured; the product must degrade gracefully rather than fail the whole response.

The legacy single-owner Basic Auth deployment must retain its existing behavior until identity mode is explicitly enabled. The new source-class enforcement must not silently change the existing operator workflow.

## Cost and quota protection

Premium provider spend is a platform operating cost, not an unlimited per-subscriber allowance. Before enabling premium ingestion:

- track usage, quota, refresh cost, and remaining allowance by provider account group and capability;
- retain account-wide quota semantics across multiple keys;
- enforce provider schedules and request budgets independently;
- provide operator-visible cost/usage guardrails and alerting;
- prevent one subscriber's request pattern from triggering unbounded provider polling;
- use shared scheduled ingestion where permitted, with bounded per-account request/response usage;
- document how provider costs scale against active paid subscribers and the expected plan revenue.

Never rotate keys from one provider account to evade account-wide limits or provider terms. Free provider operation must not depend on premium provider configuration.

## Required acceptance tests

Before external subscriber testing, add automated coverage proving:

1. Free-only source data is visible to free and paid subscribers.
2. Premium source data is visible to paid subscribers only when the premium-data entitlement is active.
3. Free operation succeeds with no premium keys/accounts configured and causes no premium provider requests.
4. A paid subscriber falls back to available free data when premium sources are unavailable.
5. Revocation, cancellation/downgrade, and expiry immediately prevent premium access, including historical records.
6. Complimentary premium access requires explicit grant and stops at expiry.
7. Mixed-source aggregates are premium-class; free-only aggregates are independently computed from free inputs.
8. HTTP endpoints, dashboards, histories, derived market APIs, notifications, exports, and background delivery cannot leak premium records.
9. Cache isolation prevents premium-to-free cross-contamination.
10. Unknown/unclassified legacy provenance fails closed.
11. Provider account quota remains account-wide across multiple keys and refresh schedules.
12. Provider secrets remain masked and case-preserved.

## Implementation sequence

1. Inventory every provider ingestion path, persisted record, derived calculation, cache, API, dashboard, and notification path.
2. Add versioned source-capability configuration and durable provenance/access-class storage with a safe migration for existing SQLite databases.
3. Add a single reusable server-side data-scope policy and query helpers; do not scatter ad hoc role checks across endpoints.
4. Make provider merge and all derived calculations classification-aware.
5. Apply scope enforcement to every customer-visible route and delivery path, including historical data.
6. Add cost/quota controls and admin visibility before enabling any premium provider schedule.
7. Add the acceptance tests above and run the full CI suite.
8. Only then proceed to isolated HTTPS staging and external tester acceptance.

## Non-goals and safety gates

- This design does not authorize purchasing provider subscriptions, entering premium credentials, changing provider contracts, enabling premium polling, enabling live Stripe billing, deploying production, or merging branches.
- Do not promise a specific league, book, market, prop, refresh interval, or historical depth until verified against the actual provider plan and observed response.
- Continue implementation and maintenance from the promoted `main`/`development` baseline. Code promotion does not authorize production provider changes, premium polling, or commercial activation.


## Implementation update — 2026-11-26

The feature branch now persists market-limit provenance alongside each posted-limit observation:

- source_provider identifies the provider source.
- Legacy market-limit rows may contain the older non-secret configured-keyset fingerprint in source_account_group; those fingerprints are not mapped to current account labels and legacy access classifications remain restricted. New observations store the normalized leased account-group label, which is independently classified and scoped.
- Quote snapshots and CLV entries created before account-group provenance was available, or written without both provider and account-group labels, are also restricted. New database writes enforce this rule so an old provider-wide free/premium label cannot silently authorize an unknown account.
- source_access_class records free, premium, or restricted.
- source_capability identifies the posted_market_limits capability.
- Existing SQLite databases receive additive columns. Historical rows without trustworthy classification retain the restricted default.

The current-limits query, change-history window, and exact-selection history apply the caller's free/premium scope before selecting latest observations or calculating changes. Change windows are partitioned by source metadata so separate source streams are not compared as if they were one feed. Identity-mode free subscribers receive only free-class limit records; paid subscribers receive free and premium records; restricted/unknown records remain hidden from both subscriber tiers. The limits endpoint also suppresses premium-source quota and diagnostic details from free subscribers.

Automated coverage includes free-versus-paid current limits, exact-selection history, source-partitioned change windows, restricted legacy rows, and premium diagnostic redaction. CI run 45746913326 passed **990 tests with 1 non-blocking warning**.

Premium-specific CLV tracking and per-account access-class enforcement are now implemented and covered by CI. Provider quota telemetry and configurable reserve floors are also implemented; provider-specific monetary cost estimates and isolated HTTPS staging acceptance remain outstanding. Premium polling remains disabled by default, and this implementation does not authorize provider purchases, production deployment, merge, or live billing.


### Provider account-group key handling — 2026-11-26

Provider key pools accept optional `group|API_KEY` entries in the existing comma/newline-separated key settings. Keys without a group remain in the shared `default` group. The service now passes the raw configured key-entry strings into the pool rather than flattening them first, preserving group identity for quota cooldowns and status reporting. A quota response from one key pauses every configured key in that same group, while keys in a different group remain independently eligible. API key bytes remain case-preserved; group labels are metadata, not credentials.

This was the foundation for the later account-tier enforcement described below. Group labels now flow through quote ingestion, storage, refresh replacement, market limits, CLV, and score synchronization. Account-specific access-class overrides and premium polling gates are enforced at lease time; do not enable premium polling in production before isolated staging acceptance.


### Tier-separated CLV tracking — 2026-11-26

CLV entries now use provider, account group, and access class as part of their exact-selection identity, allowing observations from separate provider accounts and tiers for the same event/book/market/outcome/line to be stored independently. Existing SQLite databases are migrated transactionally by rebuilding the CLV table, preserving row IDs and historical values while assigning legacy unknown rows to the restricted class. The migration recreates the pending index and changes spread duplicate detection to include access class.

Refresh processing now calculates free CLV from the free-only event view. Premium CLV is calculated from a premium-only projection of the paid view, with non-premium outcomes removed before market consensus is computed. This prevents a premium entry's fair price from being derived from free-tier quotes. Closing-consensus snapshot and quote selection are restricted to the entry's access class, while same-tier account groups remain distinct entry provenance. Subscriber-facing report scope remains free-only or free-plus-premium. CLV rows now also retain the source provider and non-secret account-group label for the entry quote, with an additive migration that preserves existing records and leaves legacy provenance blank.

These changes are implemented and covered by the 1,011-test CI checkpoint recorded below. Per-account classification and independent account-group polling are now implemented as described in the following section.


### Provider account-tier enforcement — 2026-11-26

The provider ingestion path now preserves the non-secret account-group label from the leased API key on every quote. ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES accepts comma/newline-separated entries in the form provider|group=free or provider|group=premium. For example:

```dotenv
ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES=the_odds_api|free=free,the_odds_api|pro=premium
ODDS_PREMIUM_PROVIDER_POLLING_ENABLED=false
```

When a provider has account-specific overrides, any unlisted account group is classified as restricted. The same allowlist controls key acquisition: with premium polling disabled, only groups explicitly classified as free can be leased for manual or scheduled odds refreshes. Enabling premium polling makes explicitly premium groups eligible as well; restricted/unknown groups remain ineligible. This prevents a free refresh from accidentally rotating into a premium account. Group labels are normalized as non-secret identifiers; credential values remain case-sensitive.

Merge identities include both access class and account group, so quotes from separate same-tier accounts are retained independently and an incremental refresh replaces only the leased account group's old quotes. The SQLite quote archive and history now persist source_account_group and size_source_account_group; CLV entries preserve provider and account group in their uniqueness identity and report. Closing consensus remains tier-scoped (free or premium), rather than requiring every peer bookmaker to come from the same account group. Provider-level access classes remain the backward-compatible fallback only when no account-specific overrides exist for that provider. The standalone OddsLink posted-market-limit poller uses the same eligible-group rules and stamps each returned quote with its leased account group and access class; a premium-only account cannot be selected when premium polling is disabled. TheRundown final-score synchronization also uses the same allowlist, so a score-only background job cannot bypass the premium-polling switch.

Provider quota reserve floors can be configured per account group using ODDS_PROVIDER_ACCOUNT_QUOTA_FLOORS. Example:

```dotenv
ODDS_PROVIDER_ACCOUNT_QUOTA_FLOORS=the_odds_api|premium=credits_remaining:250,oddize|premium=monthly_objects_remaining:1000
```

When a provider reports the configured remaining-quota metric at or below its floor, the entire account group is paused until the provider-reported reset time (with a 24-hour fallback if no reset is supplied). The pause survives successful HTTP responses and service restarts. The metric must match a supported provider-reported remaining counter; no dollar cost is inferred from it. Actual monetary cost estimates still require provider-specific pricing and billing data.


Automated merge, key-pool, database migration, CLV, and Admin Overview tests cover same-provider free/premium coexistence, independent same-tier account groups, fail-closed unknown groups, eligible-group polling, quota reserve floors, and credential sanitization. CI run 45760810581 at commit 0d89337bf3d45be5360ffe7f99cd0e08a10544da passed 1,025 tests with one non-blocking warning. Do not enable mixed-tier account configuration or premium polling in production until isolated staging acceptance passes.



## Isolated HTTPS staging acceptance checklist

Run this checklist only in the dedicated non-production staging environment described in docs/roadmap.md. Do not point it at production keys, production data, or the production domain.

1. Keep ODDS_PREMIUM_PROVIDER_POLLING_ENABLED=false. Configure one explicitly free and one explicitly premium account group for a provider. Confirm scheduled and manual refreshes lease only the free group; confirm the premium key remains available and is never called.
2. Confirm a free subscriber receives only free quotes, quote history, market-limit observations, movement data, and CLV entries. Confirm a paid test subscriber receives free plus premium data. Verify cache keys and history windows do not mix the tiers.
3. In staging only, enable premium polling and configure a quota reserve floor for the premium group. Confirm premium quotes become available only to the paid test subscriber.
4. Return a provider usage response at or below the configured reserve floor. Confirm every key in that account group enters cooldown, other groups remain available, the Admin Overview reports the floor trigger and reset, and a successful HTTP response does not clear the cooldown.
5. Restart the staging service while the group is paused. Confirm the saved quota telemetry restores the group cooldown and that no credential-like values appear in SQLite state, logs, health responses, or Admin Overview.
6. Refresh one account group while another same-tier or premium group has stored quotes. Confirm only the leased group's old quotes are replaced; the other group's quote history and CLV provenance remain intact.
7. Remove an account-group classification and test an unrecognized group. Confirm both fail closed as restricted and do not appear in free or paid subscriber data. Confirm legacy rows without trustworthy account-group provenance remain restricted.
8. Exercise OddsLink standalone market-limit polling and TheRundown score synchronization with a premium-only group while premium polling is disabled. Confirm neither background path can lease that group.
9. Export the staging database and configuration backup, perform the documented restore/rollback drill, then disable provider polling and stop staging workers.

Acceptance requires captured evidence for each check, no leaked credentials, no cross-tier data, and explicit operator sign-off. Passing automated CI is not a substitute for this live staging exercise.


## Current baseline status — 2026-11-27

The baseline now includes source-provider and account-group provenance for quotes and posted market limits, free/premium/restricted classification, free-versus-paid subscriber scoping for market-limit current/history/change queries, restricted handling for legacy rows without trustworthy provenance, premium-specific CLV tracking, provider quota telemetry, and tests for source-scoped results and diagnostic redaction. This does not mean every derived-data, cache, notification, export, and historical-data path has passed external acceptance. Keep the remaining acceptance tests above as launch gates; unknown provenance must continue to fail closed.
