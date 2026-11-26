import os
from dotenv import load_dotenv

load_dotenv()
APP_NAME = os.getenv("APP_NAME", "Open Odds Intelligence")
ODDS_PROVIDER = os.getenv("ODDS_PROVIDER", "none").strip().lower()
# Optional simultaneous providers; when omitted, retain the legacy single-provider setting.
ODDS_PROVIDERS_EXPLICIT = bool(os.getenv("ODDS_PROVIDERS", "").strip())
ODDS_PROVIDERS = [
    item.strip().lower() for item in os.getenv("ODDS_PROVIDERS", "").split(",")
    if item.strip()
]
# Source access classification is configured per provider account. Known free-tier
# integrations remain free by default; unknown providers fail closed in the data layer.
_raw_provider_access_classes = os.getenv("ODDS_PROVIDER_ACCESS_CLASSES", "")
ODDS_PROVIDER_ACCESS_CLASSES = {
    "therundown": "free",
    "the_odds_api": "free",
    "oddize": "free",
    "oddslink": "free",
}
# Premium sources are never polled until an operator explicitly enables them.
ODDS_PREMIUM_PROVIDER_POLLING_ENABLED = os.getenv(
    "ODDS_PREMIUM_PROVIDER_POLLING_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
for _entry in _raw_provider_access_classes.replace("\\n", ",").split(","):
    if not _entry.strip():
        continue
    if "=" not in _entry:
        raise ValueError("ODDS_PROVIDER_ACCESS_CLASSES entries must use provider=free or provider=premium.")
    _provider_name, _access_class = (part.strip().lower() for part in _entry.split("=", 1))
    if not _provider_name or _access_class not in {"free", "premium"}:
        raise ValueError("ODDS_PROVIDER_ACCESS_CLASSES supports only explicit free or premium classifications.")
    ODDS_PROVIDER_ACCESS_CLASSES[_provider_name] = _access_class

# Do not normalize or change API key case: provider keys are case-sensitive.
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "")
# TheRundown key is case-sensitive; preserve it exactly.
THERUNDOWN_API_KEY = os.getenv("THERUNDOWN_API_KEY", "")
# Optional comma/newline-separated key pools; legacy single-key values remain supported.
THERUNDOWN_API_KEYS = os.getenv("THERUNDOWN_API_KEYS", "")
THE_ODDS_API_KEYS = os.getenv("THE_ODDS_API_KEYS", "")
# Oddize Developer tier includes one API key on the free plan.
ODDIZE_API_KEY = os.getenv("ODDIZE_API_KEY", "")
ODDIZE_API_KEYS = os.getenv("ODDIZE_API_KEYS", "")
ODDSLINK_API_KEY = os.getenv("ODDSLINK_API_KEY", "")
ODDSLINK_API_KEYS = os.getenv("ODDSLINK_API_KEYS", "")
_oddslink_refresh_interval = max(0, int(os.getenv("ODDSLINK_REFRESH_INTERVAL_SECONDS", "86400")))
ODDSLINK_REFRESH_INTERVAL_SECONDS = (
    max(86400, _oddslink_refresh_interval) if _oddslink_refresh_interval > 0 else 0
)
ODDIZE_PROPS_ENABLED = os.getenv("ODDIZE_PROPS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ODDIZE_PROP_TYPES_PER_REFRESH = max(0, min(5, int(os.getenv("ODDIZE_PROP_TYPES_PER_REFRESH", "1"))))
_oddize_refresh_interval = max(0, int(os.getenv("ODDIZE_REFRESH_INTERVAL_SECONDS", "86400")))
ODDIZE_REFRESH_INTERVAL_SECONDS = (
    max(86400, _oddize_refresh_interval) if _oddize_refresh_interval > 0 else 0
)
ODDIZE_ALTS_ENABLED = os.getenv("ODDIZE_ALTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ODDIZE_ALT_EVENTS_PER_REFRESH = max(0, min(5, int(os.getenv("ODDIZE_ALT_EVENTS_PER_REFRESH", "1"))))
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "us")
# Optional explicit bookmaker keys; when set, this overrides region selection.
ODDS_BOOKMAKERS = os.getenv("ODDS_BOOKMAKERS", "").strip()
ODDS_MARKETS = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")
ODDS_FORMAT = os.getenv("ODDS_FORMAT", "american")
# TheRundown Free is delayed five minutes and capped at 1 request/second.
RUNDOWN_REFRESH_INTERVAL_SECONDS = max(300, int(os.getenv("RUNDOWN_REFRESH_INTERVAL_SECONDS", "14400")))
RUNDOWN_UTC_OFFSET_MINUTES = int(os.getenv("RUNDOWN_UTC_OFFSET_MINUTES", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/odds_history.sqlite3")
# Identity data is stored separately from the immutable odds-history database.
ACCOUNTS_DATABASE_PATH = os.getenv("ACCOUNTS_DATABASE_PATH", "./data/accounts.sqlite3")
SESSION_IDLE_TIMEOUT_SECONDS = max(60, int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "3600")))
SIMULATION_MODEL_PATH = os.getenv("SIMULATION_MODEL_PATH", "./data/simulation_models.json")
# Flag stale odds snapshots without implying a manual-refresh feed is continuously live.
ODDS_DATA_STALE_AFTER_SECONDS = max(300, int(os.getenv("ODDS_DATA_STALE_AFTER_SECONDS", "21600")))
# TheRundown score-only sync is separate from odds polling and uses affiliate_ids=0.
# Enable by default only when TheRundown is the active odds provider; can be disabled explicitly.
SIMULATION_RESULTS_SYNC_ENABLED = os.getenv("SIMULATION_RESULTS_SYNC_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
SIMULATION_RESULTS_SYNC_INTERVAL_SECONDS = max(3600, int(os.getenv("SIMULATION_RESULTS_SYNC_INTERVAL_SECONDS", "14400")))
SIMULATION_RESULTS_RETRY_SECONDS = max(3600, int(os.getenv("SIMULATION_RESULTS_RETRY_SECONDS", "14400")))
SIMULATION_RESULTS_LOOKBACK_DAYS = max(1, min(30, int(os.getenv("SIMULATION_RESULTS_LOOKBACK_DAYS", "7"))))
# Optional rechecks for recently settled games; 0 preserves the lowest-quota behavior.
SIMULATION_RESULTS_RECHECK_DAYS = max(0, min(7, int(os.getenv("SIMULATION_RESULTS_RECHECK_DAYS", "0"))))
SIMULATION_RESULTS_MAX_GROUPS_PER_RUN = max(1, min(50, int(os.getenv("SIMULATION_RESULTS_MAX_GROUPS_PER_RUN", "10"))))
BASE_DEFAULT_ODDS_SPORTS = (
    "americanfootball_nfl,americanfootball_ncaaf,basketball_nba,basketball_ncaab,"
    "icehockey_nhl,baseball_mlb,soccer_epl"
)
PROVIDER_DEFAULT_ODDS_SPORTS = {
    # TheRundown exposes ATP and WTA as stable league IDs. The Odds API uses
    # tournament-specific tennis keys, so it must not receive generic aliases.
    "therundown": BASE_DEFAULT_ODDS_SPORTS + ",tennis_atp,tennis_wta",
    "the_odds_api": BASE_DEFAULT_ODDS_SPORTS,
    "oddize": (
        "americanfootball_nfl,americanfootball_ncaaf,basketball_nba,"
        "basketball_ncaab,icehockey_nhl,baseball_mlb"
    ),
    "oddslink": (
        "americanfootball_nfl,americanfootball_ncaaf,basketball_nba,"
        "basketball_ncaab,icehockey_nhl,baseball_mlb,soccer_epl,"
        "tennis_atp,tennis_wta"
    ),
}


def default_odds_sports(provider):
    return PROVIDER_DEFAULT_ODDS_SPORTS.get(provider, BASE_DEFAULT_ODDS_SPORTS).split(",")


ODDS_SPORTS = [
    sport.strip() for sport in os.getenv(
        "ODDS_SPORTS", ",".join(default_odds_sports(ODDS_PROVIDER))
    ).split(",") if sport.strip()
]

# Automatic refresh is opt-in; 0 keeps refreshes manual.
_odds_api_refresh_interval = max(0, int(os.getenv("ODDS_REFRESH_INTERVAL_SECONDS", "0")))
# Keep the 500-credit free tier from being drained by frequent background polling.
ODDS_REFRESH_INTERVAL_SECONDS = (
    max(86400, _odds_api_refresh_interval) if _odds_api_refresh_interval > 0 else 0
)

# Discord webhook alerts are disabled unless a URL is configured.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# Final-score lookups power in-place Discord GAME STARTED / FINAL edits.
# Default on only when Discord and an active TheRundown key are both configured.
_selected_result_providers = ODDS_PROVIDERS if ODDS_PROVIDERS_EXPLICIT else ([ODDS_PROVIDER] if ODDS_PROVIDER else [])
_default_discord_result_sync = bool(DISCORD_WEBHOOK_URL and "therundown" in _selected_result_providers)
DISCORD_ALERT_RESULTS_SYNC_ENABLED = os.getenv(
    "DISCORD_ALERT_RESULTS_SYNC_ENABLED",
    "true" if _default_discord_result_sync else "false",
).strip().lower() in ("1", "true", "yes", "on")
ALERT_MIN_EV_PERCENT = max(5.0, float(os.getenv("ALERT_MIN_EV_PERCENT", "5.0")))
ALERT_MIN_ARB_PERCENT = max(1.0, float(os.getenv("ALERT_MIN_ARB_PERCENT", "1.0")))

# Re-evaluate locally stored quotes on this interval; this never triggers provider polling.
DISCORD_ALERT_REFRESH_INTERVAL_SECONDS = max(60, int(os.getenv("DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", "300")))
DISCORD_ALERT_MAX_PER_CYCLE = max(1, min(3, int(os.getenv("DISCORD_ALERT_MAX_PER_CYCLE", "3"))))

# Optional OddsLink free-tier posted-market-limit feed. Keys are case-sensitive.
ODDSLINK_API_KEY = os.getenv("ODDSLINK_API_KEY", "")
# Five configured sports x six polls/day x 30 days = about 900 calls/month.
LIMITS_REFRESH_INTERVAL_SECONDS = max(86400, int(os.getenv("LIMITS_REFRESH_INTERVAL_SECONDS", "86400")))
LIMITS_SPORTS = [s.strip() for s in os.getenv("LIMITS_SPORTS", "").split(",") if s.strip()]
LIMITS_ALERT_DECREASES = os.getenv("LIMITS_ALERT_DECREASES", "false").strip().lower() in ("1", "true", "yes", "on")

# Track the first positive-EV quote per exact selection for consensus CLV.
CLV_MIN_EV_PERCENT = float(os.getenv("CLV_MIN_EV_PERCENT", "1.0"))

# Ignore pregame snapshots older than this when measuring closing-line value.
CLV_MAX_CLOSE_AGE_MINUTES = max(1, int(os.getenv("CLV_MAX_CLOSE_AGE_MINUTES", "120")))
# Retry briefly after kickoff for a delayed final pregame refresh; post-start
# quotes remain ineligible regardless of this grace period.
CLV_CLOSE_GRACE_MINUTES = max(0, int(os.getenv("CLV_CLOSE_GRACE_MINUTES", "180")))

# Player props use The Odds API's per-event endpoint and are disabled by default.
PLAYER_PROPS_ENABLED = os.getenv("PLAYER_PROPS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
PLAYER_PROPS_SPORT = os.getenv("PLAYER_PROPS_SPORT", "basketball_nba").strip()
PLAYER_PROPS_MARKETS = os.getenv("PLAYER_PROPS_MARKETS", "").strip()
PLAYER_PROPS_REFRESH_INTERVAL_SECONDS = max(3600, int(os.getenv("PLAYER_PROPS_REFRESH_INTERVAL_SECONDS", "14400")))
PLAYER_PROPS_MAX_EVENTS = max(1, min(50, int(os.getenv("PLAYER_PROPS_MAX_EVENTS", "5"))))

# Authentication migration is explicit. Identity mode activates staged identity
# endpoints and product-route session/entitlement guards; legacy_basic remains
# the default until the non-production cutover and rollback gates are approved.
AUTH_MODE = os.getenv("AUTH_MODE", "legacy_basic").strip().lower()
if AUTH_MODE not in {"legacy_basic", "identity"}:
    raise ValueError("AUTH_MODE must be either 'legacy_basic' or 'identity'.")

# Legacy HTTP Basic credentials remain unchanged throughout the compatibility phase.
# Set BOTH values to enable; leave BOTH blank to preserve existing behavior in legacy_basic.
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

# Bounded retries apply only to transient non-quota provider failures.
PROVIDER_RETRY_ATTEMPTS = max(0, min(3, int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "2"))))
PROVIDER_RETRY_BASE_DELAY_SECONDS = max(0.1, min(5.0, float(os.getenv("PROVIDER_RETRY_BASE_DELAY_SECONDS", "0.5"))))
PROVIDER_RETRY_MAX_DELAY_SECONDS = max(PROVIDER_RETRY_BASE_DELAY_SECONDS, min(30.0, float(os.getenv("PROVIDER_RETRY_MAX_DELAY_SECONDS", "5"))))

# Dedicated Fernet key for administrator TOTP seeds. Generate out of band;
# never commit this value or reuse an odds-provider API key.
MFA_ENCRYPTION_KEY = os.getenv("MFA_ENCRYPTION_KEY", "")

# Required keyed-hash secret before persistent authentication rate limits are enabled.
RATE_LIMIT_HASH_KEY = os.getenv("RATE_LIMIT_HASH_KEY", "")

# Transactional email provider settings. API keys are case-sensitive; preserve exactly.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend").strip().lower()
if EMAIL_PROVIDER not in {"resend", "disabled"}:
    raise ValueError("EMAIL_PROVIDER must be either 'resend' or 'disabled'.")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
# Dedicated Fernet key for encrypted retry payloads; never reuse the MFA key.
EMAIL_QUEUE_ENCRYPTION_KEY = os.getenv("EMAIL_QUEUE_ENCRYPTION_KEY", "")
# Separate encryption key for offline identity database backups.
IDENTITY_BACKUP_ENCRYPTION_KEY = os.getenv("IDENTITY_BACKUP_ENCRYPTION_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


# Stripe webhook intake is inert until a dedicated signing secret is configured.
# The signing secret must be supplied out-of-band and must never be committed.
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# Dedicated Fernet key for encrypted durable Stripe webhook event bodies.
STRIPE_WEBHOOK_ENCRYPTION_KEY = os.getenv("STRIPE_WEBHOOK_ENCRYPTION_KEY", "")
STRIPE_WEBHOOK_LIVEMODE = os.getenv("STRIPE_WEBHOOK_LIVEMODE", "false").strip().lower() in ("1", "true", "yes", "on")
STRIPE_TEST_MODE_ACKNOWLEDGED = os.getenv("STRIPE_TEST_MODE_ACKNOWLEDGED", "false").strip().lower() in ("1", "true", "yes", "on")
# Stripe secret API key is case-sensitive; keep it out of logs and responses.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
# Stripe recurring Price IDs are public identifiers, not secrets. Preserve them exactly.
# Leave blank until the corresponding recurring Price objects exist in Stripe.
STRIPE_PRICE_MONTHLY_ID = os.getenv("STRIPE_PRICE_MONTHLY_ID", "")
STRIPE_PRICE_YEARLY_ID = os.getenv("STRIPE_PRICE_YEARLY_ID", "")
# Hosted purchase and customer self-service remain explicitly disabled by default.
STRIPE_CHECKOUT_ENABLED = os.getenv("STRIPE_CHECKOUT_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
STRIPE_CUSTOMER_PORTAL_ENABLED = os.getenv("STRIPE_CUSTOMER_PORTAL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
