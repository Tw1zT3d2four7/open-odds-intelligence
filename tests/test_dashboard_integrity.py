import re
from pathlib import Path


DASHBOARD = Path("web/index.html")


def test_dashboard_initial_load_does_not_reference_deferred_panel_payloads():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function load(){")
    end = html.index("async function loadAllBooks(){", start)
    initial_loader = html[start:end]
    assert "allBooks.quotes" not in initial_loader
    assert "movement.movements" not in initial_loader
    assert "getSafe(\"/api/bookmaker-board" not in initial_loader
    assert "getSafe(\"/api/movement" not in initial_loader


def test_every_market_tab_has_a_matching_panel_and_click_handler():
    html = DASHBOARD.read_text(encoding="utf-8")
    tabs = re.findall(r'<button[^>]*data-tab="([^"]+)"', html)
    assert tabs
    for tab in tabs:
        assert f'id="view-{tab}"' in html
    assert 'document.querySelectorAll(\'nav[aria-label="Market views"] .tab\')' in html
    assert 'setTab(b.dataset.tab)' in html


def test_dashboard_preferences_persist_filters_sport_and_active_tab():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert 'openOddsIntelligence.preferences.v1' in html
    assert 'savePreferences({minEv:state.minEv,consensus:state.consensus})' in html
    assert 'savePreferences({sport:state.sport})' in html
    assert 'savePreferences({activeTab:tab})' in html
    assert 'savedPreferences.minEv' in html
    assert 'savedPreferences.consensus' in html


def test_every_standalone_dashboard_button_has_an_action():
    html = DASHBOARD.read_text(encoding="utf-8")
    button_ids = re.findall(r'<button\\b[^>]*\\bid="([^"]+)"', html)
    for button_id in button_ids:
        # The parlay button is wired through the calculator's local element helper.
        if button_id == "addParlayLeg":
            assert 'el("addParlayLeg").addEventListener("click"' in html
        else:
            assert f'$("{button_id}").addEventListener' in html or (
                button_id == "refresh" and 'addEventListener("click",refresh)' in html
            )
