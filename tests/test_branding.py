from pathlib import Path


def test_public_dashboard_embeds_brand_logo_and_favicon():
    page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'src="data:image/svg+xml,' in page
    assert 'href="/favicon.svg"' in page
    assert 'alt="Odds Intelligence"' in page


def test_public_brand_page_exposes_the_official_logo_for_image_discovery():
    root = Path(__file__).resolve().parents[1]
    page = (root / "web" / "brand.html").read_text(encoding="utf-8")
    logo = (root / "web" / "logo.svg").read_text(encoding="utf-8")
    assert 'name="robots" content="index,follow"' in page
    assert 'og:image" content="https://odds-intelligence.ddns.net/logo.svg"' in page
    assert 'alt="Odds Intelligence official logo"' in page
    assert "<title" in logo and "Odds Intelligence" in logo


def test_totp_issuer_uses_the_public_odds_intelligence_brand():
    root = Path(__file__).resolve().parents[1]
    identity = (root / "app" / "identity.py").read_text(encoding="utf-8")
    assert 'issuer_name="Odds Intelligence"' in identity
