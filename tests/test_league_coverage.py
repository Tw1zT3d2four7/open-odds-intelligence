from app import config
from app.limits import SPORT_MAP, selected_limit_sports
from app.providers.therundown import TheRundownProvider


def test_college_leagues_are_in_default_platform_coverage():
    assert "americanfootball_ncaaf" in config.ODDS_SPORTS
    assert "basketball_ncaab" in config.ODDS_SPORTS


def test_college_leagues_have_therundown_and_oddslink_mappings():
    assert TheRundownProvider.SPORTS["americanfootball_ncaaf"] == (1, "NCAAF")
    assert TheRundownProvider.SPORTS["basketball_ncaab"] == (5, "NCAAB")
    assert SPORT_MAP["americanfootball_ncaaf"] == "ncaaf"
    assert SPORT_MAP["basketball_ncaab"] == "ncaab"


def test_default_oddslink_monitor_does_not_expand_free_tier_polling():
    leagues = [
        "americanfootball_nfl", "americanfootball_ncaaf",
        "basketball_nba", "basketball_ncaab", "icehockey_nhl",
        "baseball_mlb", "soccer_epl",
    ]
    assert selected_limit_sports(leagues, []) == [
        "americanfootball_nfl", "basketball_nba", "icehockey_nhl",
        "baseball_mlb", "soccer_epl",
    ]


def test_explicit_oddslink_sport_selection_is_respected():
    assert selected_limit_sports(
        ["americanfootball_nfl", "americanfootball_ncaaf"],
        ["americanfootball_ncaaf"],
    ) == ["americanfootball_ncaaf"]
