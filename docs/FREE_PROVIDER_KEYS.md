# Free-tier provider API keys

Open Odds Intelligence supports more than one API key for each currently
implemented odds provider. These values are optional and remain server-side in
the private `.env` file.

## Configuration

Keep the existing single-key settings working:

```dotenv
THERUNDOWN_API_KEY=
THE_ODDS_API_KEY=
```

For additional keys, use comma-separated or newline-separated values:

```dotenv
THERUNDOWN_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
THE_ODDS_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
```

The parser trims surrounding whitespace, removes exact duplicates, and
**preserves key case**. The legacy single-key value is also included in the
pool when supplied.

Keys without a group label are assigned to the shared `default` account group.
When a provider explicitly permits independently-quotaed accounts, separate
accounts can be labelled using `account-label|API_KEY` entries, for example:

```dotenv
THE_ODDS_API_KEYS=account-one|first-key,account-two|second-key
```

A quota exhaustion blocks every key in that account group. The system will
only fail over to a different labelled account group; it will not rotate keys
from the same account to evade that account's free-tier limit. Use separate
labels only for genuinely separate accounts with independently granted quotas,
and only where the provider's terms allow it.

## Rotation and failure behavior

The refresh service cycles through configured keys. A key that returns an
explicit quota-exhaustion response (HTTP 429/402 or a recognized exhausted
credits message) is placed in a cooldown; a different available key is tried
for that refresh. A key rejected with HTTP 401 is marked disabled for the
current application process so it is not repeatedly sent. Other provider
errors are surfaced normally and do not automatically poison a key.

The health response reports each key by a short one-way fingerprint, never by
the raw credential. It includes available, quota-cooldown, and disabled states.

Cooldown and disabled state currently live in process memory. Restarting the
application clears those temporary states; durable quota-reset persistence and
a full provider-management dashboard are follow-up work. A provider may apply
quota limits across multiple keys or an account, so multiple keys do not imply
that the provider grants multiplied free allowances. Follow the provider's
terms and actual quota headers.

## SportsGameOdds free tier

The SportsGameOdds adapter is also included. Its published Amateur tier has
a rolling 30-day object allowance, a 10-requests-per-minute limit, and support
for player props and alternate lines. The adapter checks the provider's
free account-usage endpoint before fetching events, caps the result limit to
the remaining object allowance, and is excluded from automatic polling to
protect that allowance. It is available when manually refreshing.

The free tier requires a payment card at signup according to the provider's
setup guide, but the provider states the free tier is not charged unless the
account owner upgrades. Do not upgrade for this integration.

## Oddize free Developer tier

Oddize offers a no-card Developer API tier with 1,500 credits per month, 30
requests per minute, one API key, 20 sportsbooks, and six leagues (MLB, NBA,
NFL, NCAAB, NCAAF and NHL). Current odds cost one credit per league request;
player-prop calls cost two credits per prop type. The adapter checks
`/account/usage` before each metered request, records the reset time and
remaining credits, and rotates through the league's prop types one per manual
refresh by default. It is manual-refresh-only so a four-hour primary-provider
schedule cannot burn through its monthly credits. The free Developer plan
requires only an email address and no credit card. The adapter imports main markets, rotating player/team props, and alternate
spreads/totals for a configurable number of upcoming events per league. By
default it requests alternates for one event per supported league per manual
refresh; each alternate market request costs two credits. Both the prop-type
rotation and alternate-event cap can be reduced or disabled in `.env`.

## Scope

The current foundation supports simultaneous TheRundown, The Odds API,
SportsGameOdds, and Oddize ingestion, with strict event identity matching,
team-abbreviation reconciliation, quote-level source attribution, and
partial-refresh reporting. Additional adapters and more granular
provider-specific scheduling remain follow-up work.
