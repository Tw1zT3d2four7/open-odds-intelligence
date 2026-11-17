import os
from dotenv import load_dotenv

load_dotenv()
APP_NAME = os.getenv("APP_NAME", "Open Odds Intelligence")
ODDS_PROVIDER = os.getenv("ODDS_PROVIDER", "none").strip().lower()
# Do not normalize or change API key case: provider keys are case-sensitive.
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "")
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "us")
ODDS_MARKETS = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")
ODDS_FORMAT = os.getenv("ODDS_FORMAT", "american")
ODDS_SPORTS = [s.strip() for s in os.getenv(
    "ODDS_SPORTS",
    "americanfootball_nfl,basketball_nba,icehockey_nhl,baseball_mlb,soccer_epl"
).split(",") if s.strip()]
