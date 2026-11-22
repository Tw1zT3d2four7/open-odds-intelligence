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
# Do not normalize or change API key case: provider keys are case-sensitive.
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "")
# TheRundown key is case-sensitive; preserve it exactly.
THERUNDOWN_API_KEY = os.getenv("THERUNDOWN_API_KEY", "")
# Optional comma/newline-separated key pools; legacy single-key values remain supported.
THERUNDOWN_API_KEYS = os.getenv("THERUNDOWN_API_KEYS", "")
THE_ODDS_API_KEYS = os.getenv("THE_ODDS_API_KEYS", "")
SPORTSGAMEODDS_API_KEY = os.getenv("SPORTSGAMEODDS_API_KEY", "")
SPORTSGAMEODDS_API_KEYS = os.getenv("SPORTSGAMEODDS_API_KEYS", "")
SPORTSGAMEODDS_MAX_EVENTS = max(1, min(100, int(os.getenv("SPORTSGAMEODDS_MAX_EVENTS", "25"))))
# Oddize Developer tier includes one API key on the free plan.
ODDIZE_API_KEY = os.getenv("ODDIZE_API_KEY", "")
ODDSLINK_API_KEY = os.getenv("ODDSLINK_API_KEY", "")
ODDSLINK_API_KEYS = os.getenv("ODDSLINK_API_KEYS", "")
ODDIZE_PROPS_ENABLED = os.getenv("ODDIZE_PROPS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ODDIZE_PROP_TYPES_PER_REFRESH = max(0, min(5, int(os.getenv("ODDIZE_PROP_TYPES_PER_REFRESH", "1"))))
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
    "sports_game_odds": (
        "americanfootball_nfl,americanfootball_ncaaf,basketball_nba,"
        "basketball_ncaab,icehockey_nhl,baseball_mlb,soccer_mls,"
        "soccer_uefa_champs_league"
    ),
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
ALERT_MIN_EV_PERCENT = float(os.getenv("ALERT_MIN_EV_PERCENT", "3.0"))
ALERT_MIN_ARB_PERCENT = float(os.getenv("ALERT_MIN_ARB_PERCENT", "0.5"))

# Re-evaluate locally stored quotes on this interval; this never triggers provider polling.
DISCORD_ALERT_REFRESH_INTERVAL_SECONDS = max(60, int(os.getenv("DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", "300")))

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

# Optional HTTP Basic authentication for the dashboard and all API routes except health.
# Set BOTH values to enable; leave BOTH blank to preserve private-LAN development behavior.
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

# Bounded retries apply only to transient non-quota provider failures.
PROVIDER_RETRY_ATTEMPTS = max(0, min(3, int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "2"))))
PROVIDER_RETRY_BASE_DELAY_SECONDS = max(0.1, min(5.0, float(os.getenv("PROVIDER_RETRY_BASE_DELAY_SECONDS", "0.5"))))
PROVIDER_RETRY_MAX_DELAY_SECONDS = max(PROVIDER_RETRY_BASE_DELAY_SECONDS, min(30.0, float(os.getenv("PROVIDER_RETRY_MAX_DELAY_SECONDS", "5"))))
