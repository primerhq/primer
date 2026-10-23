from __future__ import annotations

import json
from pathlib import Path

from tests.ui._web_settings_harness import (
    real_not_bootstrapped_envelope,
    web_ui,
)

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

LOCAL = {"id": "local", "provider_type": "local", "config": {"type": "local"}}
JINA = {"id": "jina-1", "provider_type": "jina", "config": {"type": "jina", "api_key": None}}
FC = {"id": "fc-1", "provider_type": "firecrawl", "config": {"type": "firecrawl", "api_key": "**********"}}

DDG = {"id": "DuckDuckGo", "provider_type": "duckduckgo", "config": {"type": "duckduckgo"}}
TAV = {"id": "tavily-a", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "**********"}}
EXA = {"id": "exa-b", "provider_type": "exa", "config": {"type": "exa", "api_key": "**********"}}

# Suppress unused import warning for web_ui fixture
_ = web_ui


def _seed_standard_resources(ui) -> None:
    ui.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}},
    )
    ui.set_resource("web:search:providers", data=[DDG])
    ui.set_resource(
        "web:fetch:active",
        data={"id": "active", "config": {"mode": "single", "provider_id": "local"}},
    )
    ui.set_resource("web:fetch:providers", data=[LOCAL, JINA, FC])


def test_a_non_admin_gets_a_notice_and_no_request(web_ui) -> None:
    ui = web_ui(role="user")
    ui.mount("WEB_SettingsPage")

    assert ui.find("web-admin-only") is not None
    assert any("Web settings need the admin role" in t for t in ui.texts())
    assert ui.asked_keys() == []
    assert ui.calls() == []


def test_the_header_is_the_kit_header_over_the_fetchers(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    hdr = ui.kit("PK_PageHeader", "Web")
    assert hdr is not None
    assert hdr["count"] == "3 fetchers"
    assert hdr["filter"]["label"] == "Filter fetchers"
    assert hdr["filter"]["testid"] == "web-fetcher-filter"
    assert hdr["action"] == {"label": "+ Fetcher", "testid": "web-add-fetcher"}
    assert hdr["pager"]["total"] == 3


def test_the_fetcher_cards_say_built_in_and_offer_no_delete(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    card_local = ui.kit("PK_Card", "local")
    assert card_local is not None
    assert card_local.get("badge") == "built in"
    assert card_local["title"] == "local"
    assert card_local["subtitle"] == "Local"
    assert card_local["facts"] == ["Fetches pages directly; no key", "Default fetcher"]
    assert "menu" not in card_local or card_local["menu"] is None

    card_jina = ui.kit("PK_Card", "jina-1")
    assert card_jina is not None
    assert card_jina["subtitle"] == "Jina Reader"
    assert card_jina["facts"] == ["No key (rate-limited)"]

    card_fc = ui.kit("PK_Card", "fc-1")
    assert card_fc is not None
    assert card_fc["facts"] == ["Key set"]

    sorted_ids = ui.js(f'WEB_sortProviders("fetch", {json.dumps([JINA, FC, LOCAL])}).map(function (r) {{ return r.id; }})')
    assert sorted_ids == ["local", "fc-1", "jina-1"]


def test_in_aggregated_mode_each_card_says_its_place(web_ui) -> None:
    ui = web_ui()
    ui.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}},
    )
    ui.set_resource("web:search:providers", data=[DDG])
    ui.set_resource(
        "web:fetch:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["jina-1", "local"]}},
    )
    ui.set_resource("web:fetch:providers", data=[LOCAL, JINA, FC])
    ui.mount("WEB_SettingsPage")

    card_jina = ui.kit("PK_Card", "jina-1")
    assert card_jina is not None
    assert card_jina["facts"][-1] == "Fetch order: 1 of 2"

    card_local = ui.kit("PK_Card", "local")
    assert card_local is not None
    assert card_local["facts"][-1] == "Fetch order: 2 of 2"

    card_fc = ui.kit("PK_Card", "fc-1")
    assert card_fc is not None
    assert not any("Fetch order" in f for f in card_fc["facts"])


