# Free and Premium Provider Data Isolation

**Status:** Approved product architecture; implementation required before external subscriber testing.  
**Working branch:** `feature/admin-panel-and-subscriptions`

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
- Keep all implementation on `feature/admin-panel-and-subscriptions`. No changes to `main` or production without explicit approval.


## Implementation update — 2026-11-26

The feature branch now persists market-limit provenance alongside each posted-limit observation:

- source_provider identifies the provider source.
- source_account_group currently stores a non-secret fingerprint of the configured provider key set. It is useful for identifying which configured key set produced the observation, but it is **not yet independent per-account grouping**.
- source_access_class records free, premium, or restricted.
- source_capability identifies the posted_market_limits capability.
- Existing SQLite databases receive additive columns. Historical rows without trustworthy classification retain the restricted default.

The current-limits query, change-history window, and exact-selection history apply the caller's free/premium scope before selecting latest observations or calculating changes. Change windows are partitioned by source metadata so separate source streams are not compared as if they were one feed. Identity-mode free subscribers receive only free-class limit records; paid subscribers receive free and premium records; restricted/unknown records remain hidden from both subscriber tiers. The limits endpoint also suppresses premium-source quota and diagnostic details from free subscribers.

Automated coverage includes free-versus-paid current limits, exact-selection history, source-partitioned change windows, restricted legacy rows, and premium diagnostic redaction. CI run 45746913326 passed **990 tests with 1 non-blocking warning**.

Still outstanding: independent access-class configuration and quota accounting for multiple account groups under the same provider; premium-specific CLV tracking; broader provider usage metering/cost guardrails; and isolated HTTPS staging acceptance. Premium polling remains disabled by default, and this implementation does not authorize provider purchases, production deployment, merge, or live billing.


### Provider account-group key handling — 2026-11-26

Provider key pools accept optional `group|API_KEY` entries in the existing comma/newline-separated key settings. Keys without a group remain in the shared `default` group. The service now passes the raw configured key-entry strings into the pool rather than flattening them first, preserving group identity for quota cooldowns and status reporting. A quota response from one key pauses every configured key in that same group, while keys in a different group remain independently eligible. API key bytes remain case-preserved; group labels are metadata, not credentials.

This closes a key-pool wiring gap but does not yet enable independent free/premium access classification or independent polling schedules by account group. Provider access classes are still provider-level for ingestion and scheduling. Do not configure mixed free/premium account groups for one provider expecting separate entitlement behavior until that next layer is implemented and tested.


### Tier-separated CLV tracking — 2026-11-26

CLV entries now use access class as part of their exact-selection identity, allowing a free and premium observation for the same event/book/market/outcome/line to be stored independently. Existing SQLite databases are migrated transactionally by rebuilding the CLV table, preserving row IDs and historical values while assigning legacy unknown rows to the restricted class. The migration recreates the pending index and changes spread duplicate detection to include access class.

Refresh processing now calculates free CLV from the free-only event view. Premium CLV is calculated from a premium-only projection of the paid view, with non-premium outcomes removed before market consensus is computed. This prevents a premium entry's fair price from being derived from free-tier quotes. Closing-consensus snapshot and quote selection are also restricted to the entry's exact access class. Subscriber-facing report scope remains free-only or free-plus-premium.

These changes are implemented but require CI verification before acceptance. Per-account classification and independent account-group polling remain outstanding.
