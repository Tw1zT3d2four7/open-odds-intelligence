from pathlib import Path


def test_public_dashboard_embeds_brand_logo_and_favicon():
    page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'src="data:image/svg+xml,' in page
    assert 'href="data:image/svg+xml,' in page
    assert 'alt="Odds Intelligence"' in page