def test_the_filter_narrows_the_cards_and_says_when_nothing_matches(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    ui.call("PK_PageHeader", "Web", "filter.onChange", "zzz")
    assert ui.find("pk-card:local") is None
    assert any('No fetchers match "zzz".' in t for t in ui.texts())
    assert ui.kit("PK_PageHeader", "Web")["count"] == "0 of 3 fetchers"


def test_plus_fetcher_opens_the_register_overlay_for_web_fetch_only(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    ui.click("web-add-fetcher")
    assert ui.overlay() == {"initialClass": "web_fetch", "allowedClasses": ["web_fetch"]}


def test_open_hands_the_whole_row_to_the_overlay(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    ui.click("pk-card-open:jina-1")
    ov = ui.overlay()
    assert ov is not None
    assert ov["editRow"] == JINA
    assert ov["initialClass"] == "web_fetch"
    assert ov["allowedClasses"] == ["web_fetch"]


def test_closing_or_saving_the_overlay_reloads_the_fetchers_and_the_fetch_config(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    ui.click("web-add-fetcher")
    ui.call("PC_RegisterOverlay", "_", "onClose")
    assert ui.overlay() is None
    ref = ui.refetched()
    assert ref.get("web:fetch:providers") == 1
    assert ref.get("web:fetch:active") == 1

    ui2 = web_ui()
    _seed_standard_resources(ui2)
    ui2.mount("WEB_SettingsPage")
    ui2.click("web-add-fetcher")
    ui2.call("PC_RegisterOverlay", "_", "onSaved", {"id": "x"})
    assert ui2.overlay() is None
    ref2 = ui2.refetched()
    assert ref2.get("web:fetch:providers") == 1
    assert ref2.get("web:fetch:active") == 1


def test_without_the_register_overlay_there_is_no_plus_fetcher(web_ui) -> None:
    ui = web_ui(overlay_loaded=False)
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    hdr = ui.kit("PK_PageHeader", "Web")
    assert hdr is not None
    assert hdr.get("action") is None
    assert ui.kit("WEB_ActiveConfigSection", "search") is not None
    assert ui.kit("WEB_ActiveConfigSection", "fetch") is not None


def test_a_search_load_error_leaves_the_fetch_section_working(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", error_envelope=real_not_bootstrapped_envelope())
    ui.set_resource("web:search:providers", data=[DDG])
    ui.set_resource(
        "web:fetch:active",
        data={"id": "active", "config": {"mode": "single", "provider_id": "local"}},
    )
    ui.set_resource("web:fetch:providers", data=[LOCAL])
    ui.mount("WEB_SettingsPage")

    assert ui.find("web-search-error") is not None
    assert ui.kit("PK_Segmented", "web-fetch-mode") is not None


def test_the_manage_link_opens_providers_at_web_search(web_ui) -> None:
    ui = web_ui()
    _seed_standard_resources(ui)
    ui.mount("WEB_SettingsPage")

    ui.click("web-search-manage")
    assert ui.nav() == [["platform", "providers", "web_search"]]


def test_on_a_phone_both_sections_use_one_column(web_ui) -> None:
    ui_mob = web_ui(mobile=True)
    _seed_standard_resources(ui_mob)
    ui_mob.mount("WEB_SettingsPage")
    assert ui_mob.kit("WEB_ActiveConfigSection", "search")["cols"] == 1
    assert ui_mob.kit("WEB_ActiveConfigSection", "fetch")["cols"] == 1

    ui_desk = web_ui(mobile=False)
    _seed_standard_resources(ui_desk)
    ui_desk.mount("WEB_SettingsPage")
    assert ui_desk.kit("WEB_ActiveConfigSection", "search")["cols"] == 2
    assert ui_desk.kit("WEB_ActiveConfigSection", "fetch")["cols"] == 2


def test_count_and_empty_words(web_ui) -> None:
    ui = web_ui()
    assert ui.js("WEB_countText(1, 1)") == "1 fetcher"
    assert ui.js("WEB_countText(3, 3)") == "3 fetchers"
    assert ui.js("WEB_countText(1, 3)") == "1 of 3 fetchers"
    assert ui.js('WEB_emptyText("zzz", 3)') == 'No fetchers match "zzz".'
    assert ui.js('WEB_emptyText("", 0)') == "No fetchers yet."


def test_index_html_loads_the_page_after_the_kit() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    kit_idx = html.index('src="components/shared/platform-kit.jsx"')
    web_idx = html.index('src="components/web-settings.jsx"')
    assert kit_idx < web_idx
