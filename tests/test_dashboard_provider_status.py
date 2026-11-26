from pathlib import Path


def test_dashboard_exposes_provider_and_per_key_quota_status():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    assert 'id="providerGrid"' in html
    assert "health.provider_keys" in html
    assert "health.provider_coverage" in html
    assert "prop_quotes" in html
    assert "account_group" in html
    assert "credits_remaining" in html
    assert "monthly_objects_remaining" in html
    assert "auto_refresh_schedule" in html



def test_dashboard_paginates_at_bottom_and_hides_card_for_credit_card_provider():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    assert 'id="paginationBottom"' in html
    assert 'id="pagePrev"' in html
    assert 'id="pageNext"' in html
    assert 'const allNames=["therundown","the_odds_api","oddize","oddslink"]' in html
    assert "sports_game_odds" not in html
    assert 'health.stored_bookmakers' in html



def test_private_admin_overview_renders_account_group_quota_telemetry():
    html = (Path(__file__).resolve().parents[1] / "web" / "admin.html").read_text()
    assert 'id="overview-provider-accounts"' in html
    assert 'id="overview-provider-policy"' in html
    assert "data.provider_accounts" in html
    assert "credits_remaining" in html
    assert "quota_floor_triggered" in html
    assert "premium_provider_polling_enabled" in html
