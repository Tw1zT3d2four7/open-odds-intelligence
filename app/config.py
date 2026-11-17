import os
from dotenv import load_dotenv

load_dotenv()
APP_NAME = os.getenv("APP_NAME", "Open Odds Intelligence")
ODDS_PROVIDER = os.getenv("ODDS_PROVIDER", "none").strip().lower()
# Do not normalize or change API key case: provider keys are case-sensitive.
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "")
# TheRundown key is case-sensitive; preserve it exactly.
THERUNDOWN_API_KEY = os.getenv("THERUNDOWN_API_KEY", "")
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "us")
# Optional explicit bookmaker keys; when set, this overrides region selection.
ODDS_BOOKMAKERS = os.getenv("ODDS_BOOKMAKERS", "").strip()
ODDS_MARKETS = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")
ODDS_FORMAT = os.getenv("ODDS_FORMAT", "american")
# TheRundown Free is delayed five minutes and capped at 1 request/second.
RUNDOWN_REFRESH_INTERVAL_SECONDS = max(300, int(os.getenv("RUNDOWN_REFRESH_INTERVAL_SECONDS", "14400")))
RUNDOWN_UTC_OFFSET_MINUTES = int(os.getenv("RUNDOWN_UTC_OFFSET_MINUTES", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/odds_history.sqlite3")
ODDS_SPORTS = [s.strip() for s in os.getenv(
    "ODDS_SPORTS",
    "americanfootball_nfl,basketball_nba,icehockey_nhl,baseball_mlb,soccer_epl"
).split(",") if s.strip()]

# Automatic refresh is opt-in; 0 keeps refreshes manual.
ODDS_REFRESH_INTERVAL_SECONDS = max(0, int(os.getenv("ODDS_REFRESH_INTERVAL_SECONDS", "0")))

# Discord webhook alerts are disabled unless a URL is configured.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERT_MIN_EV_PERCENT = float(os.getenv("ALERT_MIN_EV_PERCENT", "3.0"))
ALERT_MIN_ARB_PERCENT = float(os.getenv("ALERT_MIN_ARB_PERCENT", "0.5"))

# When Discord is enabled, keep checking markets even if general auto-refresh is off.
DISCORD_ALERT_REFRESH_INTERVAL_SECONDS = max(60, int(os.getenv("DISCORD_ALERT_REFRESH_INTERVAL_SECONDS", "300")))
