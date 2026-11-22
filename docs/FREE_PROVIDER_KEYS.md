# Free-tier provider API keys

Open Odds Intelligence supports more than one API key for each currently
implemented odds provider. These values are optional and remain server-side in
the private `.env` file.

## Configuration

Enable every currently integrated free source by listing the providers
whose keys you have configured:

```dotenv
ODDS_PROVIDERS=therundown,the_odds_api,sports_game_odds,oddize,oddslink
ODDS_SPORTS=americanfootball_nfl,americanfootball_ncaaf,basketball_nba,basketball_ncaab,icehockey_nhl,baseball_mlb,soccer_epl,tennis_atp,tennis_wta
```

Keep the existing single-key settings working:

```dotenv
THERUNDOWN_API_KEY=
THE_ODDS_API_KEY=
SPORTSGAMEODDS_API_KEY=
ODDIZE_API_KEY=
ODDSLINK_API_KEY=
```

For additional keys, use comma-separated or newline-separated values:

```dotenv
THERUNDOWN_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
THE_ODDS_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
SPORTSGAMEODDS_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
ODDSLINK_API_KEYS=first-case-sensitive-key,second-case-sensitive-key
ODDIZE_API_KEY=one-free-developer-key
ODDIZE_API_KEYS=another-free-account-key
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

Quota usage counters are persisted per masked key and displayed in the
Provider Connections dashboard. Temporary disabled/cooldown state is held in
process memory and is rebuilt from live usage checks after restart. The
dashboard shows provider connection state, masked key fingerprints, account
groups, remaining quota where reported, reset information and auto-refresh
intervals. A provider may apply quota limits across multiple keys or an
account, so multiple keys do not imply multiplied free allowances.

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
remaining credits, and rotates through one prop type per supported league per
refresh by default. Its default schedule is daily: six leagues use about 42
credits per day (one main-odds credit, one prop-type request at two credits,
and two alternate-market requests at two credits each), approximately 1,260
credits per month. The free Developer plan requires only an email address and
no credit card. The adapter imports main markets, rotating player/team props,
and alternate spreads/totals for one upcoming event per supported league by
default. Both the prop-type rotation and alternate-event cap can be reduced or
disabled in `.env`.

## OddsLink free tier

OddsLink's Free plan supplies all 47 listed books across its supported sports,
with 1,000 odds requests per month and a 2-requests/second limit. One odds call
returns every available fixture, bookmaker and requested market for that
sport; listing sports, books, fixtures and account usage is free. The Free plan
is pre-match only and does not include player props, in-play prices or history.
The adapter requests main markets, alternate lines, team totals and
available period markets in one request per sport, checks the account's
monthly allowance before each priced request, and preserves the provider's
main-line marker and posted-limit size metadata. Its default schedule is
daily (about 270 priced requests/month for nine sports), safely below the
1,000-request allowance. Player props are intentionally sourced from other
free providers rather than a paid OddsLink tier.

OddsLink supports multiple API keys on an account, but the monthly allowance is
account-wide. Keep all keys from the same account in one account group (the
default); the pool pauses the whole group when its quota is spent.

## Scope

The current foundation supports simultaneous TheRundown, The Odds API,
SportsGameOdds, Oddize, and OddsLink ingestion, with strict event identity
matching, team-abbreviation reconciliation, quote-level source attribution,
alternate-line and posted-limit preservation, independent provider schedules,
quota-aware key rotation, and partial-refresh reporting. The TheRundown
default is every four hours; The Odds API is manual by default and, when
enabled, has a one-day minimum; SportsGameOdds defaults to every three days;
Oddize and OddsLink default to daily.


## Provider plan references

Plan limits and endpoint capabilities are reviewed against the providers' own
documentation and pricing pages; free-tier limits can change, so the adapters
also read live usage endpoints where available.

- TheRundown: https://therundown.io/
- The Odds API: https://the-odds-api.com/
- SportsGameOdds: https://sportsgameodds.com/docs and https://sportsgameodds.com/pricing
- Oddize: https://oddize.com/api/v1/docs and https://oddize.com/odds-api
- OddsLink: https://oddslink.app/docs and https://oddslink.app/pricing

No paid endpoint is required by these integrations. If a market or feature is
available only on a paid plan, the adapter does not request it.
