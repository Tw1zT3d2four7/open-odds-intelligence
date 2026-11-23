from pathlib import Path


def test_provider_diagnostics_are_private_to_the_admin_dashboard():
    root = Path(__file__).resolve().parents[1]
    public_html = (root / "web" / "index.html").read_text()
    admin_html = (root / "web" / "admin.html").read_text()
    assert 'id="providerGrid"' not in public_html
    assert "health.provider_keys" not in public_html
    assert "health.provider_coverage" not in public_html
    assert "provider_usage" not in public_html
    assert "account_group" not in public_html
    assert "provider_key" in admin_html
    assert "/api/admin/control" in admin_html
    assert "monthly_objects_remaining" not in public_html
