from pathlib import Path


def test_dashboard_exposes_provider_and_per_key_quota_status():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    assert 'id="providerGrid"' in html
    assert "health.provider_keys" in html
    assert "account_group" in html
    assert "credits_remaining" in html
    assert "monthly_objects_remaining" in html
    assert "auto_refresh_schedule" in html
