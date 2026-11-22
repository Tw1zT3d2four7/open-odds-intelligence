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

## Scope

This is key-pool support for the existing TheRundown and The Odds API adapters.
It does not yet enable simultaneous multi-provider ingestion or add sportsbook
coverage by itself. Those are separate stages so provider data can be merged
and deduplicated safely at the event / bookmaker / market / outcome / line
level before the application marks a refresh complete.
