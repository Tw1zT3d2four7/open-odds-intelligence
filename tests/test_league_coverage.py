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



def test_atp_wta_are_default_for_therundown_but_not_generic_odds_api_keys():
    assert "tennis_atp" in config.default_odds_sports("therundown")
    assert "tennis_wta" in config.default_odds_sports("therundown")
    assert "tennis_atp" not in config.default_odds_sports("the_odds_api")
    assert "tennis_wta" not in config.default_odds_sports("the_odds_api")


def test_therundown_has_explicit_atp_and_wta_sport_id_mappings():
    assert TheRundownProvider.SPORTS["tennis_atp"] == (38, "ATP Tennis")
    assert TheRundownProvider.SPORTS["tennis_wta"] == (39, "WTA Tennis")
